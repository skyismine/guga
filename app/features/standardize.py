"""滚动 z-score 标准化:用历史窗口均值/标准差(仅当日及以前,避免未来数据)。

设计原则:
- 每个特征按"其原始序列"滚动标准化(仅用 t 及之前数据):
  个股特征按个股序列、市场特征按市场帧、行业特征按行业指数,保证训练/推断口径一致,
  且市场/行业特征在横截面上可比(同一天所有股票取值一致);
- market_*/ind_* 在数据源端(attach_market_features / _industry_frame_for)已标准化;
  个股特征与 alpha_* 在装配末尾按股票(standardize_dataset / standardize_stock_frame)标准化;
- 常数列(窗口内 std=0)按 std=1 处理(z=偏离均值,即 0),避免除零。
"""
import numpy as np
import pandas as pd

from app import config

# 已在源端标准化的前缀,个股维度标准化时跳过
# market_*/ind_*:数据源端(市场/行业)标准化;theme_*/style_*:advanced_features
# 按全市场序列标准化,跨截面可比,不做个股维度二次标准化。
SKIP_PREFIX = ("market_", "ind_", "theme_", "style_")


def is_per_stock(col: str) -> bool:
    return not col.startswith(SKIP_PREFIX)


def zscore_series(s: pd.Series, window: int = None, min_periods: int = None) -> pd.Series:
    window = window or config.STANDARDIZE_WINDOW
    min_periods = min_periods or config.STANDARDIZE_MIN_PERIODS
    mean = s.rolling(window, min_periods=min_periods).mean()
    std = s.rolling(window, min_periods=min_periods).std().replace(0, 1.0)
    return (s - mean) / std


def zscore_frame(df: pd.DataFrame, window: int = None, min_periods: int = None) -> pd.DataFrame:
    out = df.copy()
    for c in df.columns:
        out[c] = zscore_series(df[c], window, min_periods)
    return out


def standardize_dataset(data: pd.DataFrame, cols=None) -> pd.DataFrame:
    """训练/回测:按 code 分组滚动 z-score(跳过 market_/ind_,它们在源端已标准化)。"""
    if not config.STANDARDIZE_ROLLING or "code" not in data.columns:
        return data
    cols = [c for c in (cols or list(data.columns))
            if c not in ("label", "code") and is_per_stock(c)]
    if not cols:
        return data
    out = data.copy()
    for code in out["code"].unique():
        mask = out["code"] == code
        for c in cols:
            out.loc[mask, c] = zscore_series(out.loc[mask, c]).values
    return out


def standardize_stock_frame(features: pd.DataFrame) -> pd.DataFrame:
    """单只股票推断:滚动 z-score 除 market_/ind_ 外的列。"""
    if not config.STANDARDIZE_ROLLING:
        return features
    cols = [c for c in features.columns if is_per_stock(c)]
    if not cols:
        return features
    out = features.copy()
    for c in cols:
        out[c] = zscore_series(features[c]).values
    return out
