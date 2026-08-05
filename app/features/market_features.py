"""市场级特征:涨跌家数宽度、恐贪合成指标、期指基差,并入个股特征矩阵。

设计原则:
- 全部为"当日及历史"信息,按日期对齐,不引入未来数据(避免前视)。
- 训练与推断共用同一构建函数,保证特征一致。
- 市场帧整体本地缓存(TTL),推断时开销极小。

新增特征(market_* 前缀):
- 涨跌家数/宽度: market_adv_ratio(上涨占比) / market_above_ma20(站上20日线占比)
  / market_avg_ret_1 / market_dispersion(截面离散度) / market_hot_ratio(强势股占比)
  / market_median_vol_ratio
- 指数层面: market_index_rsi14 / market_index_ret_5 / market_index_ret_20
  / market_index_vol20 / market_index_vol_ratio
- 期指资金: market_basis_if / market_basis_ih / market_basis_ic / market_basis_im / market_basis_avg
- 恐贪合成: market_fear_greed(0~100, 低=恐慌 高=贪婪)
"""
import os
import pickle
import time

import numpy as np
import pandas as pd

from app import config
from app.data.fetcher import get_daily_history
from app.data import market as mk
from app.features.indicators import compute_features
from app.features.standardize import zscore_frame

_MARKET_FRAME_PATH = os.path.join(config.DATA_DIR, "market_frame.pkl")


def _clip01(s: pd.Series) -> pd.Series:
    return np.clip(s, 0.0, 1.0)


def _fear_greed(frame: pd.DataFrame) -> pd.Series:
    """合成恐贪指标(0~100):宽度 + 强度 + 动量 + RSI + 基差 + 波动率 六分项平均。"""
    adv = _clip01((frame["market_adv_ratio"] - 0.25) / 0.50)          # 上涨家数占比
    strength = _clip01(frame["market_above_ma20"] / 0.75)             # 站上MA20占比
    momentum = _clip01((frame["market_index_ret_20"] + 0.10) / 0.20)  # 20日动量
    rsi = _clip01(frame["market_index_rsi14"] / 100.0)                # 指数RSI
    basis = _clip01((frame["market_basis_avg"] + 0.015) / 0.030)      # 期指基差(升水=贪)
    vol = _clip01((0.45 - frame["market_index_vol20"]) / 0.35)        # 低波动=贪
    return ((adv + strength + momentum + rsi + basis + vol) / 6.0) * 100.0


def _breadth_frame(days: int) -> pd.DataFrame:
    """从样本篮子(与训练股票一致)逐日统计涨跌家数/宽度特征。"""
    rows = []
    for code in config.MARKET_BASKET:
        try:
            df = get_daily_history(code, days=days, adjust="qfq")
            f = compute_features(df)
            rows.append(pd.DataFrame({
                "ret_1": f["ret_1"], "close_ma20": f["close_ma20"],
                "volume_ratio": f["volume_ratio"],
            }))
        except Exception as e:  # noqa: BLE001
            print(f"[market_features] 篮子 {code} 失败: {e}")
    if not rows:
        raise RuntimeError("市场宽度篮子无数据")
    basket = pd.concat(rows)
    g = basket.groupby(basket.index)
    return pd.DataFrame({
        "market_adv_ratio": g["ret_1"].apply(lambda x: float((x > 0).mean())),
        "market_above_ma20": g["close_ma20"].apply(lambda x: float((x > 0).mean())),
        "market_avg_ret_1": g["ret_1"].mean(),
        "market_dispersion": g["ret_1"].std(),
        "market_hot_ratio": g["ret_1"].apply(lambda x: float((x > 0.05).mean())),
        "market_median_vol_ratio": g["volume_ratio"].median(),
    })


def _index_frame(days: int) -> pd.DataFrame:
    idx = mk.get_index_history(config.MARKET_INDEX, days=days)
    f = compute_features(idx)
    return pd.DataFrame({
        "market_index_rsi14": f["rsi14"],
        "market_index_ret_5": f["ret_5"],
        "market_index_ret_20": f["ret_20"],
        "market_index_vol20": f["vol20"],
        "market_index_vol_ratio": f["volume_ratio"],
    })


def _basis_frame(days: int) -> pd.DataFrame:
    b = mk.get_basis_series(days=days)
    return b.rename(columns={c: f"market_{c}" for c in b.columns})


def build_market_frame(days: int = None, use_cache: bool = True) -> pd.DataFrame:
    """构建按日期索引的市场特征帧,本地缓存。"""
    days = days or config.HIST_DAYS
    if use_cache and os.path.exists(_MARKET_FRAME_PATH):
        if time.time() - os.path.getmtime(_MARKET_FRAME_PATH) <= config.CACHE_TTL_SECONDS:
            try:
                with open(_MARKET_FRAME_PATH, "rb") as f:
                    return pickle.load(f)
            except Exception:
                pass

    frame = _breadth_frame(days).join(_index_frame(days), how="outer") \
        .join(_basis_frame(days), how="outer").sort_index()
    frame = frame.ffill()
    frame = frame.dropna(subset=[c for c in frame.columns if not c.endswith(("dispersion",))])
    frame["market_fear_greed"] = _fear_greed(frame)
    frame = frame.dropna(subset=["market_fear_greed"])

    with open(_MARKET_FRAME_PATH, "wb") as f:
        pickle.dump(frame, f)
    return frame


def market_feature_names() -> list:
    return [c for c in build_market_frame().columns if c.startswith("market_")]


def attach_market_features(features: pd.DataFrame, frame: pd.DataFrame = None) -> pd.DataFrame:
    """把市场特征按日期对齐到个股特征帧(ffill 缺失日期)。

    注意:个股帧的日期会跨多只股票重复出现,join 右侧必须先收敛到
    唯一日期(右侧唯一 -> 左侧重复, 多对一, 不会因重复索引发生笛卡尔爆炸)。
    market_* 特征在源端做滚动 z-score(按市场帧序列,同一天所有股票取值一致,横截面可比)。
    """
    frame = frame if frame is not None else build_market_frame()
    cols = [c for c in frame.columns if c.startswith("market_")]
    if config.STANDARDIZE_ROLLING:
        frame = zscore_frame(frame[cols])
        cols = list(frame.columns)
    out = features.copy()
    m = frame[cols].reindex(pd.Index(out.index.unique())).ffill()
    out = out.join(m, how="left")
    return out


def market_snapshot() -> dict:
    """最新市场快照:最近交易日市场特征 + 当日涨跌家数(乐咕) + 期指实时基差。"""
    frame = build_market_frame()
    row = frame.iloc[-1]
    snap = {"market_date": str(row.name.date()),
            "market": {c: (None if pd.isna(row[c]) else round(float(row[c]), 4))
                       for c in frame.columns}}
    try:
        snap["activity"] = mk.get_market_activity()
    except Exception as e:  # noqa: BLE001
        print(f"[market_features] 涨跌家数获取失败: {e}")
        snap["activity"] = None
    try:
        snap["futures"] = mk.get_futures_quotes()
    except Exception as e:  # noqa: BLE001
        print(f"[market_features] 期指实时获取失败: {e}")
        snap["futures"] = None
    return snap


def fear_greed_label(fg: float) -> str:
    if fg is None:
        return "未知"
    if fg <= config.FG_EXTREME_FEAR:
        return "极度恐惧"
    if fg <= config.FG_FEAR:
        return "恐慌"
    if fg >= config.FG_EXTREME_GREED:
        return "极度贪婪"
    if fg >= config.FG_GREED:
        return "贪婪"
    return "中性"


def basis_label(basis_avg: float) -> str:
    if basis_avg is None:
        return "未知"
    if basis_avg <= config.BASIS_DEEP_DISCOUNT:
        return "深贴水"
    if basis_avg < 0:
        return "小幅贴水"
    if basis_avg >= config.BASIS_PREMIUM:
        return "升水"
    return "接近平水"


if __name__ == "__main__":
    mf = build_market_frame()
    print(f"市场特征列: {len(mf.columns)}")
    print(mf[["market_adv_ratio", "market_fear_greed", "market_basis_avg"]].tail(5).to_string())
    print("\n== 快照 ==")
    s = market_snapshot()
    print("market_date:", s["market_date"])
    print("fear_greed:", s["market"]["market_fear_greed"], fear_greed_label(s["market"]["market_fear_greed"]))
    print("basis_avg:", s["market"]["market_basis_avg"], basis_label(s["market"]["market_basis_avg"]))
    print("adv_ratio:", s["market"]["market_adv_ratio"])
