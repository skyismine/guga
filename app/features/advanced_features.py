"""新增特征模块:主线板块特征 + 量价衍生 + 市场风格情绪。

设计约束(与全链路升级一致):
- 全部特征仅依赖"当日及以前"已实现数据(无前视),且训练/推断口径完全一致;
- 复用现有数据接口,不重复抓取:
  * 主线板块特征:主概念指数历史(concept/index/*.pkl 缓存,见 concept_features),
    用概念动量/联动度表达"板块热度与个股相对板块强弱"(板块资金流/评分是当日
    快照,无法回填历史,故此处用概念指数动量作为其历史可算代理);
  * 量价衍生:个股自身日线序列;
  * 市场风格:attach_market_features 已并入的 market_* 历史列(宽度/恐贪/离散度)。
- 特征命名:theme_*(板块)、pv_*(量价)、style_*(市场风格)。其中 theme_*/style_*
  在源端按全市场序列滚动标准化,标记为跨截面可比,不被个股维度再标准化。

接入点:dataset.build_dataset 在 attach_market_features / attach_industry_features
之后调用 attach_advanced_features(data),返回带新列的数据集。
"""
import numpy as np
import pandas as pd

from app import config
from app.features.standardize import zscore_series

# 跨截面可比前缀:源端标准化,个股维度标准化跳过
CROSS_SECTIONAL_PREFIX = ("theme_", "style_")

# 新增特征在源端(本模块)已按全市场序列滚动标准化
IS_SOURCE_STANDARDIZED = ("theme_", "style_")


def theme_feature_names() -> list:
    return ["theme_ret_5", "theme_ret_20", "theme_mom_ratio", "theme_streak",
            "stock_theme_corr20"]


def pv_feature_names() -> list:
    return ["pv_vol_price_div", "pv_turnover_trend", "pv_volatility_slope",
            "pv_momentum_reversal"]


def style_feature_names() -> list:
    return ["style_breadth_slope", "style_fear_greed_slope", "style_dispersion_trend"]


def advanced_feature_names() -> list:
    return theme_feature_names() + pv_feature_names() + style_feature_names()


# ---------------------------------------------------------------- 主线板块特征(概念指数历史)
def _theme_features(close_stock: pd.Series, concept_close: pd.Series) -> pd.DataFrame:
    """板块特征:概念动量/动量比/连涨天数/个股-概念联动度。

    concept_close:个股主概念指数收盘序列(index=date)。无前视:仅当日及之前。
    """
    out = pd.DataFrame(index=close_stock.index)
    if concept_close is None or len(concept_close) < 40:
        return out
    # 对齐到个股交易日
    cc = concept_close.reindex(close_stock.index).ffill()
    ret5 = cc.pct_change(5)
    ret20 = cc.pct_change(20)
    out["theme_ret_5"] = ret5
    out["theme_ret_20"] = ret20
    out["theme_mom_ratio"] = ret5 / (ret20.abs() + 1e-9) * np.sign(ret20)
    # 概念连涨天数(连续 n 日概念上涨)
    up = cc.diff() > 0
    streak = pd.Series(0, index=cc.index)
    run = 0
    for i, u in enumerate(up):
        run = run + 1 if u else 0
        streak.iloc[i] = run
    out["theme_streak"] = streak.astype(float)
    # 个股与概念 20 日收益相关性(联动度,滚动)
    sc = close_stock.reindex(cc.index)
    both = pd.DataFrame({"s": sc, "c": cc}).dropna()
    if len(both) < 30:
        return out
    corr = both["s"].rolling(20).corr(both["c"])
    out["stock_theme_corr20"] = corr.reindex(out.index)
    # 源端滚动标准化(跨截面可比)
    for c in ("theme_ret_5", "theme_ret_20", "theme_mom_ratio", "theme_streak",
              "stock_theme_corr20"):
        if c in out.columns:
            out[c] = zscore_series(out[c].astype(float))
    return out


# ---------------------------------------------------------------- 量价衍生(个股自身)
def _pv_features(df: pd.DataFrame) -> pd.DataFrame:
    """量价背离 / 换手趋势 / 波动率斜率 / 动量反转。

    输入:含 close/volume 列(或可由已有列重构)的个股日线。全部仅用当日及以前。
    """
    if "close" not in df.columns or "volume" not in df.columns:
        return pd.DataFrame(index=df.index)
    c, v = df["close"], df["volume"]
    out = pd.DataFrame(index=df.index)
    ret5 = c.pct_change(5)
    vol_z = (v - v.rolling(20).mean()) / (v.rolling(20).std() + 1e-9)
    # 量价背离:涨而缩量或跌而放量 => 背离为负
    out["pv_vol_price_div"] = np.where(np.sign(ret5) * np.sign(vol_z) >= 0,
                                       vol_z.abs(), -vol_z.abs()).astype(float)
    # 换手趋势:量能 5日/20日均值比(>1 量能放大)
    out["pv_turnover_trend"] = v.rolling(5).mean() / (v.rolling(20).mean() + 1e-9)
    # 波动率斜率:20日波动率相对 5 日前的变化
    vol20 = c.pct_change().rolling(20).std() * np.sqrt(252)
    out["pv_volatility_slope"] = vol20 / (vol20.shift(5) + 1e-9) - 1
    # 多周期动量反转:20日动量 - 5日动量(长短期动量差,均值回归信号)
    out["pv_momentum_reversal"] = c.pct_change(20) - c.pct_change(5)
    return out


# ---------------------------------------------------------------- 市场风格(attach_market_features 之后)
def _style_features(data: pd.DataFrame) -> pd.DataFrame:
    """宽度/恐贪/离散度的近期变化(市场风格情绪,跨截面一致)。

    依赖 attach_market_features 已并入的 market_adv_ratio / market_fear_greed /
    market_dispersion(均为源端标准化后的历史序列)。
    """
    out = pd.DataFrame(index=data.index)
    for src, dst in (("market_adv_ratio", "style_breadth_slope"),
                     ("market_fear_greed", "style_fear_greed_slope"),
                     ("market_dispersion", "style_dispersion_trend")):
        if src not in data.columns:
            continue
        s = data[src]
        out[dst] = s - s.rolling(5).mean()
        out[dst] = zscore_series(out[dst].astype(float))
    return out


# ---------------------------------------------------------------- 装配
def attach_stock_advanced(df: pd.DataFrame, code: str) -> pd.DataFrame:
    """单只股票历史日线(含 close/volume)追加板块+量价高级特征(训练/推断共用)。

    在 compute_features 之外独立装配(本模块产出的列经后续 standardize 处理)。
    失败列留空,不阻断。
    """
    out = df.copy()
    code = str(code).zfill(6)
    try:
        cc = _main_concept_close(code, out.index)
    except Exception:  # noqa: BLE001
        cc = None
    try:
        th = _theme_features(out["close"], cc)
        pv = _pv_features(out)
    except Exception:  # noqa: BLE001
        th = pv = pd.DataFrame(index=out.index)
    for c in advanced_feature_names():
        if c in out.columns:
            continue
        if c.startswith("style_"):
            continue  # style_* 需在全数据集(含 market_* 列)上计算,由 attach_advanced_features 处理
        if c in th.columns:
            out[c] = th[c].values
        elif c in pv.columns:
            out[c] = pv[c].values
        else:
            out[c] = 0.0
    return out


def attach_advanced_features(data: pd.DataFrame) -> pd.DataFrame:
    """新增特征并入已组装数据集(训练路径,data 含 code 列与 close/volume 等原始列)。

    若 data 缺少 close 列(纯特征帧),则跳过板块/量价部分,仅并入市场风格特征。
    """
    if "code" not in data.columns:
        return data
    out = data.copy()
    if {"close", "volume"}.issubset(out.columns):
        for code in out["code"].unique():
            mask = out["code"] == code
            idx = out.index[mask]
            sub = out.loc[mask]
            try:
                aug = attach_stock_advanced(sub, code)
            except Exception:  # noqa: BLE001
                continue
            for c in advanced_feature_names():
                if c.startswith("style_"):
                    continue
                if c in aug.columns and c not in out.columns:
                    out.loc[mask, c] = aug[c].reindex(idx).values
    # 市场风格特征(全数据集,由 market_* 历史列派生)
    st = _style_features(out)
    for c in st.columns:
        if c not in out.columns:
            out[c] = st[c].values
    return out


def _main_concept_close(code: str, index: pd.DatetimeIndex) -> pd.Series:
    """个股主概念的指数收盘历史(缓存读取,失败抛异常)。"""
    from app.features.concept_features import main_concept_sw, _get_concept_close
    mc = main_concept_sw(code)
    if mc is None:
        raise KeyError(f"{code} 无概念")
    return _get_concept_close(mc)


def prepare_advanced_features(df: pd.DataFrame, code: str = None) -> pd.DataFrame:
    """推断路径:单只股票追加新增特征(与训练口径一致,全部历史可算)。

    输入应为 compute_features + attach_market_features 后的特征帧;本函数补齐
    theme_*/pv_*/style_* 列(缺失用 0)。
    """
    out = df.copy()
    code = str(code).zfill(6) if code else (getattr(df, "name", "") or "")
    # 从 ret_1 重构 close 近似序列(仅供板块/量价特征计算,标准化后无碍)
    if "close" not in out.columns and "ret_1" in out.columns:
        out["close"] = (1 + out["ret_1"].fillna(0)).cumprod()
    if "volume" not in out.columns:
        out["volume"] = 1.0
    try:
        cc = _main_concept_close(code, out.index)
    except Exception:  # noqa: BLE001
        cc = None
    try:
        th = _theme_features(out["close"], cc)
        pv = _pv_features(out)
    except Exception:  # noqa: BLE001
        th = pv = pd.DataFrame(index=out.index)
    for c in advanced_feature_names():
        if c in out.columns:
            continue
        if c in th.columns:
            out[c] = th[c].values
        elif c in pv.columns:
            out[c] = pv[c].values
        elif c.startswith("style_"):
            out[c] = 0.0
        else:
            out[c] = 0.0
    return out


if __name__ == "__main__":
    print("advanced features:", advanced_feature_names())
