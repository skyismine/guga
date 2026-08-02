"""市场情绪数据:股指期货资金/基差、指数行情、全市场涨跌家数(乐咕乐股)。

- 期指资金:IF/IH/IC/IM 连续合约历史日线 + 实时快照,基差 = 期货价/现货指数 - 1
- 涨跌家数:乐咕乐股市场活跃度(当日上涨/下跌/涨停/跌停/活跃度)
- 全部带本地 pickle 缓存 + TTL
"""
import datetime as dt
import os
import pickle
import re
import time
from typing import Dict, Optional

import pandas as pd
import requests

from app import config

_SINA_HQ = "https://hq.sinajs.cn/list={symbols}"
_SINA_REFERER = "https://finance.sina.com.cn"


# ---------------------------------------------------------------- 缓存
def _path(key: str) -> str:
    return os.path.join(config.DATA_DIR, f"market_{key}.pkl")


def _load_cache(key: str, ttl: int = None) -> Optional[object]:
    p = _path(key)
    if not os.path.exists(p):
        return None
    if time.time() - os.path.getmtime(p) > (ttl or config.CACHE_TTL_SECONDS):
        return None
    try:
        with open(p, "rb") as f:
            return pickle.load(f)
    except Exception:
        return None


def _save_cache(key: str, obj) -> None:
    with open(_path(key), "wb") as f:
        pickle.dump(obj, f)


def _norm_daily(df: pd.DataFrame, close_col: str = "close") -> pd.DataFrame:
    df = df.rename(columns={"date": "date"})
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna(subset=["date", close_col]).sort_values("date").drop_duplicates("date")
    df = df.set_index("date")
    return df


# ---------------------------------------------------------------- 指数日线
def get_index_history(symbol: str = None, days: int = None, use_cache: bool = True) -> pd.DataFrame:
    """指数日线(新浪 stock_zh_index_daily)。index=date, 列 open/high/low/close/volume。"""
    symbol = symbol or config.MARKET_INDEX
    days = days or config.HIST_DAYS
    key = f"index_{symbol}"
    if use_cache:
        cached = _load_cache(key, ttl=config.CACHE_TTL_SECONDS * 6)
        if cached is not None and len(cached) >= days:
            return cached
    import akshare as ak
    df = ak.stock_zh_index_daily(symbol=symbol)
    df = _norm_daily(df)
    if df.empty:
        raise ConnectionError(f"指数日线为空: {symbol}")
    df = df.tail(days)
    _save_cache(key, df)
    return df


def get_index_spot(symbol: str = None) -> Dict:
    """指数实时快照(新浪 hq 接口,与股票行情同格式前 6 个数值字段)。"""
    symbol = symbol or config.MARKET_INDEX
    resp = requests.get(_SINA_HQ.format(symbols=symbol),
                        headers={"Referer": _SINA_REFERER}, timeout=10)
    resp.encoding = "gbk"
    m = re.search(r'="(.*)"', resp.text)
    if not m or not m.group(1):
        raise ConnectionError(f"指数实时行情无数据: {symbol}")
    parts = m.group(1).split(",")
    name = parts[0]
    try:
        open_, prev_close, price, high, low = (float(parts[i]) for i in range(1, 6))
    except (TypeError, ValueError, IndexError):
        open_ = prev_close = price = high = low = 0.0
    pct = (price - prev_close) / prev_close if prev_close else 0.0
    m2 = re.search(r"(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2})", resp.text)
    return {"symbol": symbol, "name": name, "open": open_, "prev_close": prev_close,
            "price": price, "high": high, "low": low, "pct_chg": pct,
            "datetime": f"{m2.group(1)} {m2.group(2)}" if m2 else ""}


# ---------------------------------------------------------------- 期指资金/基差
def get_futures_daily(symbol: str = "IF0", days: int = None, use_cache: bool = True) -> pd.DataFrame:
    """期指连续合约历史日线(新浪期货)。index=date, 列 open/high/low/close/volume/hold。"""
    days = days or config.HIST_DAYS
    key = f"futures_{symbol}"
    if use_cache:
        cached = _load_cache(key, ttl=config.CACHE_TTL_SECONDS * 6)
        if cached is not None and len(cached) >= days:
            return cached
    import akshare as ak
    df = ak.futures_zh_daily_sina(symbol=symbol)
    df = _norm_daily(df)
    if df.empty:
        raise ConnectionError(f"期指日线为空: {symbol}")
    df = df.tail(days)
    _save_cache(key, df)
    return df


def get_basis_series(days: int = None, use_cache: bool = True) -> pd.DataFrame:
    """四大期指(IF/IH/IC/IM)历史基差序列。

    基差 = 期货收盘 / 对应指数收盘 - 1(负=贴水,正=升水)。
    返回 index=date, 列 basis_if/basis_ih/basis_ic/basis_im/basis_avg。
    """
    days = days or config.HIST_DAYS
    if use_cache:
        cached = _load_cache("basis", ttl=config.CACHE_TTL_SECONDS * 6)
        if cached is not None and len(cached) >= days:
            return cached

    cols = {}
    for key, (fut_sym, idx_sym) in config.FUTURES_MAP.items():
        try:
            fut = get_futures_daily(fut_sym, days, use_cache=use_cache)["close"]
            idx = get_index_history(idx_sym, days, use_cache=use_cache)["close"]
            cols[f"basis_{key}"] = fut / idx.reindex(fut.index).ffill() - 1
        except Exception as e:  # noqa: BLE001
            print(f"[market] 基差计算失败 {fut_sym}/{idx_sym}: {e}")
    if not cols:
        raise ConnectionError("无法获取期指基差数据")
    df = pd.DataFrame(cols).sort_index().ffill().dropna(how="all")
    df["basis_avg"] = df.mean(axis=1)
    _save_cache("basis", df)
    return df


def get_futures_quotes(use_cache: bool = True) -> Dict:
    """四大期指实时快照(连续合约 IF0/IH0/IC0/IM0)+ 对现货的实时基差。"""
    if use_cache:
        cached = _load_cache("futures_rt", ttl=config.CACHE_TTL_SECONDS)
        if cached is not None:
            return cached
    import akshare as ak
    result = {}
    for key, variety in config.FUTURES_VARIETY.items():
        try:
            df = ak.futures_zh_realtime(symbol=variety)
            row = df[df["symbol"] == f"{key.upper()}0"]
            if row.empty:
                row = df[df["symbol"].str.upper().str.startswith(key.upper())]
            if row.empty:
                continue
            r = row.iloc[0]
            fut_price = float(r["trade"] or r["close"])
            try:
                idx = get_index_spot(config.FUTURES_MAP[key][1])
                basis = (fut_price / idx["price"] - 1) if idx["price"] else None
            except Exception:  # noqa: BLE001
                idx, basis = None, None
            result[key] = {
                "symbol": str(r["symbol"]), "price": fut_price,
                "pct_chg": float(r["changepercent"] or 0),
                "volume": float(r["volume"] or 0), "position": float(r["position"] or 0),
                "tradedate": str(r["tradedate"]),
                "index_price": idx["price"] if idx else None,
                "basis": round(basis, 5) if basis is not None else None,
            }
        except Exception as e:  # noqa: BLE001
            print(f"[market] 期指实时获取失败 {key}: {e}")
    _save_cache("futures_rt", result)
    return result


# ---------------------------------------------------------------- 涨跌家数(乐咕乐股)
def get_market_activity(use_cache: bool = True) -> Dict:
    """全市场当日涨跌家数/涨停跌停/活跃度(乐咕乐股快照)。"""
    if use_cache:
        cached = _load_cache("activity", ttl=config.CACHE_TTL_SECONDS)
        if cached is not None:
            return cached
    import akshare as ak
    df = ak.stock_market_activity_legu()
    mapping = {r["item"]: r["value"] for _, r in df.iterrows()}
    up = float(mapping.get("上涨", 0) or 0)
    down = float(mapping.get("下跌", 0) or 0)
    result = {
        "advance": up,
        "decline": down,
        "flat": float(mapping.get("平盘", 0) or 0),
        "suspended": float(mapping.get("停牌", 0) or 0),
        "limit_up": float(mapping.get("涨停", 0) or 0),
        "real_limit_up": float(mapping.get("真实涨停", 0) or 0),
        "limit_down": float(mapping.get("跌停", 0) or 0),
        "real_limit_down": float(mapping.get("真实跌停", 0) or 0),
        "activity_pct": float(str(mapping.get("活跃度", "0")).replace("%", "") or 0),
        "date": str(mapping.get("统计日期", "")),
        "adv_ratio": up / (up + down) if (up + down) else None,
    }
    _save_cache("activity", result)
    return result


if __name__ == "__main__":
    print("== 涨跌家数 ==")
    print(get_market_activity())
    print("\n== 期指实时 ==")
    print(get_futures_quotes())
    print("\n== 期指基差历史(尾部) ==")
    print(get_basis_series().tail(3).to_string())
    print("\n== 沪深300实时 ==")
    print(get_index_spot())
