"""特征层:使用 VectorBT 快速计算技术指标特征。

输入 DataFrame(index=date, columns=open/high/low/close/volume/amount),
输出同索引、附加特征列的 DataFrame。所有特征仅依赖当期及历史数据,
保证预测时不引入未来信息(避免前视偏差)。
"""
import numpy as np
import pandas as pd
import vectorbt as vbt


def _s(x: pd.Series) -> pd.Series:
    """确保传入的是 Series(去掉因 DataFrame 产生的多余维度)。"""
    if isinstance(x, pd.DataFrame):
        return x.iloc[:, 0]
    return x


def compute_features(df: pd.DataFrame) -> pd.DataFrame:
    """基于单只股票日线计算特征矩阵。"""
    need = {"open", "high", "low", "close", "volume"}
    if not need.issubset(df.columns):
        raise ValueError(f"缺少必要列: {need - set(df.columns)}")
    df = df.copy().sort_index()

    o, h, l, c, v = (df["open"], df["high"], df["low"], df["close"], df["volume"])
    out = pd.DataFrame(index=df.index)

    # ------- 收益率(多周期动量)
    for n in (1, 2, 3, 5, 10, 20, 60):
        out[f"ret_{n}"] = c.pct_change(n)

    # ------- 均线系统
    ma5 = vbt.indicators.MA.run(c, window=5).ma
    ma10 = vbt.indicators.MA.run(c, window=10).ma
    ma20 = vbt.indicators.MA.run(c, window=20).ma
    ma60 = vbt.indicators.MA.run(c, window=60).ma
    ma120 = vbt.indicators.MA.run(c, window=120).ma
    out["ma5"] = _s(ma5)
    out["ma10"] = _s(ma10)
    out["ma20"] = _s(ma20)
    out["ma60"] = _s(ma60)
    out["ma120"] = _s(ma120)
    out["close_ma5"] = c / out["ma5"] - 1
    out["close_ma10"] = c / out["ma10"] - 1
    out["close_ma20"] = c / out["ma20"] - 1
    out["close_ma60"] = c / out["ma60"] - 1
    out["ma5_ma20"] = out["ma5"] / out["ma20"] - 1
    out["ma20_ma60"] = out["ma20"] / out["ma60"] - 1

    # ------- MACD
    macd = vbt.indicators.MACD.run(c, fast_window=12, slow_window=26, signal_window=9)
    out["macd"] = _s(macd.macd)
    out["macd_signal"] = _s(macd.signal)
    out["macd_hist"] = _s(macd.macd - macd.signal)
    out["macd_hist_norm"] = out["macd_hist"] / (out["macd"].abs() + 1e-9)

    # ------- RSI
    rsi = vbt.indicators.RSI.run(c, window=14)
    out["rsi14"] = _s(rsi.rsi)
    out["rsi6"] = _s(vbt.indicators.RSI.run(c, window=6).rsi)

    # ------- 布林带
    bb = vbt.indicators.BBANDS.run(c, window=20, alpha=2)
    out["bb_lower"] = _s(bb.lower)
    out["bb_middle"] = _s(bb.middle)
    out["bb_upper"] = _s(bb.upper)
    out["bb_position"] = (c - out["bb_lower"]) / (out["bb_upper"] - out["bb_lower"] + 1e-9)
    out["bb_width"] = (out["bb_upper"] - out["bb_lower"]) / out["bb_middle"]

    # ------- 波动率
    atr = vbt.indicators.ATR.run(h, l, c, window=14)
    out["atr14"] = _s(atr.atr)
    out["atr_pct"] = out["atr14"] / c
    out["vol20"] = c.pct_change().rolling(20).std() * np.sqrt(252)
    out["range_pct"] = (h - l) / c

    # ------- 量能
    out["volume"] = v
    out["vol_ma5"] = v.rolling(5).mean()
    out["vol_ma20"] = v.rolling(20).mean()
    out["volume_ratio"] = v / (v.rolling(5).mean() + 1e-9)
    out["volume_zscore"] = (v - v.rolling(20).mean()) / (v.rolling(20).std() + 1e-9)
    out["obv"] = vbt.indicators.OBV.run(c, v).obv

    # ------- 位置/形态
    roll_max20 = c.rolling(20).max()
    roll_min20 = c.rolling(20).min()
    out["close_pos20"] = (c - roll_min20) / (roll_max20 - roll_min20 + 1e-9)
    roll_max60 = c.rolling(60).max()
    roll_min60 = c.rolling(60).min()
    out["close_pos60"] = (c - roll_min60) / (roll_max60 - roll_min60 + 1e-9)
    out["gap_pct"] = o / c.shift(1) - 1
    out["upper_shadow"] = (h - np.maximum(o, c)) / c
    out["lower_shadow"] = (np.minimum(o, c) - l) / c
    out["body_pct"] = (c - o) / o

    # ------- 相对强弱(20 日收益 vs 波动率)
    out["cror"] = out["ret_20"] / (out["vol20"] + 1e-9)

    return out
