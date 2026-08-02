"""VectorBT 回测:用 ML 预测概率作为交易信号,验证策略有效性。

规则:收盘后得到当日预测,次日开盘执行(T+1,信号滞后一天)。
- 买入:p_up >= buy_threshold 且未持仓
- 卖出:p_down >= sell_threshold 或触及止损
"""
from typing import Dict, List, Optional

import numpy as np
import pandas as pd
import vectorbt as vbt

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from app import config
from app.data.fetcher import get_daily_history, get_stock_name
from app.features.indicators import compute_features
from app.features.market_features import attach_market_features
from app.ml.predictor import Predictor

_METRIC_KEYS = {
    "total_return": "Total Return [%]",
    "benchmark_return": "Benchmark Return [%]",
    "max_drawdown": "Max Drawdown [%]",
    "sharpe": "Sharpe Ratio",
    "win_rate": "Win Rate [%]",
    "trades": "Total Trades",
    "profit_factor": "Profit Factor",
    "expectancy": "Expectancy",
    "fees": "Total Fees Paid",
}


def build_signals(df: pd.DataFrame, predictor: Predictor,
                  buy_threshold: float = None, sell_threshold: float = None):
    """由历史特征与模型生成 (entries, exits) 布尔序列。"""
    buy_threshold = buy_threshold or config.BUY_P_UP
    sell_threshold = sell_threshold or config.SELL_P_DOWN
    features = attach_market_features(compute_features(df))
    proba = predictor.predict_proba(features)
    proba = proba.dropna()

    entries = (proba["up"] >= buy_threshold) & (proba["up"] > proba["down"])
    exits = (proba["down"] >= sell_threshold) & (proba["down"] > proba["up"])

    # 次日执行,避免前视
    entries = entries.shift(1).fillna(False)
    exits = exits.shift(1).fillna(False)
    return entries, exits


def backtest_stock(code: str, predictor: Optional[Predictor] = None,
                   buy_threshold: float = None, sell_threshold: float = None,
                   init_cash: float = 100_000) -> Dict:
    """单只股票回测。"""
    code = str(code).zfill(6)
    predictor = predictor or Predictor()
    df = get_daily_history(code, days=config.HIST_DAYS, adjust="qfq")
    entries, exits = build_signals(df, predictor, buy_threshold, sell_threshold)

    close = df["close"].reindex(entries.index)
    pf = vbt.Portfolio.from_signals(
        close.values, entries.values.astype(bool), exits.values.astype(bool),
        size=20_000, size_type="value",
        fees=config.COMMISSION + config.SLIPPAGE,
        slippage=0.0,
        init_cash=init_cash,
        freq="D",
    )
    stats = pf.stats()
    metrics = {"code": code, "name": get_stock_name(code)}
    for k, v in _METRIC_KEYS.items():
        try:
            metrics[k] = float(stats[v]) if k != "trades" else int(stats[v])
        except (TypeError, ValueError, KeyError):
            metrics[k] = None

    # 买卖点明细
    orders = pf.orders.records_readable
    trades = []
    if orders is not None and len(orders) > 0:
        for _, row in orders.iterrows():
            bar = int(row["Timestamp"])
            trades.append({
                "date": str(pd.Timestamp(close.index[bar]).date()),
                "side": "买" if row["Side"] == "Buy" else "卖",
                "price": round(float(row["Price"]), 2),
                "size": int(row["Size"]),
                "fees": round(float(row["Fees"]), 2),
            })
    metrics["trades_detail"] = trades
    return metrics


def backtest_universe(codes: Optional[List[str]] = None,
                      predictor: Optional[Predictor] = None) -> pd.DataFrame:
    """多股票回测汇总。"""
    codes = codes or config.TRAIN_STOCK_CODES
    predictor = predictor or Predictor()
    rows = []
    for code in codes:
        try:
            rows.append(backtest_stock(code, predictor))
        except Exception as e:  # noqa: BLE001
            print(f"  [回测] {code} 失败: {e}")
    df = pd.DataFrame(rows)
    return df


def backtest_oos(codes: Optional[List[str]] = None, train_ratio: float = 0.7,
                 horizon: int = None, threshold: float = None,
                 buy_threshold: float = None, sell_threshold: float = None,
                 init_cash: float = 100_000) -> pd.DataFrame:
    """样本外回测:只用每只股票历史前 train_ratio 部分训练模型,
    在后 1-train_ratio 的测试期内用该模型生成信号并回测(无前视)。
    """
    from lightgbm import LGBMClassifier
    from app.ml.dataset import build_dataset, build_labels

    codes = codes or config.TRAIN_STOCK_CODES
    horizon = horizon or config.PREDICT_HORIZON
    threshold = threshold or config.PREDICT_THRESHOLD
    buy_threshold = buy_threshold or config.BUY_P_UP
    sell_threshold = sell_threshold or config.SELL_P_DOWN

    # 1) 组织数据
    trains, tests, all_features = {}, {}, {}
    for code in codes:
        df = get_daily_history(code, days=config.HIST_DAYS, adjust="qfq")
        features = attach_market_features(compute_features(df))
        split_i = int(len(features) * train_ratio)
        trains[code] = features.iloc[:split_i]
        tests[code] = features.iloc[split_i:]
        all_features[code] = features

    # 2) 训练(仅训练段)
    X_tr_all, y_tr_all, feat_names = [], [], None
    for code, f in trains.items():
        df = get_daily_history(code, days=config.HIST_DAYS, adjust="qfq")
        y = build_labels(df["close"], horizon, threshold).reindex(f.index).dropna()
        X = f.reindex(y.index)
        X_tr_all.append(X.values)
        y_tr_all.append(y.values)
        feat_names = list(X.columns)
    X_tr_all = np.concatenate(X_tr_all)
    y_tr_all = np.concatenate(y_tr_all)
    model = LGBMClassifier(n_estimators=400, learning_rate=0.04,
                           max_depth=3, num_leaves=8, reg_lambda=1.0,
                           min_child_samples=50, subsample=0.8,
                           colsample_bytree=0.8, verbosity=-1, random_state=42)
    model.fit(X_tr_all, y_tr_all)

    # 3) 测试段信号 + 回测
    rows = []
    for code in codes:
        f = tests[code]
        X = f[feat_names].values
        proba = model.predict_proba(X)
        classes = list(model.classes_)
        idx_up = classes.index(2)
        idx_down = classes.index(0)
        up = pd.Series(proba[:, idx_up], index=f.index)
        down = pd.Series(proba[:, idx_down], index=f.index)

        entries = (up >= buy_threshold) & (up > down)
        exits = (down >= sell_threshold) & (down > up)
        entries = entries.shift(1).fillna(False)
        exits = exits.shift(1).fillna(False)

        df = get_daily_history(code, days=config.HIST_DAYS, adjust="qfq")
        close = df["close"].reindex(f.index)
        pf = vbt.Portfolio.from_signals(close.values, entries.values.astype(bool),
                                        exits.values.astype(bool), size=20_000,
                                        size_type="value",
                                        fees=config.COMMISSION + config.SLIPPAGE,
                                        init_cash=init_cash, freq="D")
        stats = pf.stats()
        m = {"code": code, "name": get_stock_name(code),
             "start": str(f.index[0].date()), "end": str(f.index[-1].date())}
        for k, v in _METRIC_KEYS.items():
            try:
                m[k] = float(stats[v]) if k != "trades" else int(stats[v])
            except (TypeError, ValueError, KeyError):
                m[k] = None
        rows.append(m)
    return pd.DataFrame(rows)


if __name__ == "__main__":
    print("========== 全样本回测(演示,存在前视) ==========")
    res = backtest_universe()
    cols = ["code", "name", "total_return", "sharpe", "max_drawdown", "win_rate", "trades"]
    print(res[cols].to_string(index=False))
    print("\n平均:")
    print(res[["total_return", "sharpe", "max_drawdown", "win_rate"]].mean().round(2).to_string())

    print("\n========== 样本外回测(仅用前70%训练,后30%验证) ==========")
    try:
        oos = backtest_oos()
        print(oos[["code", "name", "start", "end", "total_return", "sharpe",
                   "max_drawdown", "win_rate", "trades"]].to_string(index=False))
        print("\n平均:")
        print(oos[["total_return", "sharpe", "max_drawdown", "win_rate"]].mean().round(2).to_string())
    except Exception as e:  # noqa: BLE001
        print(f"样本外回测失败: {e}")
