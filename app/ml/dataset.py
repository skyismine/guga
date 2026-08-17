"""数据集构建:由历史特征生成训练样本与标签。

标签定义(预测未来 horizon 个交易日的涨跌):
  forward_return = close.shift(-horizon) / close - 1
  -  >= +threshold  -> 2 (up)
  -  <= -threshold  -> 0 (down)
  -  其余            -> 1 (flat)

阈值支持两种模式(config.LABEL_MODE):
  1. quantile(默认):对每只个股,日期 t 的涨/跌阈值取"过去 window 个交易日已实现
     的 forward_return"的 30%/70% 分位数。窗口整体 shift(horizon),保证阈值只依赖
     t 时点已实现的数据(无前视)。自动适配个股波动率与市场环境,三类样本比例天然
     稳定在约 30/40/30,从根源上解决固定百分比造成的阈值漂移。
  2. atr:固定收益阈值 + 个股 ATR 动态调整。阈值 = max(FIXED, k_atr * atr_pct),
     用个股波动率个性化涨跌判定(高波动股阈值放宽,低波动股收紧)。

不可交易样本剔除(config.DROP_LIMIT_DAYS / DROP_HALT_DAYS):
  - 标签日处于涨/跌停封板的样本剔除(当日无法成交);
  - 未来 horizon 内含停牌段(交易间隔 > MAX_HALT_GAP_DAYS)的样本剔除。
"""
from typing import List, Tuple

import numpy as np
import pandas as pd

from app import config
from app.features.indicators import compute_features
from app.features.market_features import attach_market_features, build_market_frame
from app.features.industry_features import attach_industry_features

LABEL_NAME = {0: "down", 1: "flat", 2: "up"}
LABEL_NAME_CN = {0: "下跌", 1: "震荡", 2: "上涨"}


def _limit_mask(df: pd.DataFrame) -> pd.Series:
    """涨跌停封板掩码:当日收盘触及涨跌停价格视为不可成交。

    以昨收为基准,按板块涨跌幅限制估算涨停/跌停价(主板10%,创业板/科创板20%)。
    封板(收盘价 == 涨停/跌停价)无法成交,返回 True 表示"应剔除"。
    """
    close = df["close"]
    prev = close.shift(1)
    code6 = str(getattr(df, "name", ""))
    # 创业板 300/301 与科创板 688/689 涨跌幅 20%,其余 10%
    limit = 0.20 if code6.startswith(("300", "301", "688", "689")) else 0.10
    up = prev * (1 + limit)
    dn = prev * (1 - limit)
    at_limit = (close >= up * 0.9995) | (close <= dn * 1.0005)
    # 首行(无昨收)不参与
    at_limit.iloc[0] = False
    return at_limit


def _halt_mask(df: pd.DataFrame, horizon: int) -> pd.Series:
    """未来停牌掩码:未来 horizon 个交易日内存在停牌段(间隔 > 阈值)则剔除。

    日线序列若某日与下一交易日自然日间隔过大(> MAX_HALT_GAP_DAYS),视为停牌;
    该停牌发生时刻前的 horizon 窗口内样本收益计算失真,应剔除。
    """
    if not hasattr(config, "MAX_HALT_GAP_DAYS"):
        return pd.Series(False, index=df.index)
    idx = pd.DatetimeIndex(df.index)
    gaps = idx.to_series().diff().dt.days
    halt_bad = pd.Series(False, index=df.index)
    bad_days = set(idx[gaps > config.MAX_HALT_GAP_DAYS].strftime("%Y-%m-%d"))
    for i, ts in enumerate(idx):
        fwd = idx[i + 1:i + 1 + horizon]
        if any(d.strftime("%Y-%m-%d") in bad_days for d in fwd):
            halt_bad.iloc[i] = True
    return halt_bad


def build_labels(df, horizon: int = None,
                 threshold: float = None) -> pd.Series:
    """基于个股日线构建三分类标签(自动应用不可交易样本剔除)。

    df 兼容两种输入:DataFrame(个股日线,含 close)或 Series(close 序列,
    旧调用方兼容;此时不做涨跌停/停牌剔除)。
    """
    horizon = horizon or config.PREDICT_HORIZON
    if isinstance(df, pd.Series):
        close = df
        drop_limits = drop_halts = False
    else:
        close = df["close"]
        drop_limits = getattr(config, "DROP_LIMIT_DAYS", True)
        drop_halts = getattr(config, "DROP_HALT_DAYS", True)
    fwd = close.shift(-horizon) / close - 1

    window = getattr(config, "LABEL_QUANTILE_WINDOW", 0)
    mode = getattr(config, "LABEL_MODE", "quantile")
    if mode == "atr":
        # ATR 动态阈值:阈值 = max(FIXED, k_atr * atr_pct)
        fixed = float(getattr(config, "LABEL_ATR_THRESHOLD", threshold or 0.015))
        k = float(getattr(config, "LABEL_ATR_K", 1.0))
        atr_pct = None
        if isinstance(df, pd.DataFrame):
            atr_pct = df.get("atr_pct")
        if atr_pct is None:
            import vectorbt as vbt
            if isinstance(df, pd.DataFrame):
                h, l = df["high"], df["low"]
            else:  # Series 输入无 high/low,用 close 近似
                h = l = close
            atr_pct = vbt.indicators.ATR.run(h, l, close, window=14).atr / close
        thr = pd.concat([pd.Series(fixed, index=close.index), k * atr_pct], axis=1).max(axis=1)
        y = pd.Series(np.where(fwd >= thr, 2, np.where(fwd <= -thr, 0, 1)),
                      index=close.index, dtype=float)
        y = y.mask(fwd.isna() | thr.isna())
    elif window and window > 0:
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
        threshold = threshold or config.PREDICT_THRESHOLD
        y = pd.Series(np.where(fwd >= threshold, 2, np.where(fwd <= -threshold, 0, 1)),
                      index=close.index, dtype=float)
        y = y.mask(fwd.isna())

    # 不可交易样本剔除(仅 DataFrame 输入可用)
    if drop_limits:
        y = y.mask(_limit_mask(df))
    if drop_halts:
        y = y.mask(_halt_mask(df, horizon))
    return y


def _extract_xy(df: pd.DataFrame, horizon: int, threshold: float, code: str):
    features = compute_features(df)
    # 新增高级特征:板块动量/量价衍生/市场风格(历史可算,无前视)
    if getattr(config, "ADVANCED_FEATURES_ENABLED", True):
        from app.features.advanced_features import attach_stock_advanced, advanced_feature_names
        aug = attach_stock_advanced(df, code)
        for c in advanced_feature_names():
            if c.startswith("style_"):
                continue  # style_* 依赖 market_* 列,在全数据集装配阶段统一计算
            if c not in features.columns:
                features[c] = aug[c].reindex(features.index).values
    y = build_labels(df, horizon, threshold)
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
    # 并入行业特征(行业指数涨跌 + 相对行业超额收益),按 (code,date) 对齐
    data = attach_industry_features(data)
    # 并入新增高级特征的市场风格列(theme_/pv_ 已在个股层装配;style_ 依赖 market_*)
    if getattr(config, "ADVANCED_FEATURES_ENABLED", True):
        from app.features.advanced_features import attach_advanced_features
        data = attach_advanced_features(data)
    mcols = [c for c in data.columns if c.startswith(("market_", "ind_", "alpha_"))]
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
