"""Akshare 数据抓取层。

- 主用新浪数据源(稳定),东财/腾讯作为回退
- 本地 pickle 缓存 + TTL
- 提供实时行情、股票列表、交易日历等接口
"""
import datetime as dt
import os
import pickle
import re
import time
from typing import Dict, List, Optional

import pandas as pd
import requests

from app import config
from app.data import patch_requests  # noqa: F401  先注入 UA 补丁
from app.data import dal

patch_requests.install()

_SINA_HQ = "https://hq.sinajs.cn/list={symbols}"
_SINA_REFERER = "https://finance.sina.com.cn"
_SINA_SPOT = "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeData"

# 历史日线缓存 TTL(秒):收盘后的历史日线不再变动,延长到 24h 避免每 4h 全量重抓 680 池;
# 盘中最新价由 get_spot_quotes 短缓存提供,日线特征可容忍当日行稍滞后。
_HIST_TTL_SECONDS = 24 * 3600


def _fault(e: BaseException, note: str = ""):
    """记录一次被降级吞掉的异常(接入 fault 统一日志, 不再静默)。"""
    try:
        from app.support import fault as _flt
        _flt.warning("fetcher", note or "处理降级(按缺省继续)", exc=e)
    except Exception as _e:  # noqa: BLE001
        _fault(_e)


# ---------------------------------------------------------------- 工具
def code_to_symbol(code: str) -> str:
    """6xxxxx->sh, 0/3xxxxx->sz, 4/8/9xxxxx->bj, 5xxxxx->sh基金"""
    code = str(code).zfill(6)
    if code.startswith(("60", "68", "50", "51", "52", "56", "58", "11", "12")):
        return f"sh{code}"
    if code.startswith(("00", "30", "15", "16", "18")):
        return f"sz{code}"
    return f"bj{code}"


def symbol_to_code(symbol: str) -> str:
    return str(symbol)[-6:]


def is_stock(code: str) -> bool:
    code = str(code).zfill(6)
    return code.startswith(("60", "68", "00", "30"))


def is_etf(code: str) -> bool:
    """ETF/LOF 判定:沪市 51/52/56/58, 深市 15/16/18。"""
    code = str(code).zfill(6)
    return code.startswith(config.ETF_PREFIXES)


def is_trading_time(now: Optional[dt.datetime] = None) -> bool:
    now = now or dt.datetime.now()
    if now.weekday() >= 5:
        return False
    t = now.time()
    return (dt.time(9, 30) <= t <= dt.time(11, 30)) or \
           (dt.time(13, 0) <= t <= dt.time(15, 0))


def get_trade_dates(end: str = None, count: int = 60) -> List[pd.Timestamp]:
    """最近 count 个交易日(交易日历接口 → fuyao 官方日历兜底 → 股票历史推算)。"""
    try:
        import akshare as ak
        df = ak.tool_trade_date_hist_sina()
        dates = pd.to_datetime(df["trade_date"]).tolist()
        return dates[-count:]
    except Exception as _e:  # noqa: BLE001
        _fault(_e)
    # fuyao 官方交易日历兜底(近一年;不足 count 时继续回退)
    try:
        from app.data.fuyao import enabled as _fy_enabled
        from app.data.fuyao import get_calendar_trading_days as _fy_cal
        if _fy_enabled():
            items = _fy_cal()
            dates = pd.to_datetime([it["date"] for it in items], format="%Y%m%d").tolist()
            if len(dates) >= count:
                return dates[-count:]
    except Exception as _e:  # noqa: BLE001
        _fault(_e)
    try:
        df = get_daily_history("600519", days=max(count * 2, 120))
        return list(df.index[-count:])
    except Exception:
        return list(pd.bdate_range(end=end, periods=count))


# ---------------------------------------------------------------- 缓存
def _cache_path(code: str) -> str:
    return os.path.join(config.HIST_DIR, f"{str(code).zfill(6)}.pkl")


def _load_cache(code: str, ttl: float = None) -> Optional[pd.DataFrame]:
    path = _cache_path(code)
    if not os.path.exists(path):
        return None
    mtime = os.path.getmtime(path)
    if time.time() - mtime > (ttl if ttl is not None else config.CACHE_TTL_SECONDS):
        return None
    try:
        with open(path, "rb") as f:
            return pickle.load(f)
    except Exception:
        return None


def _save_cache(code: str, df: pd.DataFrame) -> None:
    with open(_cache_path(code), "wb") as f:
        pickle.dump(df, f)


# ---------------------------------------------------------------- 历史日线
def _norm(df: pd.DataFrame, source: str) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    mapping = {
        "date": "date", "日期": "date", "trade_date": "date",
        "open": "open", "开盘": "open",
        "high": "high", "最高": "high",
        "low": "low", "最低": "low",
        "close": "close", "收盘": "close",
        "volume": "volume", "成交量": "volume", "vol": "volume",
        "amount": "amount", "成交额": "amount",
    }
    df = df.rename(columns=mapping)
    keep = [c for c in ("date", "open", "high", "low", "close", "volume", "amount") if c in df.columns]
    df = df[keep].copy()
    if df.empty:
        return df
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna(subset=["date", "close"]).sort_values("date").drop_duplicates("date")
    df = df.set_index("date")
    for c in ("open", "high", "low", "close", "volume", "amount"):
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


def _fetch_sina(code: str, start: str, end: str, adjust: str) -> pd.DataFrame:
    import akshare as ak
    df = ak.stock_zh_a_daily(symbol=code_to_symbol(code), start_date=start, end_date=end, adjust=adjust)
    return _norm(df, "sina")


def _fetch_eastmoney(code: str, start: str, end: str, adjust: str) -> pd.DataFrame:
    import akshare as ak
    df = ak.stock_zh_a_hist(symbol=str(code).zfill(6), period="daily", start_date=start, end_date=end, adjust=adjust)
    return _norm(df, "eastmoney")


def _fetch_tencent(code: str, start: str, end: str, adjust: str) -> pd.DataFrame:
    import akshare as ak
    df = ak.stock_zh_a_hist_tx(symbol=code_to_symbol(code), start_date=start, end_date=end, adjust=adjust)
    return _norm(df, "tencent")


def _fetch_etf_sina(code: str, start: str, end: str) -> pd.DataFrame:
    """ETF 历史日线(新浪基金,全量数据后按日期裁剪;不含复权)。"""
    import akshare as ak
    df = ak.fund_etf_hist_sina(symbol=code_to_symbol(code))
    df = _norm(df, "etf_sina")
    if df.empty:
        return df
    start_ts = pd.Timestamp(start)
    end_ts = pd.Timestamp(end) + pd.Timedelta(days=1)
    return df[(df.index >= start_ts) & (df.index < end_ts)]


def _fetch_etf_em(code: str, start: str, end: str, adjust: str) -> pd.DataFrame:
    """ETF 历史日线(东财,支持复权;可能被 WAF 限流作为回退)。"""
    import akshare as ak
    df = ak.fund_etf_hist_em(symbol=str(code).zfill(6), period="daily",
                             start_date=start, end_date=end, adjust=adjust)
    return _norm(df, "etf_em")


def get_daily_history(code: str, days: int = None, adjust: str = "qfq", use_cache: bool = True) -> pd.DataFrame:
    """获取个股日线,自动多源回退并本地缓存。返回 index=date 的 DataFrame。

    复权口径统一: 默认前复权(qfq),与全系统一致(dal.ADJUST_UNIFIED)。
    缓存: 内存(60s,DAL 标准化 key hist:{code}:daily) → 本地文件(HIST_DIR,24h)。
    """
    days = days or config.HIST_DAYS
    code = str(code).zfill(6)
    _hist_key = dal.cache_key("hist", code, period="daily")
    _need_cols = {"open", "high", "low", "close", "volume"}
    if use_cache:
        mem = dal.mem_get(_hist_key)
        if mem is not None and len(mem) >= days and _need_cols.issubset(mem.columns):
            return mem
        cached = _load_cache(code, ttl=_HIST_TTL_SECONDS)
        if (cached is not None and len(cached) >= days and _need_cols.issubset(cached.columns)):
            dal.mem_set(_hist_key, cached, 60)
            return cached

    end = dt.datetime.now().strftime("%Y%m%d")
    start = (dt.datetime.now() - dt.timedelta(days=int(days * 1.6) + 40)).strftime("%Y%m%d")

    df = pd.DataFrame()
    errors = []
    if is_etf(code):
        source_map = {
            "etf_sina": lambda: _fetch_etf_sina(code, start, end),
            "etf_em": lambda: _fetch_etf_em(code, start, end, adjust),
        }
        source_order = config.ETF_DATA_SOURCE_ORDER
    else:
        source_map = {
            "sina": lambda: _fetch_sina(code, start, end, adjust),
            "eastmoney": lambda: _fetch_eastmoney(code, start, end, adjust),
            "tencent": lambda: _fetch_tencent(code, start, end, adjust),
        }
        source_order = config.DATA_SOURCE_ORDER

    for source in source_order:
        try:
            df = source_map[source]()
            if df is not None and len(df) > 0:
                break
        except Exception as e:  # noqa: BLE001
            errors.append(f"{source}:{e}")
            time.sleep(0.8)

    if df is None or df.empty:
        # P0 兜底: fuyao 官方 API(需 settings.fuyao.enabled;前复权 forward ≈ 本地 qfq 口径)。
        # 仅股票走 fuyao:ETF 的 fund 行情端点在部分账号/Key 下不可用,ETF 仍用 akshare 双源。
        if not is_etf(code):
            try:
                from app.data.fuyao import enabled as _fy_enabled
                from app.data.fuyao import get_daily_history as _fy_hist
                if _fy_enabled():
                    df = _fy_hist(code, days, adjust="forward")
            except Exception as e:  # noqa: BLE001
                errors.append(f"fuyao:{e}")

    if df is None or df.empty:
        kind = "ETF" if is_etf(code) else "股票"
        raise ConnectionError(f"所有数据源均失败 [{kind} {code}]: {errors}")

    df = df.tail(days)
    _save_cache(code, df)
    dal.mem_set(_hist_key, df, 60)
    return df


# ---------------------------------------------------------------- 股票列表
def get_stock_list() -> pd.DataFrame:
    """全市场股票列表,优先新浪接口(稳定),东财回退。缓存:内存(60s)→文件(按交易日)。"""
    _key = dal.cache_key("stock_list", date=dal.trade_date_str())
    hit = dal.mem_get(_key)
    if hit is not None:
        return hit
    cache_path = os.path.join(config.DATA_DIR, "stock_list.pkl")
    if os.path.exists(cache_path) and time.time() - os.path.getmtime(cache_path) < config.CACHE_TTL_SECONDS:
        with open(cache_path, "rb") as f:
            df = pickle.load(f)
        dal.mem_set(_key, df, 60)
        return df

    import akshare as ak
    df = pd.DataFrame()
    try:
        df = ak.stock_zh_a_spot()          # 新浪
    except Exception:
        time.sleep(1)
        try:
            df = ak.stock_zh_a_spot_em()   # 东财回退
        except Exception as e:
            raise ConnectionError(f"获取股票列表失败: {e}") from e

    cols = {"代码": "code", "symbol": "code", "名称": "name", "最新价": "close", "涨跌幅": "pct_chg"}
    df = df.rename(columns=cols)
    if "code" not in df.columns:
        df = df.rename(columns={df.columns[0]: "code", df.columns[1]: "name"})
    df["code"] = df["code"].astype(str).str.replace(r"(sh|sz|bj)", "", regex=True).str.zfill(6)
    df = df[df["code"].apply(is_stock)]
    with open(cache_path, "wb") as f:
        pickle.dump(df, f)
    dal.mem_set(_key, df, 60)
    return df


def get_stock_name(code: str) -> str:
    code = str(code).zfill(6)
    try:
        quote = get_spot_quote(code)
        name = quote.get("name") or ""
        if name:
            return name
    except Exception as _e:  # noqa: BLE001
        _fault(_e)
    try:
        lst = get_stock_list()
        hit = lst[lst["code"] == code]
        if not hit.empty:
            return str(hit.iloc[0]["name"])
    except Exception as _e:  # noqa: BLE001
        _fault(_e)
    return code


# ---------------------------------------------------------------- 实时行情
_QUOTE_FIELDS = ("name", "open", "prev_close", "price", "high", "low",
                 "bid", "ask", "volume", "amount", "bid1_volume", "bid1",
                 "bid2_volume", "bid2", "bid3_volume", "bid3",
                 "bid4_volume", "bid4", "bid5_volume", "bid5",
                 "ask1_volume", "ask1", "ask2_volume", "ask2",
                 "ask3_volume", "ask3", "ask4_volume", "ask4",
                 "ask5_volume", "ask5", "date", "time", "status")


def _spot_local_fallback(codes) -> Dict[str, Dict]:
    """本地日快照兜底:全A快照(spot_{date}.json)+ ETF 快照(etf_{date}.json)。

    新浪/东财实时接口被限流或不可达(403/断连)时,用系统每日必然生成的
    data_cache/spot_*.json 与 etf_*.json 提供 name/price,避免逐只联网阻塞页面。
    """
    out = {}
    try:
        from app.support.mainline import _a_spot_map, _etf_map
        spot = _a_spot_map()
        for c in codes:
            it = spot.get(c) or {}
            if it.get("price"):
                out[c] = {"name": it.get("name", ""), "price": float(it.get("price")),
                          "pct_chg": float(it.get("pct_chg") or 0),
                          "amount": float(it.get("amount") or 0),
                          "source": "local_snapshot"}
        etf = _etf_map()
        for c in codes:
            if c in out:
                continue
            it = next((v for v in etf.values() if v.get("code") == c), None)
            if not it or not it.get("price"):
                continue
            nm = next((n for n, v in etf.items() if v.get("code") == c), c)
            out[c] = {"name": nm, "price": float(it.get("price")),
                      "pct_chg": 0.0, "amount": float(it.get("amount_wan") or 0) * 1e4,
                      "source": "local_snapshot"}
    except Exception:  # noqa: BLE001  本地兜底失败不抛错,按无行情继续
        pass
    return out


def get_spot_quote(code: str) -> Dict:
    """实时快照(新浪行情接口,轻量快速)。内存缓存 30s;附 data_quality 标注。

    新浪不可达(403/断连)时回退本地日快照,保证页面/风控仍可用不联网卡死。
    """
    code = str(code).zfill(6)
    _key = dal.cache_key("spot", code, date=dal.today_str())
    hit = dal.mem_get(_key)
    if hit is not None:
        return hit
    symbol = code_to_symbol(code)
    try:
        resp = requests.get(_SINA_HQ.format(symbols=symbol),
                            headers={"Referer": _SINA_REFERER}, timeout=10)
        resp.encoding = "gbk"
        m = re.search(r'="(.*)"', resp.text)
        if m and m.group(1):
            parts = m.group(1).split(",")
            quote = dict(zip(_QUOTE_FIELDS, parts))
            for k in ("open", "prev_close", "price", "high", "low", "volume", "amount"):
                try:
                    quote[k] = float(quote[k])
                except (TypeError, ValueError):
                    quote[k] = 0.0
            quote["pct_chg"] = (quote["price"] - quote["prev_close"]) / quote["prev_close"] if quote["prev_close"] else 0.0
            quote["datetime"] = f"{quote.get('date')} {quote.get('time')}"
            dal.attach_quality(quote, src="sina_hq", note="实时快照")
            dal.mem_set(_key, quote, 30)
            return quote
    except Exception as _e:  # noqa: BLE001  实时失败降级本地快照
        _fault(_e, f"新浪实时行情失败({code}),回退本地快照")
    fb = _spot_local_fallback([code]).get(code)
    if fb:
        dal.attach_quality(fb, 0.6, "local_snapshot", "本地日快照(实时接口不可达)")
        dal.mem_set(_key, fb, 30)
        return fb
    # fuyao 官方行情兜底(股票): 新浪/本地快照均缺时取同花顺快照, 避免静默回退旧日线
    try:
        from app.data.fuyao import enabled as _fy_enabled, _get, _thscode
        if _fy_enabled() and not is_etf(code):
            _data = _get("/api/a-share/prices/snapshot",
                         {"thscodes": _thscode(code)}, ttl=30) or {}
            for _it in (_data.get("item") or []):
                _lp = _it.get("last_price")
                if not _lp:
                    break
                _pc = float(_it.get("prev_price") or 0) or 0.0
                try:
                    _pct = (float(_lp) - _pc) / _pc if _pc else 0.0
                except (TypeError, ValueError):  # noqa: BLE001
                    _pct = 0.0
                _q = {
                    "name": "", "price": float(_lp), "prev_close": _pc,
                    "open": float(_it.get("open_price") or 0) or 0.0,
                    "high": float(_it.get("high_price") or 0) or 0.0,
                    "low": float(_it.get("low_price") or 0) or 0.0,
                    "volume": float(_it.get("volume") or 0) or 0.0,
                    "amount": float(_it.get("turnover") or 0) or 0.0,
                    "pct_chg": _pct,
                }
                dal.attach_quality(_q, 1.0, "fuyao", "官方行情快照(兜底)")
                dal.mem_set(_key, _q, 30)
                return _q
    except Exception as _e:  # noqa: BLE001
        _fault(_e, "fuyao 单票行情兜底失败")
    raise ConnectionError(f"新浪实时行情无数据: {code}")


def get_spot_quotes(codes: List[str]) -> Dict[str, Dict]:
    """批量实时快照。内存缓存 30s;每条附 data_quality 标注。

    本地日快照优先(spot/etf json,非交易时段即当日收盘价,零网络等待);
    快照缺失的代码再走新浪实时,仍失败则跳过(不阻塞调用方)。
    """
    codes = [str(c).zfill(6) for c in codes]
    _today = dal.today_str()
    out = {}
    fresh_codes = []
    for c in codes:
        hit = dal.mem_get(dal.cache_key("spot", c, date=_today))
        if hit is not None:
            out[c] = hit
        else:
            fresh_codes.append(c)
    if fresh_codes:
        for c, q in _spot_local_fallback(fresh_codes).items():
            dal.attach_quality(q, 0.6, "local_snapshot", "本地日快照(收盘价)")
            out[c] = q
            dal.mem_set(dal.cache_key("spot", c, date=_today), q, 30)
        live_codes = [c for c in fresh_codes if c not in out]
        if live_codes:
            try:
                symbols = ",".join(code_to_symbol(c) for c in live_codes)
                resp = requests.get(_SINA_HQ.format(symbols=symbols),
                                    headers={"Referer": _SINA_REFERER}, timeout=10)
                resp.encoding = "gbk"
                for line in resp.text.strip().splitlines():
                    m = re.match(r'var hq_str_(\w+)="(.*)";?', line)
                    if not m:
                        continue
                    symbol, payload = m.group(1), m.group(2)
                    if not payload:
                        continue
                    parts = payload.split(",")
                    q = dict(zip(_QUOTE_FIELDS, parts))
                    for k in ("open", "prev_close", "price", "high", "low", "volume", "amount"):
                        try:
                            q[k] = float(q[k])
                        except (TypeError, ValueError):
                            q[k] = 0.0
                    q["pct_chg"] = (q["price"] - q["prev_close"]) / q["prev_close"] if q["prev_close"] else 0.0
                    dal.attach_quality(q, src="sina_hq", note="实时快照")
                    c = symbol_to_code(symbol)
                    out[c] = q
                    dal.mem_set(dal.cache_key("spot", c, date=_today), q, 30)
            except Exception as _e:  # noqa: BLE001  实时失败按已有快照继续
                _fault(_e, "新浪批量行情失败,按本地快照继续")
        # fuyao 官方行情兜底: 本地快照/新浪均缺的股票, 批量取同花顺快照(收盘后=当日收盘)。
        # 解决新浪实时不可达时持仓/标的行情静默回退"上一交易日日线"致复盘数据滞后一日。
        miss = [c for c in codes if c not in out and not is_etf(c)]
        if miss:
            names = {}
            try:
                _lst = get_stock_list()
                if _lst is not None and len(_lst):
                    names = {str(r["code"]).zfill(6): str(r["name"])
                             for r in _lst.to_dict("records")}
            except Exception:  # noqa: BLE001
                names = {}
            try:
                from app.data.fuyao import enabled as _fy_enabled
                if _fy_enabled():
                    from app.data.fuyao import _get, _thscode
                    for _i in range(0, len(miss), 50):
                        _chunk = miss[_i:_i + 50]
                        _data = _get("/api/a-share/prices/snapshot",
                                     {"thscodes": ",".join(_thscode(c) for c in _chunk)},
                                     ttl=30) or {}
                        for _it in (_data.get("item") or []):
                            _c = str(_it.get("ticker", ""))[-6:]
                            _lp = _it.get("last_price")
                            if not _lp:
                                continue
                            _pc = float(_it.get("prev_price") or 0) or 0.0
                            try:
                                _pct = (float(_lp) - _pc) / _pc if _pc else 0.0
                            except (TypeError, ValueError):  # noqa: BLE001
                                _pct = 0.0
                            _q = {
                                "name": names.get(_c, ""),
                                "price": float(_lp),
                                "prev_close": _pc,
                                "open": float(_it.get("open_price") or 0) or 0.0,
                                "high": float(_it.get("high_price") or 0) or 0.0,
                                "low": float(_it.get("low_price") or 0) or 0.0,
                                "volume": float(_it.get("volume") or 0) or 0.0,
                                "amount": float(_it.get("turnover") or 0) or 0.0,
                                "pct_chg": _pct,
                            }
                            dal.attach_quality(_q, 1.0, "fuyao", "官方行情快照(兜底)")
                            out[_c] = _q
                            dal.mem_set(dal.cache_key("spot", _c, date=_today), _q, 30)
            except Exception as _e:  # noqa: BLE001  官方兜底失败按现有行情继续
                _fault(_e, "fuyao 行情兜底失败,按现有快照继续")
    return out


# ---------------------------------------------------------------- 分钟K线(触发量化)
def get_intraday_bars(code: str, period: str = "5", limit: int = 48) -> pd.DataFrame:
    """当日分钟K线(东财),返回 index=time 的 DataFrame(open/high/low/close/amount)。

    用于盘中触发状态量化判断;非交易时段或失败返回空 DataFrame(不抛错)。
    默认 5 分钟周期取当日最近 48 根(约覆盖全天 4 小时)。
    """
    code = str(code).zfill(6)
    try:
        import akshare as ak
        df = ak.stock_zh_a_hist_min_em(symbol=code, period=period, adjust="")
        if df is None or df.empty:
            return pd.DataFrame()
        df = df.rename(columns={c: str(c) for c in df.columns})
        keep = {k: v for k, v in {
            "时间": "time", "开盘": "open", "最高": "high", "最低": "low",
            "收盘": "close", "成交量": "volume", "成交额": "amount",
        }.items() if k in df.columns}
        df = df.rename(columns=keep)
        if "time" not in df.columns or "amount" not in df.columns:
            return pd.DataFrame()
        df["time"] = pd.to_datetime(df["time"], errors="coerce")
        df = df.dropna(subset=["time"])
        for c in ("open", "high", "low", "close", "amount"):
            if c in df.columns:
                df[c] = pd.to_numeric(df[c], errors="coerce")
        df = df.set_index("time").sort_index().tail(limit)
        return df
    except Exception:  # noqa: BLE001
        return pd.DataFrame()
