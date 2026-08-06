"""每日信号排序(Top-N):对股票池按"预期收益"排序输出买入候选与风险提示。

信号系统定位:不做自动交易,仅每日收盘后给出"今日买什么/卖什么"。
用相对排序(取池内前 N)替代绝对概率阈值,天然适配个股波动率与市场环境差异;
`rank_backtest` 做 walk-forward 排序回测,验证 top-N 在样本外的真实捕捉能力。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import numpy as np
import pandas as pd

from app import config
from app.data.fetcher import get_daily_history, get_stock_name
from app.features.indicators import compute_features
from app.features.market_features import attach_market_features
from app.features.industry_features import prepare_features
from app.ml.predictor import Predictor


def _one_signal(code: str, predictor: Predictor) -> dict:
    df = get_daily_history(code, days=config.HIST_DAYS, adjust="qfq")
    r = predictor.predict_latest(prepare_features(df, code))
    return {
        "code": code,
        "name": get_stock_name(code),
        "close": round(float(df["close"].iloc[-1]), 2),
        "date": r["date"],
        "p_up": r["p_up"],
        "p_down": r["p_down"],
        "direction": r["direction_cn"],
        "expected_return": float(r["expected_return"]),
        "reward_risk": float(r["reward_risk"]),
    }


def daily_signals(codes=None, predictor: Predictor = None) -> dict:
    """今日股票池信号排序。返回 {all, top(买入候选), risk(风险提示)}。"""
    codes = codes or config.TRAIN_STOCK_CODES
    predictor = predictor or Predictor()

    rows = []
    for code in codes:
        try:
            rows.append(_one_signal(code, predictor))
        except Exception as e:  # noqa: BLE001
            print(f"  [信号] {code} 失败: {e}")

    out = pd.DataFrame(rows).sort_values("expected_return", ascending=False)
    cand = out[(out["p_up"] >= config.RANK_MIN_P_UP) &
               (out["expected_return"] >= config.RANK_MIN_EXP_RET)]
    return {
        "all": out,
        "top": cand.head(config.RANK_TOP_N),
        "risk": out.sort_values("expected_return").head(config.RANK_TOP_N),
    }


def _exp_return_from_proba(proba: pd.DataFrame, class_avg_returns: dict) -> pd.Series:
    """预期收益 = 各类概率 × 各类训练期平均收益。"""
    ret = pd.Series(0.0, index=proba.index)
    for k, v in class_avg_returns.items():
        if k in proba.columns:
            ret = ret + proba[k] * v
    return ret


def rank_backtest(codes=None, top_n: int = None, k_folds: int = None) -> dict:
    """walk-forward 排序回测:把时间轴切为多段,前段初始训练、其后每段逐一测试;
    每折用"该测试折之前全部历史"训练(无前视),对池内股票按预期收益排序,
    统计 top-N / 末位 / 全池的"未来 horizon 日实际收益",跨折汇总。
    与训练验证口径一致,结果比单次切分更贴合实盘连续预测。
    """
    from lightgbm import LGBMClassifier

    from app.ml.dataset import build_dataset
    from app.ml.trainer import (_class_avg_returns, _model_params,
                                _walk_forward_splits)

    codes = codes or config.TRAIN_STOCK_CODES
    top_n = top_n or config.RANK_TOP_N
    horizon = config.PREDICT_HORIZON

    hist = {c: get_daily_history(c, days=config.HIST_DAYS, adjust="qfq") for c in codes}
    for c, df in hist.items():
        df.name = c
    data, _, feature_names = build_dataset(list(hist.values()), horizon)
    if getattr(config, "FEATURE_SELECT", True):
        from app.features.select_features import apply_selection
        feature_names = apply_selection(data, feature_names)
    from app.features.standardize import standardize_dataset
    data = standardize_dataset(data, feature_names)
    data = data.dropna(subset=feature_names)

    k = k_folds or int(getattr(config, "WF_K_FOLDS", 4))
    init = max(1, min(k - 2, int(getattr(config, "WF_INITIAL_FOLDS", 1))))
    window = int(getattr(config, "WF_FIXED_WINDOW_DAYS", 0))
    splits = _walk_forward_splits(data, k, init, window)
    if not splits:
        raise RuntimeError("rank_backtest: 无可评估折(时间窗过短)")

    fwd_cache = {c: df["close"].shift(-horizon) / df["close"] - 1
                 for c, df in hist.items()}

    rows = []
    fold_summaries = []
    for i, (tr_df, te_df, ts, te) in enumerate(splits, 1):
        model = LGBMClassifier(**_model_params())
        model.fit(tr_df[feature_names].values, tr_df["label"].values)
        avg = _class_avg_returns(tr_df, hist, horizon)
        proba = model.predict_proba(te_df[feature_names].values)
        pcols = {c: j for j, c in enumerate(model.classes_)}
        te_df = te_df.copy()
        te_df["_exp"] = sum(proba[:, pcols[k]] * v
                            for k, v in avg.items() if k in pcols)

        fold_rows = []
        for d in sorted(te_df.index.unique()):
            sub = te_df[te_df.index == d]
            if len(sub) < top_n:
                continue
            ranked = sub.sort_values("_exp", ascending=False)
            top_codes = list(ranked["code"].head(top_n))
            bot_codes = list(ranked["code"].tail(top_n))
            top_fwd = np.mean([fwd_cache[c].get(d, np.nan) for c in top_codes])
            bot_fwd = np.mean([fwd_cache[c].get(d, np.nan) for c in bot_codes])
            pool_fwd = np.mean([fwd_cache[c].get(d, np.nan) for c in sub["code"]])
            rows.append({"date": d, "top_fwd": top_fwd, "bot_fwd": bot_fwd,
                         "pool_fwd": pool_fwd, "fold": i})
            fold_rows.append({"top_fwd": top_fwd, "bot_fwd": bot_fwd,
                              "pool_fwd": pool_fwd})
        if fold_rows:
            fr = pd.DataFrame(fold_rows)
            fold_summaries.append({
                "fold": i,
                "period": f"{pd.Timestamp(ts).date()} ~ {pd.Timestamp(te).date()}",
                "days": int(len(fr)),
                "top_fwd_mean": round(float(fr["top_fwd"].mean()), 4),
                "bottom_fwd_mean": round(float(fr["bot_fwd"].mean()), 4),
                "pool_fwd_mean": round(float(fr["pool_fwd"].mean()), 4),
                "top_win_vs_pool": round(float((fr["top_fwd"] > fr["pool_fwd"]).mean()), 4),
            })

    df = pd.DataFrame(rows).set_index("date").dropna()
    summary = {
        "pool": codes,
        "split_date": str(pd.Timestamp(splits[-1][2]).date()),
        "period": f"{df.index[0].date()} ~ {df.index[-1].date()}" if len(df) else "-",
        "days": len(df),
        "top_n": top_n,
        "top_fwd_mean": round(float(df["top_fwd"].mean()), 4),
        "bottom_fwd_mean": round(float(df["bot_fwd"].mean()), 4),
        "pool_fwd_mean": round(float(df["pool_fwd"].mean()), 4),
        "top_hit_rate": round(float((df["top_fwd"] > 0).mean()), 4),
        "top_win_vs_pool": round(float((df["top_fwd"] > df["pool_fwd"]).mean()), 4),
        "validation": {"method": "walk_forward", "k_folds": len(splits),
                       "folds": fold_summaries},
        "class_avg_returns": avg,
        "daily": df,
    }
    return summary


if __name__ == "__main__":
    import sys
    mode = sys.argv[1] if len(sys.argv) > 1 else "today"
    if mode == "bt":
        s = rank_backtest()
        tn = s["top_n"]
        v = s["validation"]
        print(f"\n[排序回测] 池 {len(s['pool'])} 只,top_n={tn},Walk-Forward {v['k_folds']} 折,"
              f" 周期 {s['period']} ({s['days']} 交易日)")
        for f in v["folds"]:
            print(f"  折{f['fold']} {f['period']} ({f['days']}日): "
                  f"Top{tn} {f['top_fwd_mean']:.2%} | 全池 {f['pool_fwd_mean']:.2%} | "
                  f"末位 {f['bottom_fwd_mean']:.2%} | 跑赢 {f['top_win_vs_pool']:.1%}")
        print(f"[排序回测] Top{tn} 未来{config.PREDICT_HORIZON}日均收益 {s['top_fwd_mean']:.2%}"
              f" | 全池均值 {s['pool_fwd_mean']:.2%} | 末位 {s['bottom_fwd_mean']:.2%}")
        print(f"[排序回测] Top{tn} 正向占比 {s['top_hit_rate']:.1%}, "
              f"跑赢全池占比 {s['top_win_vs_pool']:.1%}")
    else:
        res = daily_signals()
        pd.set_option("display.width", 200)
        print("\n[今日信号] 股票池按预期收益排序:")
        print(res["all"].round(4).to_string(index=False))
        print(f"\n[买入候选] Top{config.RANK_TOP_N}(p_up>={config.RANK_MIN_P_UP}, "
              f"预期收益>={config.RANK_MIN_EXP_RET}):")
        print(res["top"].round(4).to_string(index=False))
        print(f"\n[风险提示] 预期收益末位 {config.RANK_TOP_N}:")
        print(res["risk"].round(4).to_string(index=False))
