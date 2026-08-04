"""数据集构建:由历史特征生成训练样本与标签。

标签定义(预测未来 horizon 个交易日的涨跌):
  forward_return = close.shift(-horizon) / close - 1
  -  >= +threshold  -> 2 (up)
  -  <= -threshold  -> 0 (down)
  -  其余            -> 1 (flat)

阈值支持两种模式:
  1. 滚动分位数(默认):对每只个股,日期 t 的涨/跌阈值取"过去 window 个交易日已实现
     的 forward_return"的 30%/70% 分位数。窗口整体 shift(horizon),保证阈值只依赖
     t 时点已实现的数据(无前视)。自动适配个股波动率与市场环境,三类样本比例天然
     稳定在约 30/40/30,从根源上解决固定百分比造成的阈值漂移。
  2. 固定百分比(config.PREDICT_THRESHOLD):仅当 LABEL_QUANTILE_WINDOW<=0 时回退。
"""
from typing import List, Tuple

import numpy as np
import pandas as pd

from app import config
from app.features.indicators import compute_features
from app.features.market_features import attach_market_features, build_market_frame

LABEL_NAME = {0: "down", 1: "flat", 2: "up"}
LABEL_NAME_CN = {0: "下跌", 1: "震荡", 2: "上涨"}


def build_labels(close: pd.Series, horizon: int = None, threshold: float = None) -> pd.Series:
    horizon = horizon or config.PREDICT_HORIZON
    threshold = threshold or config.PREDICT_THRESHOLD
    fwd = close.shift(-horizon) / close - 1

    window = getattr(config, "LABEL_QUANTILE_WINDOW", 0)
    if window and window > 0:
        q_lo = getattr(config, "LABEL_QUANTILE_LOW", 0.30)
        q_hi = getattr(config, "LABEL_QUANTILE_HIGH", 0.70)
        min_periods = getattr(config, "LABEL_QUANTILE_MIN_PERIODS", 60)
        # 无前视: t 时刻阈值只用 s+horizon<=t 的已实现收益(即 fwd 整体 shift(horizon))
        thr_lo = fwd.shift(horizon).rolling(window, min_periods=min_periods).quantile(q_lo)
        thr_hi = fwd.shift(horizon).rolling(window, min_periods=min_periods).quantile(q_hi)
        y = pd.Series(np.where(fwd >= thr_hi, 2, np.where(fwd <= thr_lo, 0, 1)),
                      index=close.index, dtype=float)
        # 阈值窗口不足或 forward_return 缺失的日期统一置为 NaN(由上层 dropna 剔除)
        y = y.mask(fwd.isna() | thr_lo.isna() | thr_hi.isna())
    else:
        y = pd.Series(np.where(fwd >= threshold, 2, np.where(fwd <= -threshold, 0, 1)),
                      index=close.index, dtype=float)
        y = y.mask(fwd.isna())
    return y


def _extract_xy(df: pd.DataFrame, horizon: int, threshold: float, code: str):
    features = compute_features(df)
    y = build_labels(df["close"], horizon, threshold)
    data = pd.concat([features, y.to_frame("label")], axis=1)
    # 删除 warmup 期(NaN)与末尾无法标注的行,保证标签严格对应未来
    data = data.dropna()
    data["code"] = code
    return data


def build_dataset(df_list: List[pd.DataFrame], horizon: int = None,
                  threshold: float = None) -> Tuple[pd.DataFrame, pd.Series, List[str]]:
    """合并多只股票历史数据为样本集。

    返回 (X_df, y_series, feature_names), X_df 额外带 code/date 元信息列。
    """
    horizon = horizon or config.PREDICT_HORIZON
    threshold = threshold or config.PREDICT_THRESHOLD

    frames = []
    for i, df in enumerate(df_list):
        try:
            code = getattr(df, "name", str(i))
            d = _extract_xy(df, horizon, threshold, code)
            if len(d) > config.MIN_HIST_DAYS // 3:
                frames.append(d)
        except Exception as e:  # noqa: BLE001
            print(f"[dataset] 处理 {code} 失败: {e}")

    if not frames:
        raise RuntimeError("没有任何可用样本")

    data = pd.concat(frames).sort_index()
    # 并入市场级特征(涨跌家数/恐贪/期指基差),按日期对齐
    data = attach_market_features(data)
    mcols = [c for c in data.columns if c.startswith("market_")]
    data = data.dropna(subset=mcols)
    # 特征列 = 除 label/code 外的全部指标列
    feature_names = [c for c in data.columns if c not in ("label", "code")]
    X = data[feature_names]
    y = data["label"]
    return data, y, feature_names


def time_split(data: pd.DataFrame, test_ratio: float = None):
    """按时间切分:前段训练,后段验证(保持时间顺序,避免随机切分泄漏)。"""
    test_ratio = test_ratio or config.TEST_RATIO
    dates = data.index.sort_values()
    split_idx = int(len(dates) * (1 - test_ratio))
    split_date = dates[split_idx]
    train = data[data.index < split_date]
    test = data[data.index >= split_date]
    return train, test, split_date
