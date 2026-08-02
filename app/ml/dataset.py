"""数据集构建:由历史特征生成训练样本与标签。

标签定义(预测未来 horizon 个交易日的涨跌):
  forward_return = close.shift(-horizon) / close - 1
  -  >= +threshold  -> 2 (up)
  -  <= -threshold  -> 0 (down)
  -  其余            -> 1 (flat)
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
    y = pd.Series(np.where(fwd >= threshold, 2, np.where(fwd <= -threshold, 0, 1)),
                  index=close.index, dtype=int)
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
