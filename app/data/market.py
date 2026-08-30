"""市场情绪数据:股指期货资金/基差、指数行情、全市场涨跌家数(乐咕乐股)。

- 期指资金:IF/IH/IC/IM 连续合约历史日线 + 实时快照,基差 = 期货价/现货指数 - 1
- 涨跌家数:fuyao 全市场日K(收盘口径,优先) + 乐咕乐股(盘中实时,回退)
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

# 盘中实时数据短 TTL:乐咕涨跌家数盘中持续变化,60s 内复用避免频繁抓取
_INTRADAY_TTL = 60


def _is_trading_time(now=None) -> bool:
    """是否处于交易时段(工作日 9:30-11:30 / 13:00-15:00)。"""
    now = now or dt.datetime.now()
    if now.weekday() >= 5:
        return False
    hm = now.hour * 60 + now.minute
    return (9 * 60 + 30) <= hm <= (11 * 60 + 30) or (13 * 60) <= hm <= (15 * 60)


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
    amount = 0.0
    try:
        amount = float(parts[9]) if len(parts) > 9 else 0.0
    except (TypeError, ValueError, IndexError):
        pass
    m2 = re.search(r"(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2})", resp.text)
    return {"symbol": symbol, "name": name, "open": open_, "prev_close": prev_close,
            "price": price, "high": high, "low": low, "pct_chg": pct,
            "amount": amount,
            "datetime": f"{m2.group(1)} {m2.group(2)}" if m2 else ""}


def get_two_market_amount() -> Optional[float]:
    """两市当日成交额(元):上证指数+深证成指(新浪实时)。失败返回 None。"""
    try:
        sh = get_index_spot("sh000001")
        sz = get_index_spot("sz399001")
        if sh.get("amount") and sz.get("amount"):
            return sh["amount"] + sz["amount"]
    except Exception:  # noqa: BLE001
        pass
    return None


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
    """四大期指实时快照(连续合约 IF0/IH0/IC0/IM0)+ 对现货的实时基差。

    交易时段基差盘中持续变化,短 TTL(60s)刷新;非交易时段用长 TTL(4h)。
    """
    if use_cache:
        ttl = _INTRADAY_TTL if _is_trading_time() else config.CACHE_TTL_SECONDS
        cached = _load_cache("futures_rt", ttl=ttl)
        if cached is not None:
            return cached
    import akshare as ak
    result = {}
    errors = []
    for key, variety in config.FUTURES_VARIETY.items():
        df = None
        for attempt in range(2):  # 间歇性 JSON 解析失败,轻量重试一次
            try:
                df = ak.futures_zh_realtime(symbol=variety)
                break
            except Exception as e:  # noqa: BLE001
                if attempt == 0:
                    errors.append(f"{key}:{type(e).__name__}")
                else:
                    errors.append(f"{key}:{type(e).__name__}")
                time.sleep(0.4)
        if df is None or df.empty:
            continue
        try:
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
            errors.append(f"{key}:{type(e).__name__}")
    if errors and len(errors) < len(config.FUTURES_VARIETY):
        print(f"[market] 期指部分获取失败: {errors}")
    if result:  # 接口失败返回空时不污染缓存,避免盘中一直命中空快照
        _save_cache("futures_rt", result)
    return result


# ---------------------------------------------------------------- 涨跌家数(fuyao 全市场日K)
_FUYAO_DUMP_PAT = re.compile(r"/releases/(\d{8})/")


def _fuyao_dump_path() -> str:
    """确定全市场日K(近10交易日)本地文件路径,缺失时下载。"""
    from app.data.fuyao import get_market_dump_url as _fy_url
    url = _fy_url("daily-k-10d")
    m = _FUYAO_DUMP_PAT.search(url)
    release = m.group(1) if m else dt.date.today().strftime("%Y%m%d")
    path = os.path.join(config.DATA_DIR, f"market_dump_10d_{release}.parquet")
    if not os.path.exists(path):
        print(f"[market] 下载 fuyao 全市场日K(近10交易日,{release})...")
        with requests.get(url, stream=True, timeout=120) as r:
            r.raise_for_status()
            with open(path, "wb") as f:
                for chunk in r.iter_content(1 << 20):
                    f.write(chunk)
    return path


def _fuyao_dump_df() -> pd.DataFrame:
    """读取 fuyao 全市场日K Parquet(index 保持原样,列 thscode/date_ms/close_price)。"""
    import pyarrow.parquet as pq
    return pq.read_table(_fuyao_dump_path(),
                         columns=["thscode", "date_ms", "close_price"]).to_pandas()


def _breadth(close: "pd.Series", prev: "pd.Series", thscodes: "pd.Series",
             date: str, source: str) -> Dict:
    """由 {股票: 现价/收盘} 与 {股票: 昨收} 计算涨跌家数/涨停跌停(统一口径)。

    涨停/跌停判定:现价是否触及涨跌停价(主板10%/创业科创板20%/北交所30%)。
    """
    ret = close / prev - 1
    ratio = pd.Series(1.10, index=close.index)
    ratio[thscodes.str.startswith(("30", "68"))] = 1.20
    ratio[thscodes.str.endswith(".BJ")] = 1.30
    up = int((ret > 0).sum())
    down = int((ret < 0).sum())
    flat = int((ret == 0).sum())
    limit_up = int((close >= (prev * ratio).round(2) - 1e-9).sum())
    limit_down = int((close <= (prev * (2 - ratio)).round(2) + 1e-9).sum())
    total = up + down + flat
    return {
        "advance": up, "decline": down, "flat": flat, "suspended": 0,
        "limit_up": limit_up, "real_limit_up": limit_up,
        "limit_down": limit_down, "real_limit_down": limit_down,
        "activity_pct": round(up / total * 100, 2) if total else 0.0,
        "date": date,
        "adv_ratio": round(up / (up + down), 4) if (up + down) else None,
        "source": source,
    }


def _activity_from_fuyao() -> Dict:
    """全市场涨跌家数(fuyao 全市场日K,按最新交易日收盘口径计算)。

    与乐咕口径差异(注释说明):
    - 收盘口径:盘中不更新,反映最新完整交易日(fuyao daily-k 为日终快照);
    - 停牌股无当日 close 行、新上市无 prev_close → 均不参与计数;
    - 涨停/跌停按收盘价是否触及涨跌停价判定(≈乐咕"真实涨停/跌停"),
      "触板未封" 的涨停(乐咕统计口径)无法从日K还原,归入 real_limit_up 同值;
    - 无名称字段无法识别 ST,涨跌停价按主板10%/创业科创板20%/北交所30% 估算,
      ST(5%)样本占全市场比例极小,对计数影响可忽略。
    """
    from app.data.fuyao import enabled as _fy_enabled
    if not _fy_enabled():
        raise RuntimeError("fuyao 未启用(settings.fuyao.enabled)")
    df = _fuyao_dump_df()
    df["date"] = pd.to_datetime(df["date_ms"], unit="ms").dt.tz_localize(
        "UTC").dt.tz_convert("Asia/Shanghai").dt.strftime("%Y-%m-%d")
    last_date = str(df["date"].max())
    # prev_close 需在整个窗口内逐股 shift,先算再截取最新交易日
    df["prev_close"] = df.groupby("thscode")["close_price"].shift(1)
    df = df[df["date"] == last_date].dropna(subset=["prev_close"])
    return _breadth(df["close_price"], df["prev_close"], df["thscode"],
                    last_date, "fuyao")


def _activity_from_fuyao_realtime() -> Dict:
    """盘中实时涨跌家数/涨停跌停(fuyao 全市场快照 + 官方涨跌停池)。

    仅交易时段调用;非交易时段 snapshot 回落为上一收盘价,故非盘中仍走 _activity_from_fuyao。
    - 涨跌家数:全市场快照官方分页(每页 5000,约 2 次请求取全 A 股,短 TTL 盘中刷新);
    - 涨停/跌停:官方涨跌停池 total(省略 date_ms=当前自然日,交易时段实时,权威且含
      ST/次新标注);total=0(上游未就绪)或请求失败时,保留快照涨跌停价阈值估算。
    """
    from app.data.fuyao import enabled as _fy_enabled
    from app.data.fuyao import get_market_snapshot as _fy_snap
    from app.data.fuyao import get_limit_up_pool_total as _fy_lu
    from app.data.fuyao import get_limit_down_pool_total as _fy_ld
    if not _fy_enabled():
        raise RuntimeError("fuyao 未启用(settings.fuyao.enabled)")
    items = _fy_snap(limit=5000, ttl=_INTRADAY_TTL)
    df = pd.DataFrame(items)
    if df.empty:
        raise RuntimeError("fuyao 全市场快照为空")
    df = df.dropna(subset=["last_price", "prev_price"])
    if (df["last_price"] <= 0).all():
        raise RuntimeError("fuyao 全市场快照无有效报价")
    today = dt.datetime.now().strftime("%Y-%m-%d")
    result = _breadth(df["last_price"], df["prev_price"], df["thscode"],
                      today, "fuyao_rt")
    for key, fn in (("limit_up", _fy_lu), ("limit_down", _fy_ld)):
        try:
            n = int(fn())
            if n > 0:
                result[key] = result[f"real_{key}"] = n
        except Exception as e:  # noqa: BLE001
            print(f"[market] fuyao {key} 池获取失败,保留快照估算: {e}")
    return result


def _activity_cache_ttl(cached: Dict) -> int:
    """活动度缓存 TTL(秒):交易时段短刷新(60s),非交易时段长 TTL(4h)。

    交易时段无论缓存来自实时还是收盘口径都 60s 刷新,便于盘中持续跟进;
    非交易时段数据已定型,4h 内复用。
    """
    return _INTRADAY_TTL if _is_trading_time() else config.CACHE_TTL_SECONDS


def get_market_activity(use_cache: bool = True) -> Dict:
    """全市场当日涨跌家数/涨停跌停/活跃度。

    数据源优先级:
    P1 交易时段:fuyao 全市场实时快照(盘中 60s 刷新);
    P2 任意时段:fuyao 全市场日K(收盘口径,最新完整交易日);
    P3 乐咕乐股(原源,盘中 60s 实时)——前两级失败时回退。
    """
    if use_cache:
        cached = _load_cache("activity", ttl=None)
        if cached is not None:
            ttl = _activity_cache_ttl(cached)
            if time.time() - os.path.getmtime(_path("activity")) <= ttl:
                return cached
    if _is_trading_time():
        try:
            result = _activity_from_fuyao_realtime()
        except Exception as e:  # noqa: BLE001
            print(f"[market] fuyao 实时涨跌家数获取失败,回退收盘口径: {e}")
        else:
            _save_cache("activity", result)
            return result
    try:
        result = _activity_from_fuyao()
    except Exception as e:  # noqa: BLE001
        print(f"[market] fuyao 涨跌家数获取失败,回退乐咕: {e}")
    else:
        _save_cache("activity", result)
        return result
    # P3 回退: 乐咕乐股(原源;页面反爬/结构变化时可能抛异常)
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
