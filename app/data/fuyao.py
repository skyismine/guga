"""同花顺金融数据 API(fuyao.aicubes.cn)客户端。

- REST 鉴权: 请求头 `X-api-key`;统一响应信封 {code, message, request_id, data},code=0 成功。
- thscode 格式: 600519.SH / 000001.SZ / 430001.BJ / ETF 510300.SH / THS指数 885525.TI。
- 所有请求走 requests,超时由 patch_requests 兜底 15s;内置内存缓存 + 请求间隔节流,
  规避上游 4001 频率超限;未启用/失败一律抛异常,由上层多源回退捕获。
- 数据语义: 毫秒 Unix 时间戳(Asia/Shanghai),价格 CNY;历史K线仅支持 1d。
"""
import json
import threading
import time

import requests

from app.support import settings as _st


def _cfg() -> dict:
    return _st.load().get("fuyao") or {}


def enabled() -> bool:
    c = _cfg()
    return bool(c.get("enabled") and c.get("api_key"))


def _base_url() -> str:
    return (_cfg().get("base_url") or "https://fuyao.aicubes.cn").rstrip("/")


def _thscode(code) -> str:
    """6 位代码 → 带交易所后缀 thscode(与 fetcher.code_to_symbol 口径一致)。"""
    code = str(code).zfill(6)
    if code.startswith(("60", "68", "50", "51", "52", "56", "58", "11", "12")):
        return f"{code}.SH"
    if code.startswith(("00", "30", "15", "16", "18")):
        return f"{code}.SZ"
    return f"{code}.BJ"


def _is_etf(code) -> bool:
    from app.data.fetcher import is_etf
    return is_etf(code)


# ------------------------------------------------------------------ 请求层(缓存 + 节流)
_CACHE = {}
_LOCK = threading.Lock()
_LAST_CALL = 0.0


def _get(path: str, params: dict = None, ttl: int = 0):
    """GET /api/** 返回 data 容器。未启用抛 RuntimeError;业务错误按 code 抛。"""
    global _LAST_CALL
    if not enabled():
        raise RuntimeError("fuyao 未启用(settings.fuyao.enabled)")
    key = json.dumps([path, params], ensure_ascii=False, sort_keys=True)
    with _LOCK:
        hit = _CACHE.get(key)
        if hit and time.time() - hit["t"] < ttl:
            return hit["v"]
        # 节流:距上次请求不足 qps_gap 时等待
        gap = float(_cfg().get("qps_gap", 0.3) or 0.3)
        wait = _LAST_CALL + gap - time.time()
        if wait > 0:
            time.sleep(wait)
        _LAST_CALL = time.time()
    r = requests.get(_base_url() + path, params=params,
                     headers={"X-api-key": _cfg().get("api_key", "")}, timeout=15)
    try:
        data = r.json()
    except ValueError:
        raise RuntimeError(f"fuyao {path} 响应非JSON: HTTP {r.status_code}")
    if data.get("code") != 0:
        raise RuntimeError(f"fuyao {path} 业务错误 code={data.get('code')}: {data.get('message')}")
    with _LOCK:
        _CACHE[key] = {"t": time.time(), "v": data.get("data")}
    return data.get("data")


# ------------------------------------------------------------------ 业务封装
def _ms_range(days: int) -> tuple:
    """近 days 个自然日的毫秒时间戳窗口(end=now)。"""
    end = int(time.time() * 1000)
    start = end - days * 86400000
    return start, end


def get_daily_history(code, days: int = 600, adjust: str = "forward"):
    """单只日线 DataFrame(index=date, columns open/high/low/close/volume/amount)。

    股票走 /api/a-share/prices/historical;ETF 走 /api/fund/market/historical(仅支持 1d)。
    """
    import pandas as pd
    ths = _thscode(code)
    start, end = _ms_range(int(days * 1.6) + 30)
    if _is_etf(code):
        data = _get("/api/fund/market/historical",
                    {"thscode": ths, "interval": "1d", "start": start, "end": end})
    else:
        data = _get("/api/a-share/prices/historical",
                    {"thscode": ths, "interval": "1d", "start": start, "end": end,
                     "adjust": adjust})
    items = data.get("item") or [] if isinstance(data, dict) else []
    if not items:
        raise RuntimeError(f"fuyao {ths} 历史K线为空")
    df = pd.DataFrame(items)
    df["date"] = pd.to_datetime(df["date_ms"], unit="ms", utc=True).dt.tz_convert(
        "Asia/Shanghai").dt.tz_localize(None)
    df = df.rename(columns={"open_price": "open", "high_price": "high",
                            "low_price": "low", "close_price": "close"})
    df = df.set_index("date").sort_index()
    df = df[["open", "high", "low", "close", "volume", "turnover"]]
    df["amount"] = df["turnover"]  # 成交额(元),fuyao turnover 口径
    df = df.drop(columns=["turnover"])
    return df.tail(days)


def get_limit_up_pool(page: int = 1, size: int = 100):
    """涨停池(含连板数/封单额/涨停原因/涨停时间)。"""
    data = _get("/api/a-share/special-data/limit-up-pool",
                {"page": page, "size": size}, ttl=300)
    return (data or {}).get("item") or []


def get_limit_down_pool(page: int = 1, size: int = 100):
    """跌停池(含首次/最后跌停时间/换手率)。"""
    data = _get("/api/a-share/special-data/limit-down-pool",
                {"page": page, "size": size}, ttl=300)
    return (data or {}).get("item") or []


def _pool_total(path: str, ttl: int = 300) -> int:
    """涨跌停/炸板股票池总家数(分页信息 total,单页请求即可取到)。"""
    data = _get(path, {"page": 1, "size": 200}, ttl=ttl)
    return int(((data or {}).get("pagination") or {}).get("total") or 0)


def get_limit_up_pool_total(ttl: int = 300) -> int:
    """当日涨停家数(涨停池 total;date_ms 省略=当前自然日,交易时段实时)。"""
    return _pool_total("/api/a-share/special-data/limit-up-pool", ttl=ttl)


def get_limit_down_pool_total(ttl: int = 300) -> int:
    """当日跌停家数(跌停池 total;date_ms 省略=当前自然日,交易时段实时)。"""
    return _pool_total("/api/a-share/special-data/limit-down-pool", ttl=ttl)


def get_limit_up_ladder():
    """近30交易日连板梯队矩阵(供复盘梯队完整度)。"""
    data = _get("/api/a-share/special-data/limit-up-ladder", {}, ttl=3600)
    return (data or {}).get("item") or []


def get_hot_stock_list(period: str = "day"):
    """同花顺 A 股热股榜 Top30(day=24小时榜 / hour=小时榜)。"""
    data = _get("/api/a-share/special-data/hot-stock-list", {"period": period}, ttl=1800)
    return (data or {}).get("item") or []


def get_hot_stock_list_history(date: str):
    """历史热股榜排行(date=YYYY-MM-DD,近一年)。"""
    data = _get("/api/a-share/special-data/hot-stock-list-history", {"date": date}, ttl=86400)
    return (data or {}).get("item") or []


def get_skyrocket_list(period: str = "day"):
    """热度排名飙升榜 Top30(day 日榜 / hour 小时榜)。"""
    data = _get("/api/a-share/special-data/skyrocket-list", {"period": period}, ttl=1800)
    return (data or {}).get("item") or []


def get_dragon_tiger_list(board_type: str = "all"):
    """龙虎榜榜单(all 全部 / org 机构榜 / hot_money 游资榜)。"""
    data = _get("/api/a-share/special-data/dragon-tiger-list", {"board_type": board_type}, ttl=1800)
    return (data or {}) if isinstance(data, dict) else {}


def get_anomaly_analysis_list(tag_codes: str = None):
    """当日个股异动原因列表(可选 tag_codes: LIMIT_UP/LIMIT_DOWN/SHARP_RISE/SHARP_FALL...)。"""
    params = {"tag_codes": tag_codes} if tag_codes else {}
    data = _get("/api/a-share/special-data/anomaly-analysis-list", params, ttl=1800)
    return (data or {}).get("item") or []


def get_ths_index_list(tag: str = "cn_concept"):
    """同花顺指数目录(概念/行业/区域/特色)。"""
    data = _get("/api/a-share-index/catalog/ths-index-list", {"tag": tag}, ttl=86400)
    return (data or {}).get("item") or []


def get_ths_constituents(ths: str):
    """同花顺指数成分股清单(支持 885525.TI 与 000300.SH)。"""
    data = _get("/api/a-share-index/constituents/ths-stock-list", {"thscode": ths}, ttl=86400)
    return (data or {}).get("item") or []


def ths_index_map(tag: str = "cn_concept") -> dict:
    """同花顺指数 名称->thscode 映射(供按概念名取成分/指数日线)。"""
    return {it.get("name"): it.get("thscode")
            for it in get_ths_index_list(tag) if it.get("thscode")}


def get_ths_index_daily(ths: str, days: int = 400):
    """同花顺指数日线 DataFrame(index=date, close 等)。"""
    import pandas as pd
    start, end = _ms_range(int(days * 1.6) + 30)
    data = _get("/api/a-share-index/prices/historical",
                {"thscode": ths, "interval": "1d", "start": start, "end": end}, ttl=3600)
    items = (data or {}).get("item") or []
    if not items:
        raise RuntimeError(f"fuyao 指数 {ths} 历史K线为空")
    df = pd.DataFrame(items)
    df["date"] = pd.to_datetime(df["date_ms"], unit="ms", utc=True).dt.tz_convert(
        "Asia/Shanghai").dt.tz_localize(None)
    df = df.rename(columns={"open_price": "open", "high_price": "high",
                            "low_price": "low", "close_price": "close"})
    df = df.set_index("date").sort_index()
    return df[["open", "high", "low", "close", "volume", "turnover"]]


def get_index_snapshot(thscodes: str) -> list:
    """指数行情快照(批量, 含收盘/涨跌幅/成交额), 权威收盘口径。"""
    data = _get("/api/a-share-index/prices/snapshot", {"thscodes": thscodes}, ttl=600)
    return (data or {}).get("item") or []


def get_stock_snapshot(thscodes: list, ttl: int = 30) -> dict:
    """A股行情快照(批量, 每批 ≤500)。返回 {6位代码: 快照项}。

    用于全市场盘中涨跌家数:单批过大(>500)会被上游 400 拒绝,自动分批。
    """
    items = []
    for i in range(0, len(thscodes), 500):
        batch = thscodes[i:i + 500]
        data = _get("/api/a-share/prices/snapshot",
                    {"thscodes": ",".join(batch)}, ttl=ttl)
        items.extend((data or {}).get("item") or [])
    return {str(it.get("ticker"))[-6:]: it for it in items if it.get("ticker")}


def get_market_snapshot(limit: int = 5000, ttl: int = 30) -> list:
    """全市场行情快照(官方分页模式,一次取全 A 股)。

    省略 thscodes 时上游按 thscode 升序遍历完整 A 股代码表并按 limit/offset 分页;
    实测 limit=5000 时仅 2 次请求即可取完 5000+ 只,替代逐批 thscodes 查询。
    """
    items, offset = [], 0
    while True:
        data = _get("/api/a-share/prices/snapshot",
                    {"limit": limit, "offset": offset}, ttl=ttl)
        page = (data or {}).get("item") or []
        items.extend(page)
        if len(page) < limit:
            break
        offset += limit
    return items


def get_calendar_trading_days():
    """近一年交易日序列(升序)。"""
    data = _get("/api/a-share/calendar/trading-days", {}, ttl=86400)
    return (data or {}).get("item") or []


def get_market_dump_url(kind: str = "daily-k") -> str:
    """全市场 Parquet 预签名下载链接(短时效,取后立即下载)。

    kind: daily-k(10年日K) / daily-k-10d(近10交易日) / adjustment-factors(复权因子)。
    """
    path = {
        "daily-k": "/api/dump/market-dumps/daily-k/download-url",
        "daily-k-10d": "/api/dump/market-dumps/daily-k-10d/download-url",
        "adjustment-factors": "/api/dump/market-dumps/adjustment-factors/download-url",
    }[kind]
    data = _get(path, {}, ttl=0) or {}
    url = (data.get("presigned_url") or "").strip()
    if not url:
        raise RuntimeError(f"fuyao 未返回 {kind} 下载链接")
    return url
