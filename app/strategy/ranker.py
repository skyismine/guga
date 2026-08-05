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


def rank_backtest(codes=None, top_n: int = None, train_ratio: float = None) -> dict:
    """walk-forward 排序回测:前段训练,后段每个交易日对池内股票按"预期收益"排序,
    统计 top-N 的"未来 horizon 日实际收益",并与全池均值(随机基准)对比。
    无前视:特征只用当日及历史,模型只在训练段上训练,收益为未来真实收益。
    """
    from lightgbm import LGBMClassifier

    from app.ml.dataset import build_dataset, time_split

    codes = codes or config.TRAIN_STOCK_CODES
    top_n = top_n or config.RANK_TOP_N
    train_ratio = train_ratio or (1 - config.TEST_RATIO)
    horizon = config.PREDICT_HORIZON

    hist = {c: get_daily_history(c, days=config.HIST_DAYS, adjust="qfq") for c in codes}
    for c, df in hist.items():
        df.name = c
    data, _, feature_names = build_dataset(list(hist.values()), horizon)
    if getattr(config, "FEATURE_SELECT", True):
        from app.features.select_features import apply_selection
        feature_names = apply_selection(data, feature_names)
    train_df, test_df, split_date = time_split(data)

    X_tr, y_tr = train_df[feature_names].values, train_df["label"].values
    model = LGBMClassifier(n_estimators=400, learning_rate=0.04,
                           max_depth=3, num_leaves=8, reg_lambda=1.0,
                           min_child_samples=50, subsample=0.8,
                           colsample_bytree=0.8, verbosity=-1, random_state=42)
    model.fit(X_tr, y_tr)

    # 训练段各类别平均未来收益(预期收益的定价基础)
    fwd_cache = {c: df["close"].shift(-horizon) / df["close"] - 1
                 for c, df in hist.items()}
    tmp = train_df.copy()
    tmp["_fwd"] = [fwd_cache[c].get(d, np.nan) for c, d in zip(tmp["code"], tmp.index)]
    class_avg = tmp.groupby("label")["_fwd"].mean()
    class_avg_returns = {int(k): round(float(v), 5) for k, v in class_avg.items()}

    # 测试段逐日排序
    proba = model.predict_proba(test_df[feature_names].values)
    pcols = {c: i for i, c in enumerate(model.classes_)}
    test_df = test_df.copy()
    test_df["_exp"] = sum(proba[:, pcols[k]] * v for k, v in class_avg_returns.items()
                          if k in pcols)

    dates = sorted(test_df.index.unique())
    rows = []
    for d in dates:
        sub = test_df[test_df.index == d]
        if len(sub) < top_n:
            continue
        ranked = sub.sort_values("_exp", ascending=False)
        top_codes = list(ranked["code"].head(top_n))
        bot_codes = list(ranked["code"].tail(top_n))
        top_fwd = np.mean([fwd_cache[c].get(d, np.nan) for c in top_codes])
        bot_fwd = np.mean([fwd_cache[c].get(d, np.nan) for c in bot_codes])
        pool_fwd = np.mean([fwd_cache[c].get(d, np.nan) for c in sub["code"]])
        rows.append({"date": d, "top_fwd": top_fwd, "bot_fwd": bot_fwd,
                     "pool_fwd": pool_fwd})
    df = pd.DataFrame(rows).set_index("date").dropna()

    summary = {
        "pool": codes,
        "split_date": str(pd.Timestamp(split_date).date()),
        "period": f"{df.index[0].date()} ~ {df.index[-1].date()}" if len(df) else "-",
        "days": len(df),
        "top_n": top_n,
        "top_fwd_mean": round(float(df["top_fwd"].mean()), 4),
        "bottom_fwd_mean": round(float(df["bot_fwd"].mean()), 4),
        "pool_fwd_mean": round(float(df["pool_fwd"].mean()), 4),
        "top_hit_rate": round(float((df["top_fwd"] > 0).mean()), 4),
        "top_win_vs_pool": round(float((df["top_fwd"] > df["pool_fwd"]).mean()), 4),
        "class_avg_returns": class_avg_returns,
        "daily": df,
    }
    return summary


if __name__ == "__main__":
    import sys
    mode = sys.argv[1] if len(sys.argv) > 1 else "today"
    if mode == "bt":
        s = rank_backtest()
        tn = s["top_n"]
        print(f"\n[排序回测] 池 {len(s['pool'])} 只,top_n={tn}, "
              f"切分 {s['split_date']}, 周期 {s['period']} ({s['days']} 交易日)")
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
