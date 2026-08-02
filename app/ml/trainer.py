"""模型训练:多股票历史数据 -> 三分类涨跌模型(时间切分验证)。"""
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
from app.ml.dataset import (LABEL_NAME, build_dataset, time_split)


def _history_groups(verbose: bool = True):
    """按代码分组的历史数据,供多股票训练。"""
    groups = {}
    for code in config.TRAIN_STOCK_CODES:
        try:
            df = get_daily_history(code, days=config.HIST_DAYS, adjust="qfq")
            df = df.tail(int(config.HIST_DAYS))
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


def train(horizon: int = None, threshold: float = None, verbose: bool = True) -> dict:
    """训练并保存模型,返回评估摘要。"""
    horizon = horizon or config.PREDICT_HORIZON
    threshold = threshold or config.PREDICT_THRESHOLD
    t0 = time.time()

    if verbose:
        print(f"[训练] 开始,horizon={horizon}日,threshold={threshold:.1%}")
    groups = _history_groups(verbose)
    data, y, feature_names = build_dataset(list(groups.values()), horizon, threshold)
    train_df, test_df, split_date = time_split(data)
    if verbose:
        print(f"[训练] 样本: {len(data)}(train {len(train_df)} / test {len(test_df)}), "
              f"特征: {len(feature_names)}, 切分日: {split_date.date()}")

    if len(train_df) < config.MIN_TRAIN_SAMPLES:
        raise RuntimeError(f"训练样本不足: {len(train_df)} < {config.MIN_TRAIN_SAMPLES}")

    X_tr = train_df[feature_names].values
    y_tr = train_df["label"].values
    X_te = test_df[feature_names].values
    y_te = test_df["label"].values

    model = LGBMClassifier(
        n_estimators=400,
        learning_rate=0.04,
        max_depth=3,
        num_leaves=8,
        reg_lambda=1.0,
        min_child_samples=50,
        subsample=0.8,
        colsample_bytree=0.8,
        verbosity=-1,
        random_state=42,
    )
    model.fit(X_tr, y_tr)

    # ---- 评估
    proba = model.predict_proba(X_te)
    pred = model.predict(X_te)
    acc = accuracy_score(y_te, pred)
    f1 = f1_score(y_te, pred, average="weighted", zero_division=0)
    prec = precision_score(y_te, pred, average="weighted", zero_division=0)
    rec = recall_score(y_te, pred, average="weighted", zero_division=0)
    cm = confusion_matrix(y_te, pred, labels=[0, 1, 2])

    # ---- 二值化评估(涨 vs 非涨 / 跌 vs 非跌)
    from sklearn.metrics import roc_auc_score
    up_auc = roc_auc_score((y_te == 2).astype(int), proba[:, list(model.classes_).index(2)])
    down_auc = roc_auc_score((y_te == 0).astype(int), proba[:, list(model.classes_).index(0)])

    summary = {
        "model_name": config.MODEL_NAME,
        "framework": "lightgbm",
        "model_type": "LGBMClassifier",
        "trained_at": dt.datetime.now().isoformat(timespec="seconds"),
        "horizon": horizon,
        "threshold": threshold,
        "n_samples": int(len(data)),
        "n_train": int(len(train_df)),
        "n_test": int(len(test_df)),
        "n_features": len(feature_names),
        "split_date": str(pd.Timestamp(split_date).date()),
        "classes": {int(k): v for k, v in LABEL_NAME.items()},
        "metrics": {
            "accuracy": round(float(acc), 4),
            "f1_weighted": round(float(f1), 4),
            "precision_weighted": round(float(prec), 4),
            "recall_weighted": round(float(rec), 4),
            "up_rank_metric": round(float(up_auc), 4),
            "down_rank_metric": round(float(down_auc), 4),
            "confusion_matrix": cm.tolist(),
        },
        "feature_names": feature_names,
        "train_codes": list(groups.keys()),
        "elapsed_sec": round(time.time() - t0, 1),
    }

    # ---- 保存
    path = os.path.join(config.MODEL_DIR, f"{config.MODEL_NAME}_h{horizon}.joblib")
    joblib.dump({"model": model, "meta": summary}, path)
    with open(path.replace(".joblib", ".json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    if verbose:
        print(f"[训练] 完成(LightGBM), 总准确率 {acc:.2%}, F1 {f1:.3f}, 耗时 {summary['elapsed_sec']}s")
        print(f"[训练] 混淆矩阵(行=真实, 列=预测) [下跌/震荡/上涨]:")
        print(cm)
        print(f"[训练] 模型已保存: {path}")

    return summary


def train_all(verbose: bool = True):
    """训练默认模型(入口用)。"""
    return train(horizon=config.PREDICT_HORIZON, threshold=config.PREDICT_THRESHOLD,
                 verbose=verbose)


if __name__ == "__main__":
    train_all()
