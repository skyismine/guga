"""行业/风格特征:所属申万一级行业指数涨跌幅 + 相对行业超额收益(alpha)。

目的:让模型区分"个股 alpha"与"行业 beta",提升跨风格/跨行业稳定性。
新增特征(ind_* / alpha_* 前缀):
- ind_ret_1/5/20 : 行业指数近 1/5/20 日涨跌幅(行业 beta)
- ind_ma20_gap   : 行业指数相对其 MA20 的位置(行业趋势)
- ind_vol20      : 行业指数 20 日波动率(行业风险)
- alpha_1/5/20   : 个股涨跌幅 - 行业涨跌幅(相对超额收益/alpha)
- alpha_trend    : (个股相对其MA20) - (行业相对其MA20)(相对行业动能)

设计原则:
- 行业指数用申万一级(sina 源,index_hist_sw),历史长且稳定;
- 个股->行业映射:样本池静态表优先,任意代码动态解析兜底,均本地缓存;
- 全部为"当日及历史"信息,无前视;解析失败/ETF 等无行业标的的行业特征为 NaN,
  LightGBM 原生处理缺失。
"""
import os
import pickle
import time

import numpy as np
import pandas as pd

from app import config
from app.features.indicators import compute_features
from app.features.market_features import attach_market_features

# 申万一级行业名称 <-> 代码(固定集合)
SW_CODE_TO_NAME = {
    "801010": "农林牧渔", "801030": "基础化工", "801040": "钢铁",
    "801050": "有色金属", "801080": "电子", "801110": "家用电器",
    "801120": "食品饮料", "801130": "纺织服饰", "801140": "轻工制造",
    "801150": "医药生物", "801160": "公用事业", "801170": "交通运输",
    "801180": "房地产", "801200": "商贸零售", "801210": "社会服务",
    "801230": "综合", "801710": "建筑材料", "801720": "建筑装饰",
    "801730": "电力设备", "801740": "国防军工", "801750": "计算机",
    "801760": "传媒", "801770": "通信", "801780": "银行",
    "801790": "非银金融", "801880": "汽车", "801890": "机械设备",
    "801950": "煤炭", "801960": "石油石化", "801970": "环保",
    "801980": "美容护理",
}
SW_NAME_TO_CODE = {v: k for k, v in SW_CODE_TO_NAME.items()}

# 样本池静态映射:股票代码 -> 申万一级行业代码(训练/回测池,避免逐只动态解析)
STATIC_STOCK_INDUSTRY = {
    "600519": "801120", "601318": "801790", "600036": "801780",
    "601899": "801050", "600030": "801790", "600900": "801160",
    "601012": "801730", "600887": "801120", "600309": "801030",
    "603259": "801150", "000001": "801780", "000858": "801120",
    "000333": "801110", "000651": "801110", "002594": "801880",
    "002415": "801080", "300750": "801730", "300059": "801790",
    "300124": "801730", "002230": "801750",
}

_IND_HIST_DIR = os.path.join(config.DATA_DIR, "industry")
_MAP_PATH = os.path.join(config.DATA_DIR, "industry_code_map.json")
_map_cache = {}


def get_industry_sw(code: str):
    """返回股票对应的申万一级行业代码(或 None)。本地缓存 + 静态表优先。"""
    code = str(code).zfill(6)
    if code in STATIC_STOCK_INDUSTRY:
        return STATIC_STOCK_INDUSTRY[code]
    if not _map_cache:
        _load_map()
    if code in _map_cache:
        return _map_cache[code]
    return _resolve_dynamic(code)


def _load_map():
    global _map_cache
    try:
        import json
        with open(_MAP_PATH, encoding="utf-8") as f:
            _map_cache = json.load(f)
    except (OSError, ValueError):
        _map_cache = {}


def _save_map():
    global _map_cache
    try:
        import json
        os.makedirs(os.path.dirname(_MAP_PATH), exist_ok=True)
        with open(_MAP_PATH, "w", encoding="utf-8") as f:
            json.dump(_map_cache, f, ensure_ascii=False, indent=2)
    except OSError:
        pass


def _resolve_dynamic(code: str):
    """动态解析个股行业(akshare,失败返回 None)。"""
    sw = None
    try:
        import akshare as ak
        info = ak.stock_individual_info_em(symbol=code)
        for _, row in info.iterrows():
            if row.get("item") == "行业":
                name = str(row.get("value", "")).strip()
                if name in SW_NAME_TO_CODE:
                    sw = SW_NAME_TO_CODE[name]
                else:
                    for nm, c in SW_NAME_TO_CODE.items():
                        if nm in name or name in nm:
                            sw = c
                            break
                break
    except Exception as e:  # noqa: BLE001
        print(f"[industry] {code} 行业解析失败: {e}")
    _map_cache[code] = sw
    _save_map()
    return sw


def _get_sw_index_close(sw_code: str) -> pd.Series:
    """申万一级行业指数日收盘序列(本地缓存 TTL)。"""
    os.makedirs(_IND_HIST_DIR, exist_ok=True)
    path = os.path.join(_IND_HIST_DIR, f"{sw_code}.pkl")
    if os.path.exists(path) and time.time() - os.path.getmtime(path) <= config.CACHE_TTL_SECONDS:
        try:
            with open(path, "rb") as f:
                return pickle.load(f)
        except Exception:
            pass
    import akshare as ak
    df = ak.index_hist_sw(symbol=sw_code, period="day")
    close = df.set_index("日期")["收盘"].astype(float).sort_index()
    with open(path, "wb") as f:
        pickle.dump(close, f)
    return close


def _industry_frame_for(sw_code: str, index: pd.DatetimeIndex) -> pd.DataFrame:
    """行业特征帧,对齐到给定交易日索引。"""
    close = _get_sw_index_close(sw_code).reindex(index).ffill()
    ret1 = close.pct_change()
    ind = pd.DataFrame(index=index)
    ind["ind_ret_1"] = ret1
    ind["ind_ret_5"] = close.pct_change(5)
    ind["ind_ret_20"] = close.pct_change(20)
    ind["ind_ma20_gap"] = close / close.rolling(20).mean() - 1
    ind["ind_vol20"] = ret1.rolling(20).std()
    return ind


def attach_industry_features(data: pd.DataFrame) -> pd.DataFrame:
    """把行业特征按 (code, date) 对齐并入数据集(训练路径,data 含 code 列)。"""
    if "code" not in data.columns:
        return data
    out = data.copy()
    for code in out["code"].unique():
        sw = get_industry_sw(code)
        if sw is None:
            continue
        mask = out["code"] == code
        idx = out.index[mask]
        ind = _industry_frame_for(sw, pd.DatetimeIndex(idx))
        for col in ind.columns:
            out.loc[mask, col] = ind[col].values
        for h in (1, 5, 20):
            out.loc[mask, f"alpha_{h}"] = out.loc[mask, f"ret_{h}"] - ind[f"ind_ret_{h}"].values
        out.loc[mask, "alpha_trend"] = out.loc[mask, "close_ma20"].values - ind["ind_ma20_gap"].values
    return out


def prepare_features(df: pd.DataFrame, code: str = None) -> pd.DataFrame:
    """统一特征装配:个股技术特征 + 市场级特征 + 行业特征(推断/回测路径)。"""
    code = str(code).zfill(6) if code else (getattr(df, "name", None) or None)
    features = attach_market_features(compute_features(df))
    sw = get_industry_sw(code) if code else None
    if sw:
        ind = _industry_frame_for(sw, features.index)
        features = features.join(ind, how="left")
        for h in (1, 5, 20):
            features[f"alpha_{h}"] = features[f"ret_{h}"] - features[f"ind_ret_{h}"]
        features["alpha_trend"] = features["close_ma20"] - features["ind_ma20_gap"]
    return features


if __name__ == "__main__":
    for c in ("600519", "300750", "000001"):
        sw = get_industry_sw(c)
        print(f"{c} -> {sw}({SW_CODE_TO_NAME.get(sw, '?')})")
    df = prepare_features(__import__("app.data.fetcher", fromlist=["get_daily_history"])
                          .get_daily_history("600519", days=650, adjust="qfq"), "600519")
    print("行业特征列:", [c for c in df.columns if c.startswith(("ind_", "alpha_"))])
    print(df[["ind_ret_20", "alpha_20", "ind_ma20_gap"]].tail(3).round(4).to_string())
