# -*- coding: utf-8 -*-
"""TDX(通达信)数据源适配层(基于 eltdx, 默认启用)。

定位: **行情优先源/兜底源**——实时快照 + 前复权日K + 指数实时; 不覆盖板块资金流/龙虎榜/新闻。
在"能用"的行情请求处优先走 TDX(命中即用), 其它源(本地快照/新浪/东财/fuyao)作降级, 以减轻 fuyao 429。
开关: settings.tdx.enabled(默认 True)。eltdx 不可导入(如服务器未安装)时全函数返回空, 自动降级。
健壮性: 单例客户端 + 进程锁 + 失败负缓存(避免重试风暴); 全程异常吞掉。
性能: eltdx 首次构造会探测服务器/下载资源, 较慢; 命中后快。
许可: eltdx 为 Research-Only, 仅用于个人研究。
"""
import threading
import time

import pandas as pd

_LOCK = threading.Lock()
_CLIENT = None
_FAIL_TS = 0.0
_FAIL_BACKOFF = 300.0     # 失败后负缓存秒数
_IMPORT_ERR = None


def _cfg() -> dict:
    try:
        from app.support import settings as _st
        return (_st.load().get("tdx") or {})
    except Exception:  # noqa: BLE001
        return {}


def enabled() -> bool:
    """是否启用: settings.tdx.enabled=True 且 eltdx 可导入。"""
    global _IMPORT_ERR
    if not _cfg().get("enabled", False):
        return False
    try:
        import eltdx  # noqa: F401
        return True
    except Exception as e:  # noqa: BLE001
        _IMPORT_ERR = e
        return False


def _sym(code) -> str:
    """6 位代码 → eltdx 市场前缀符号(复用 fetcher.code_to_symbol, 惰性导入防环)。"""
    try:
        from app.data.fetcher import code_to_symbol
        return code_to_symbol(str(code).zfill(6))
    except Exception:  # noqa: BLE001
        c = str(code).zfill(6)
        if c.startswith(("6", "5", "9")):
            return "sh" + c
        if c.startswith(("0", "2", "3", "1")):
            return "sz" + c
        return "bj" + c


def _client():
    """单例 TdxClient(惰性构造); 失败进入负缓存。"""
    global _CLIENT, _FAIL_TS
    with _LOCK:
        if _CLIENT is not None:
            return _CLIENT
        if time.time() < _FAIL_TS:
            return None
        try:
            from eltdx import TdxClient
            c = _cfg()
            hosts = c.get("hosts") or None
            _CLIENT = TdxClient(timeout=float(c.get("timeout", 8.0) or 8.0),
                                hosts=hosts, probe_hosts=bool(c.get("probe_hosts", True)))
            return _CLIENT
        except Exception:  # noqa: BLE001
            _FAIL_TS = time.time() + _FAIL_BACKOFF
            return None


def _mark_fail() -> None:
    global _FAIL_TS
    _FAIL_TS = time.time() + _FAIL_BACKOFF


def get_spot(codes) -> dict:
    """批量实时快照 → {code: {name,price,prev_close,open,high,low,volume,amount,pct_chg,source}}。

    失败返回 {}(不抛)。仅用于兜底, 调用方按缺失代码传入即可。
    """
    if not enabled():
        return {}
    codes = [str(c).zfill(6) for c in (codes or [])]
    if not codes:
        return {}
    cl = _client()
    if cl is None:
        return {}
    out = {}
    try:
        snaps = cl.quotes.get_snapshots([_sym(c) for c in codes])
        from app.data import dal
        for s in (snaps or []):
            code = str(getattr(s, "code", "") or "").zfill(6)
            px = getattr(s, "last_price", None)
            if not code or not px:
                continue
            pre = float(getattr(s, "pre_close_price", 0) or 0)
            q = {"name": "", "price": float(px), "prev_close": pre,
                 "open": float(getattr(s, "open_price", 0) or 0),
                 "high": float(getattr(s, "high_price", 0) or 0),
                 "low": float(getattr(s, "low_price", 0) or 0),
                 "volume": float(getattr(s, "total_hand", 0) or 0),
                 "amount": float(getattr(s, "amount", 0) or 0),
                 "pct_chg": (float(px) / pre - 1) if pre else 0.0,
                 "source": "tdx"}
            try:
                dal.attach_quality(q, 1.0, "tdx", "通达信实时快照(兜底源)")
            except Exception:  # noqa: BLE001
                pass
            out[code] = q
        return out
    except Exception:  # noqa: BLE001
        _mark_fail()
        return {}


def get_daily(code, days: int = 600):
    """前复权日线 DataFrame(index=date; open/high/low/close/volume/amount); 失败返回 None。"""
    if not enabled():
        return None
    try:
        cl = _client()
        if cl is None:
            return None
        ks = cl.bars.get(_sym(code), period="day", count=int(days), adjust=str(_cfg().get("adjust", "qfq")))
        bars = list(getattr(ks, "bars", []) or [])
        if not bars:
            return None
        recs = [{"date": getattr(b, "time", None),
                 "open": getattr(b, "open", None), "high": getattr(b, "high", None),
                 "low": getattr(b, "low", None), "close": getattr(b, "close", None),
                 "volume": getattr(b, "volume_lots", None) or getattr(b, "volume_wire_value", None),
                 "amount": getattr(b, "amount", None)} for b in bars]
        df = pd.DataFrame(recs)
        df["date"] = pd.to_datetime(df["date"], errors="coerce")
        # 统一 tz-naive: tdx 时间为 Asia/Shanghai tz-aware, 下游(特征/训练)按 naive 比较否则报错
        try:
            if getattr(df["date"].dt, "tz", None) is not None:
                df["date"] = df["date"].dt.tz_localize(None)
        except Exception:  # noqa: BLE001
            pass
        df = df.dropna(subset=["date", "close"]).drop_duplicates("date").sort_values("date")
        if df.empty:
            return None
        df = df.set_index("date")
        return df
    except Exception:  # noqa: BLE001
        _mark_fail()
        return None


def get_index_spot(symbol: str):
    """指数实时点(传入已带市场前缀的符号, 如 sh000300) → dict; 失败返回 None。"""
    if not enabled():
        return None
    try:
        cl = _client()
        if cl is None:
            return None
        snaps = cl.quotes.get_snapshots([symbol])
        s = (snaps or [None])[0]
        if s is None:
            return None
        px = getattr(s, "last_price", None)
        if not px:
            return None
        pre = float(getattr(s, "pre_close_price", 0) or 0)
        return {"symbol": symbol, "name": "", "open": float(getattr(s, "open_price", 0) or 0),
                "prev_close": pre, "price": float(px),
                "high": float(getattr(s, "high_price", 0) or 0),
                "low": float(getattr(s, "low_price", 0) or 0),
                "pct_chg": (float(px) / pre - 1) if pre else 0.0,
                "amount": float(getattr(s, "amount", 0) or 0), "datetime": "", "src": "tdx"}
    except Exception:  # noqa: BLE001
        _mark_fail()
        return None


_MF_CACHE = {}            # code -> (date, {"main_net","main_ratio","total_amount"})


def money_flow_batch(codes) -> dict:
    """批量个股资金流(最近一日) → {code: {date,main_net,main_ratio,total_amount}}。

    单次 eltdx 调用可传多代码; 结果按 code 进程内按日缓存。失败返回 {}(不抛)。
    板块资金流 eltdx 不支持。
    """
    if not enabled():
        return {}
    codes = [str(c).zfill(6) for c in (codes or []) if c]
    if not codes:
        return {}
    # 命中缓存
    import datetime as _dt
    today = _dt.date.today().isoformat()
    out, need = {}, []
    for c in codes:
        hit = _MF_CACHE.get(c)
        if hit and hit[0] == today:
            out[c] = hit[1]
        else:
            need.append(c)
    if not need:
        return out
    cl = _client()
    if cl is None:
        return out
    try:
        mf = cl.money_flow.daily([_sym(c) for c in need])
        for blk in (getattr(mf, "blocks", []) or []):
            code = str(getattr(blk, "code", "") or "").zfill(6)
            recs = list(getattr(blk, "records", []) or [])
            if not code or not recs:
                continue
            r = recs[-1]
            info = {"date": str(getattr(r, "date", "")),
                    "main_net": getattr(r, "main_net", None),
                    "main_ratio": getattr(r, "main_ratio", None),
                    "total_amount": getattr(r, "total_amount", None)}
            out[code] = info
            _MF_CACHE[code] = (today, info)
        return out
    except Exception:  # noqa: BLE001
        _mark_fail()
        return out


def get_money_flow(code):
    """个股资金流(最近若干日) → list[dict]; 失败返回 []。板块资金流 eltdx 不支持。"""
    if not enabled():
        return []
    try:
        cl = _client()
        if cl is None:
            return []
        mf = cl.money_flow.daily([_sym(code)])
        blocks = getattr(mf, "blocks", []) or []
        if not blocks:
            return []
        recs = list(getattr(blocks[0], "records", []) or [])
        out = []
        for r in recs:
            out.append({"date": str(getattr(r, "date", "")),
                        "main_net": getattr(r, "main_net", None),
                        "main_ratio": getattr(r, "main_ratio", None),
                        "total_amount": getattr(r, "total_amount", None)})
        return out
    except Exception:  # noqa: BLE001
        _mark_fail()
        return []


def shutdown() -> None:
    """关闭客户端(进程退出/测试用)。"""
    global _CLIENT
    with _LOCK:
        try:
            if _CLIENT is not None:
                _CLIENT.close()
        except Exception:  # noqa: BLE001
            pass
        _CLIENT = None
