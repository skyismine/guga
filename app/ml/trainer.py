"""模型训练:多股票历史数据 -> 三分类涨跌模型。

验证方式(默认)为时间序列滚动前视交叉验证(Walk-Forward Validation):
把时间轴按交易日切为 K 段,前 INITIAL 段作初始训练,其后每段依次作为测试折,
每折训练集 = 该测试折之前的所有历史(expand)或固定滚动窗口,保证全部无前视;
逐折报告样本外指标并求平均,取最后一折(最新时段)模型部署——最贴合实盘连续预测。
可用 TRAIN_MODE="single_split" 回退到单次时间切分。
"""
import datetime as dt
import json
import os
import time

import joblib
import numpy as np
import pandas as pd
from lightgbm import LGBMClassifier
from sklearn.metrics import (accuracy_score, confusion_matrix,
                             f1_score, precision_score, recall_score)

from app import config
from app.data.fetcher import get_daily_history, get_stock_name
from app.ml.dataset import LABEL_NAME, build_dataset, time_split
from app.ml.pool_builder import (filter_df_by_active_periods,
                                 load_training_pool, mainline_codes,
                                 sample_weights)

_METRIC_KEYS = ("accuracy", "f1_weighted", "precision_weighted",
                "recall_weighted", "up_rank_metric", "down_rank_metric")


def _pool_codes(verbose: bool = True):
    """训练池代码 + A500 调样期(池缺失时回退静态配置)。"""
    pool = load_training_pool()
    codes = pool.get("codes")
    periods = pool.get("a500_periods") or []
    if not codes:
        codes = list(config.TRAIN_STOCK_CODES)
    return codes, periods


def _history_groups(verbose: bool = True):
    """按代码分组的历史数据,供多股票训练(含 A500 幸存者偏差裁剪)。"""
    codes, periods = _pool_codes(verbose)
    # 需多取:滚动窗口(400) + 特征 warmup + 标签 horizon 对齐,统一放宽到 3 倍
    fetch_days = int(max(config.HIST_DAYS, getattr(config, "WF_FIXED_WINDOW_DAYS", 0) * 3))
    groups = {}
    for code in codes:
        try:
            df = get_daily_history(code, days=fetch_days, adjust="qfq")
            df = df.tail(fetch_days)
            # A500 历史成分期裁剪:调出股票只用其有效期内样本(消除幸存者偏差)
            if periods:
                df = filter_df_by_active_periods(df, code, periods)
            df.name = code
            groups[code] = df
            if verbose:
                print(f"  [数据] {code} {get_stock_name(code)}: {len(df)} 行")
        except Exception as e:  # noqa: BLE001
            if verbose:
                print(f"  [数据] {code} 获取失败: {e}")
        time.sleep(0.3)
    if not groups:
        raise RuntimeError("未获取到任何训练数据")
    return groups


def _model_params() -> dict:
    return dict(n_estimators=400, learning_rate=0.04, max_depth=3, num_leaves=8,
                reg_lambda=1.0, min_child_samples=50, subsample=0.8,
                colsample_bytree=0.8, verbosity=-1, random_state=42)


def _build_training_data(horizon: int, threshold: float, verbose: bool):
    """数据准备:历史分组 -> 特征+标签 -> 特征筛选 -> 滚动 z-score 标准化。

    返回 (groups, data, feature_names, mainline_set)。
    mainline_set:主线标的代码集合(供各折内计算样本权重),可为空集。
    """
    groups = _history_groups(verbose)
    data, _, feature_names = build_dataset(list(groups.values()), horizon, threshold)
    if getattr(config, "FEATURE_SELECT", True):
        from app.features.select_features import apply_selection
        selected = apply_selection(data, feature_names)
        if len(selected) < len(feature_names) and verbose:
            print(f"[训练] 特征筛选: {len(feature_names)} -> {len(selected)}")
        feature_names = selected
    from app.features.standardize import standardize_dataset
    data = standardize_dataset(data, feature_names)
    data = data.dropna(subset=feature_names)
    mainline = set()
    if getattr(config, "SAMPLE_WEIGHT_ENABLED", True):
        try:
            mainline = mainline_codes()
        except Exception as e:  # noqa: BLE001
            if verbose:
                print(f"[训练] 主线标的解析失败,样本加权退化为纯类别平衡: {e}")
    if verbose:
        vc = data["label"].value_counts(normalize=True)
        print(f"[训练] 样本: {len(data)}, 特征: {len(feature_names)}, "
              f"三类占比: 下跌 {vc.get(0, 0):.1%} / 震荡 {vc.get(1, 0):.1%} / "
              f"上涨 {vc.get(2, 0):.1%}")
    atr_baseline = None
    # ATR baseline 必须用标准化前的原始 atr_pct(标准化后中位数≈0 无意义)。
    # 从 groups 原始日线估算:各股最新 atr14/close 的中位数。
    try:
        import vectorbt as vbt
        vals = []
        for code, g in groups.items():
            if len(g) < 30 or {"high", "low", "close"}.issubset(g.columns):
                atr = vbt.indicators.ATR.run(g["high"], g["low"], g["close"],
                                             window=14).atr
                vals.append(float((atr / g["close"]).dropna().median()))
        if vals:
            atr_baseline = float(np.median(vals))
    except Exception:  # noqa: BLE001
        atr_baseline = None
    return groups, data, feature_names, mainline, atr_baseline


def _class_avg_returns(train_df: pd.DataFrame, groups: dict, horizon: int) -> dict:
    """各类别平均未来收益(训练段统计,用于预测期输出预期涨跌幅/盈亏比)。"""
    fwd_cache = {code: g["close"].shift(-horizon) / g["close"] - 1
                 for code, g in groups.items()}
    tmp = train_df.copy()
    tmp["_fwd_ret"] = [fwd_cache[c].get(d, np.nan)
                       for c, d in zip(tmp["code"], tmp.index)]
    avg = tmp.groupby("label")["_fwd_ret"].mean()
    return {int(k): round(float(v), 5) for k, v in avg.items()}


def _eval_metrics(model, X_te: np.ndarray, y_te: np.ndarray) -> dict:
    """测试集评估(含混淆矩阵与涨/跌二值 AUC)。"""
    from sklearn.metrics import roc_auc_score
    proba = model.predict_proba(X_te)
    pred = model.predict(X_te)
    acc = accuracy_score(y_te, pred)
    f1 = f1_score(y_te, pred, average="weighted", zero_division=0)
    prec = precision_score(y_te, pred, average="weighted", zero_division=0)
    rec = recall_score(y_te, pred, average="weighted", zero_division=0)
    cm = confusion_matrix(y_te, pred, labels=[0, 1, 2])
    up_auc = roc_auc_score((y_te == 2).astype(int),
                           proba[:, list(model.classes_).index(2)])
    down_auc = roc_auc_score((y_te == 0).astype(int),
                             proba[:, list(model.classes_).index(0)])
    return {"accuracy": acc, "f1_weighted": f1, "precision_weighted": prec,
            "recall_weighted": rec, "up_rank_metric": up_auc,
            "down_rank_metric": down_auc, "confusion_matrix": cm.tolist()}


def _walk_forward_splits(data: pd.DataFrame, k_folds: int, initial_folds: int,
                         fixed_window_days: int):
    """按时间生成 walk-forward 的 (train_df, test_df, start, end),全部无前视。

    - 前 initial_folds 段合并为初始训练集(保证首折样本充足);
    - 其后每段为一次测试折,训练集 = 测试折之前全部历史(expand)或固定滚动窗口。
    """
    dates = sorted(data.index.unique())
    n = len(dates)
    if n < k_folds * config.WF_MIN_TEST_DAYS:
        raise RuntimeError(f"交易日不足: {n} < {k_folds} 段 x {config.WF_MIN_TEST_DAYS} 日")
    bounds = np.array_split(np.arange(n), k_folds)
    segments = [dates[int(b[0]): int(b[-1]) + 1] for b in bounds if len(b)]

    splits = []
    for i in range(initial_folds, len(segments)):
        test_dates = segments[i]
        if len(test_dates) < config.WF_MIN_TEST_DAYS:
            continue
        test_start = test_dates[0]
        if fixed_window_days and fixed_window_days > 0:
            pos = dates.index(test_start)
            train_dates = dates[max(0, pos - fixed_window_days): pos]
            train_df = data[data.index.isin(train_dates)]
        else:
            train_df = data[data.index < test_start]
        test_df = data[data.index.isin(test_dates)]
        splits.append((train_df, test_df,
                       pd.Timestamp(test_start), pd.Timestamp(test_dates[-1])))
    return splits


def walk_forward_train(horizon: int = None, threshold: float = None,
                       verbose: bool = True) -> dict:
    """时间序列滚动前视交叉验证训练。返回评估摘要(含逐折与平均指标)。"""
    horizon = horizon or config.PREDICT_HORIZON
    threshold = threshold or config.PREDICT_THRESHOLD
    t0 = time.time()

    if verbose:
        print(f"[训练] 开始,horizon={horizon}日,threshold={threshold:.1%}(滚动分位数: "
              f"window={config.LABEL_QUANTILE_WINDOW}, "
              f"q=({config.LABEL_QUANTILE_LOW}, {config.LABEL_QUANTILE_HIGH}))")

    groups, data, feature_names, mainline_set, atr_baseline = _build_training_data(horizon, threshold, verbose)
    k = max(3, int(getattr(config, "WF_K_FOLDS", 4)))
    init = max(1, min(k - 2, int(getattr(config, "WF_INITIAL_FOLDS", 1))))
    window = int(getattr(config, "WF_FIXED_WINDOW_DAYS", 0))

    splits = _walk_forward_splits(data, k, init, window)
    if not splits:
        raise RuntimeError("walk-forward 无可评估折(时间窗过短)")
    if verbose:
        print(f"[WF] 时间轴切 {k} 段,初始训练 {init} 段,"
              f"测试折 {len(splits)} 折(window={window or 'expand'})")

    # 特征重要性缓存(最后一折模型)
    last_importance = None
    fold_results = []
    for i, (tr_df, te_df, ts, te) in enumerate(splits, 1):
        if len(tr_df) < config.MIN_TRAIN_SAMPLES:
            if verbose:
                print(f"[WF] 折{i} 训练样本不足 {len(tr_df)} < "
                      f"{config.MIN_TRAIN_SAMPLES},跳过")
            continue
        # 与训练行对齐的样本权重(类别平衡 × 主线 1.2)
        tr_w = None
        if getattr(config, "SAMPLE_WEIGHT_ENABLED", True):
            tr_w = sample_weights(tr_df, mainline_set)

        # 可选:贝叶斯调参(在折内训练集上做时间切分验证,默认关闭)
        params = _model_params()
        if getattr(config, "BAYESIAN_TUNE_ENABLED", False) and len(tr_df) > 2000:
            from app.ml.tune import tune_hyperparams
            dates = sorted(tr_df.index.unique())
            vd = int(getattr(config, "BAYESIAN_TUNE_VERIFY_DAYS", 120))
            va_dates = dates[-vd:]
            va_idx = tr_df.index.isin(va_dates)
            va_w = tr_w[va_idx] if tr_w is not None else None
            params = tune_hyperparams(
                tr_df.loc[~va_idx, feature_names].values,
                tr_df.loc[~va_idx, "label"].values,
                tr_df.loc[va_idx, feature_names].values,
                tr_df.loc[va_idx, "label"].values,
                sample_weight_tr=tr_w[~va_idx] if tr_w is not None else None,
                verbose=verbose)

        model = LGBMClassifier(**params)
        model.fit(tr_df[feature_names].values, tr_df["label"].values,
                  sample_weight=tr_w)
        # 特征重要性在校准前捕获(校准模型不暴露 feature_importances_)
        importance = {}
        if hasattr(model, "feature_importances_"):
            imp = model.feature_importances_
            importance = {feature_names[j]: float(imp[j])
                          for j in np.argsort(imp)[::-1][:10]}
        # 概率校准(默认开启,训练集内部 CV 拟合校准器)
        if getattr(config, "CALIBRATE_ENABLED", True):
            from app.ml.validation import calibrate_probabilities
            model = calibrate_probabilities(
                model, tr_df[feature_names].values, tr_df["label"].values,
                method=config.CALIBRATE_METHOD, cv=int(getattr(config, "CALIBRATE_CV", 3)))
        metrics = _eval_metrics(model, te_df[feature_names].values,
                                te_df["label"].values)
        avg = _class_avg_returns(tr_df, groups, horizon)
        fold_results.append({
            "fold": i,
            "period": f"{ts.date()} ~ {te.date()}",
            "n_train": int(len(tr_df)),
            "n_test": int(len(te_df)),
            "model": model,
            "class_avg_returns": avg,
            "feature_importance_top10": importance,
            **metrics,
        })
        if verbose:
            f = fold_results[-1]
            print(f"[WF] 折{i} {f['period']} | train {f['n_train']} / "
                  f"test {f['n_test']} | acc {f['accuracy']:.2%} "
                  f"F1 {f['f1_weighted']:.3f} | up_auc {f['up_rank_metric']:.3f} "
                  f"down_auc {f['down_rank_metric']:.3f}")

    if not fold_results:
        raise RuntimeError("walk-forward 无有效折")

    mean = {k: round(float(np.mean([f[k] for f in fold_results])), 4)
            for k in _METRIC_KEYS}
    if verbose:
        print(f"[WF] 平均 | acc {mean['accuracy']:.2%} F1 {mean['f1_weighted']:.3f} "
              f"| up_auc {mean['up_rank_metric']:.3f} down_auc {mean['down_rank_metric']:.3f}")

    # ---- 最后一折(最新时段)模型部署
    last = fold_results[-1]
    last_test_start = last["period"].split(" ~ ")[0]

    # 分池验证(默认关闭):用最后一折测试集按训练池分类评估
    split_valid = {}
    if getattr(config, "SPLIT_VALIDATION_ENABLED", False):
        try:
            from app.ml.pool_builder import load_training_pool
            from app.ml.validation import split_validation
            pool = load_training_pool()
            cats = pool.get("categories") or {}
            te = splits[-1][1]
            split_valid = split_validation(
                last["model"], te[feature_names].values, te["label"].values,
                te["code"].values, cats)
        except Exception as e:  # noqa: BLE001
            if verbose:
                print(f"[WF] 分池验证失败: {e}")

    summary = {
        "model_name": config.MODEL_NAME,
        "framework": "lightgbm",
        "model_type": "LGBMClassifier",
        "trained_at": dt.datetime.now().isoformat(timespec="seconds"),
        "horizon": horizon,
        "threshold": threshold,
        "label_method": getattr(config, "LABEL_MODE", "quantile"),
        "label_window": config.LABEL_QUANTILE_WINDOW,
        "label_quantiles": [config.LABEL_QUANTILE_LOW, config.LABEL_QUANTILE_HIGH],
        "n_samples": int(len(data)),
        "n_train": last["n_train"],
        "n_test": last["n_test"],
        "n_features": len(feature_names),
        "split_date": last_test_start,
        "classes": {int(k): v for k, v in LABEL_NAME.items()},
        "class_avg_returns": last["class_avg_returns"],
        "atr_pct_baseline": atr_baseline,
        "metrics": {k: last[k] for k in _METRIC_KEYS}
                  | {"confusion_matrix": last["confusion_matrix"]},
        "feature_importance_top10": last.get("feature_importance_top10") or {},
        "validation": {
            "method": "walk_forward",
            "k_folds": k,
            "initial_folds": init,
            "fixed_window_days": window,
            "calibrated": bool(getattr(config, "CALIBRATE_ENABLED", True)),
            "sample_weighted": bool(getattr(config, "SAMPLE_WEIGHT_ENABLED", True)),
            "split_validation": split_valid or None,
            "test_ratio_note": "滚动前视交叉验证,非随机拆分,无未来信息泄露",
            "folds": [{kk: vv for kk, vv in f.items()
                       if kk not in ("model", "feature_importance_top10")}
                      for f in fold_results],
            "mean": mean,
        },
        "feature_names": feature_names,
        "train_codes": list(groups.keys()),
        "pool_stats": None,
        "elapsed_sec": round(time.time() - t0, 1),
    }
    try:
        from app.ml.pool_builder import pool_report
        summary["pool_stats"] = pool_report()
    except Exception:  # noqa: BLE001
        pass

    _save_model(last["model"], summary, horizon)
    if verbose:
        print(f"[训练] 完成(LightGBM Walk-Forward), 部署模型 = 最新折"
              f"({last['period']}), 耗时 {summary['elapsed_sec']}s")
    return summary


def _save_model(model, summary: dict, horizon: int) -> str:
    path = os.path.join(config.MODEL_DIR, f"{config.MODEL_NAME}_h{horizon}.joblib")
    joblib.dump({"model": model, "meta": summary}, path)
    with open(path.replace(".joblib", ".json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    return path


def single_split_train(horizon: int = None, threshold: float = None,
                       verbose: bool = True) -> dict:
    """单次时间切分训练(single_split 模式,兼容旧流程)。"""
    horizon = horizon or config.PREDICT_HORIZON
    threshold = threshold or config.PREDICT_THRESHOLD
    t0 = time.time()
    if verbose:
        print(f"[训练] 开始,horizon={horizon}日,threshold={threshold:.1%}(滚动分位数: "
              f"window={config.LABEL_QUANTILE_WINDOW}, "
              f"q=({config.LABEL_QUANTILE_LOW}, {config.LABEL_QUANTILE_HIGH}))")

    groups, data, feature_names, mainline_set, atr_baseline = _build_training_data(horizon, threshold, verbose)
    train_df, test_df, split_date = time_split(data)
    tr_w = sample_weights(train_df, mainline_set) \
        if getattr(config, "SAMPLE_WEIGHT_ENABLED", True) else None
    if verbose:
        print(f"[训练] 切分日: {split_date.date()} (train {len(train_df)} / "
              f"test {len(test_df)})")

    if len(train_df) < config.MIN_TRAIN_SAMPLES:
        raise RuntimeError(f"训练样本不足: {len(train_df)} < {config.MIN_TRAIN_SAMPLES}")

    model = LGBMClassifier(**_model_params())
    model.fit(train_df[feature_names].values, train_df["label"].values,
              sample_weight=tr_w)
    importance = {}
    if hasattr(model, "feature_importances_"):
        imp = model.feature_importances_
        importance = {feature_names[j]: float(imp[j])
                      for j in np.argsort(imp)[::-1][:10]}
    if getattr(config, "CALIBRATE_ENABLED", True):
        from app.ml.validation import calibrate_probabilities
        model = calibrate_probabilities(
            model, train_df[feature_names].values, train_df["label"].values,
            method=config.CALIBRATE_METHOD, cv=int(getattr(config, "CALIBRATE_CV", 3)))
    metrics = _eval_metrics(model, test_df[feature_names].values,
                            test_df["label"].values)
    class_avg_returns = _class_avg_returns(train_df, groups, horizon)

    summary = {
        "model_name": config.MODEL_NAME,
        "framework": "lightgbm",
        "model_type": "LGBMClassifier",
        "trained_at": dt.datetime.now().isoformat(timespec="seconds"),
        "horizon": horizon,
        "threshold": threshold,
        "label_method": getattr(config, "LABEL_MODE", "quantile"),
        "label_window": config.LABEL_QUANTILE_WINDOW,
        "label_quantiles": [config.LABEL_QUANTILE_LOW, config.LABEL_QUANTILE_HIGH],
        "n_samples": int(len(data)),
        "n_train": int(len(train_df)),
        "n_test": int(len(test_df)),
        "n_features": len(feature_names),
        "split_date": str(pd.Timestamp(split_date).date()),
        "classes": {int(k): v for k, v in LABEL_NAME.items()},
        "class_avg_returns": class_avg_returns,
        "atr_pct_baseline": atr_baseline,
        "metrics": {k: metrics[k] for k in _METRIC_KEYS}
                  | {"confusion_matrix": metrics["confusion_matrix"]},
        "feature_importance_top10": importance,
        "validation": {"method": "single_split", "test_ratio": config.TEST_RATIO,
                       "calibrated": bool(getattr(config, "CALIBRATE_ENABLED", True)),
                       "sample_weighted": bool(getattr(config, "SAMPLE_WEIGHT_ENABLED", True))},
        "feature_names": feature_names,
        "train_codes": list(groups.keys()),
        "pool_stats": None,
        "elapsed_sec": round(time.time() - t0, 1),
    }
    try:
        from app.ml.pool_builder import pool_report
        summary["pool_stats"] = pool_report()
    except Exception:  # noqa: BLE001
        pass

    path = _save_model(model, summary, horizon)
    if verbose:
        print(f"[训练] 完成(LightGBM), 总准确率 {metrics['accuracy']:.2%}, "
              f"F1 {metrics['f1_weighted']:.3f}, 耗时 {summary['elapsed_sec']}s")
        print(f"[训练] 模型已保存: {path}")
    return summary


def train(horizon: int = None, threshold: float = None, verbose: bool = True) -> dict:
    """训练入口:默认 Walk-Forward 验证,TRAIN_MODE='single_split' 时回退单次切分。"""
    if getattr(config, "TRAIN_MODE", "walk_forward") == "walk_forward":
        return walk_forward_train(horizon, threshold, verbose)
    return single_split_train(horizon, threshold, verbose)


def train_all(verbose: bool = True):
    """训练默认模型(入口用)。"""
    return train(horizon=config.PREDICT_HORIZON, threshold=config.PREDICT_THRESHOLD,
                 verbose=verbose)


if __name__ == "__main__":
    train_all()
