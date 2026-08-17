"""A股每日复盘 · 数据采集层:收集指数/板块/资金/情绪/事件数据。

数据源(当前网络环境验证可用):
- 指数全景:sina stock_zh_index_daily + 新浪 hq 实时(收盘后为当日收盘);
- 涨跌停家数:乐咕乐股 get_market_activity;
- 涨停池明细:东财 push2ex stock_zt_pool_em(连板/炸板/封板资金/所属行业);
- 板块资金流:同花顺 stock_fund_flow_concept('即时')(概念板块净流入);
- 北向资金:东财 stock_hsgt_fund_flow_summary_em;
- 大盘资金(可选):stock_market_fund_flow;
- 近10日市场日度(量能/主力资金/涨停):东财 push2his kline + fflow + stock_zt_pool_em(支持历史交易日),涨跌家数当日取乐咕并逐日存档累积;
- 事件:东财要闻 stock_info_global_em + 央视联播 news_cctv。

每类数据独立容错 + 本地缓存 + 重试,单个失败不影响整体复盘。
"""
import datetime as dt
import json
import os
import pickle
import re
import time
from typing import Dict, List, Optional

import pandas as pd
import requests

from app import config

# 强制直连(绕开系统代理对 eastmoney 的干扰),放行后 akshare 各源直连
try:
    import requests as _req
    _req.sessions.Session.trust_env = False
except Exception:
    pass

_REVIEW_DIR = os.path.join(config.DATA_DIR, "review")
os.makedirs(_REVIEW_DIR, exist_ok=True)

# 大盘指数全景(sina 代码, 中文名)
MAJOR_INDICES = [
    ("sh000001", "上证指数"), ("sz399001", "深证成指"), ("sz399006", "创业板指"),
    ("sh000300", "沪深300"), ("sh000016", "上证50"), ("sh000905", "中证500"),
    ("sh000852", "中证1000"), ("sh000688", "科创50"),
]

_SINA_HQ = "https://hq.sinajs.cn/list={symbols}"
_SINA_REFERER = "https://finance.sina.com.cn"

_KEYWORDS = ("央行", "国务院", "证监会", "国常会", "降准", "降息", "LPR", "MLF",
             "政策", "关税", "美联储", "欧央行", "出口", "半导体", "芯片", "AI",
             "人工智能", "新能源", "光伏", "锂电", "汽车", "地产", "消费", "医药",
             "科技", "北向", "外资", "IPO", "注册制", "量化", "两融", "印花税",
             "退市", "并购", "重组", "涨价", "减产", "招标", "订单", "业绩预告")


def _cache_path(key: str) -> str:
    return os.path.join(_REVIEW_DIR, f"{key}.pkl")


def _load_cache(key: str, ttl: int = None) -> Optional[object]:
    p = _cache_path(key)
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
    try:
        with open(_cache_path(key), "wb") as f:
            pickle.dump(obj, f)
    except OSError:
        pass


def _retry(fn, n: int = 3, slp: float = 1.5):
    last = None
    for i in range(n):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(slp)
    raise last


# ---------------------------------------------------------------- 复盘交易日
def review_date() -> dt.date:
    """最近一个交易日(取上证指数日线的最后日期)。"""
    import akshare as ak
    cache = _load_cache("idx_sh000001", ttl=config.CACHE_TTL_SECONDS * 6)
    if cache is not None and len(cache):
        return pd.Timestamp(cache.index[-1]).date()
    df = ak.stock_zh_index_daily(symbol="sh000001")
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna(subset=["date"]).set_index("date")
    _save_cache("idx_sh000001", df)
    return pd.Timestamp(df.index[-1]).date()


# ---------------------------------------------------------------- 指数全景
def _sina_spot_batch(symbols) -> Dict[str, Dict]:
    """新浪 hq 批量实时快照(收盘后为当日收盘价)。"""
    out = {}
    resp = requests.get(_SINA_HQ.format(symbols=",".join(symbols)),
                        headers={"Referer": _SINA_REFERER}, timeout=10)
    resp.encoding = "gbk"
    for m in re.finditer(r'hq_str_([a-z]{2}\d{6})="([^"]*)"', resp.text):
        code, payload = m.group(1), m.group(2)
        parts = payload.split(",")
        if len(parts) < 5 or not parts[1]:
            continue
        try:
            name = parts[0]
            prev_close, price = float(parts[2]), float(parts[3])
            pct = (price - prev_close) / prev_close if prev_close else 0.0
            out[code] = {"symbol": code, "name": name, "price": price,
                         "pct_chg": pct, "prev_close": prev_close}
        except (TypeError, ValueError):
            continue
    return out


def collect_indices(date: dt.date = None) -> list:
    """各大盘指数收盘点数/涨跌幅。优先新浪实时(收盘后=当日收盘),失败回退日线。"""
    symbols = [s for s, _ in MAJOR_INDICES]
    try:
        spots = _retry(lambda: _sina_spot_batch(symbols), n=2)
    except Exception:
        spots = {}
    rows = []
    for sym, name in MAJOR_INDICES:
        item = {"symbol": sym, "name": name}
        sp = spots.get(sym)
        if sp:
            item.update({"close": sp["price"], "pct_chg": sp["pct_chg"]})
        else:
            try:
                df = _retry(lambda: _index_daily(sym))
                if len(df) >= 2:
                    c0 = float(df["close"].iloc[-2])
                    c1 = float(df["close"].iloc[-1])
                    item.update({"close": c1, "pct_chg": (c1 / c0 - 1)})
            except Exception:
                pass
        if "close" in item:
            rows.append(item)
    return rows


def _index_daily(symbol: str) -> pd.DataFrame:
    import akshare as ak
    cache = _load_cache(f"idx_{symbol}", ttl=config.CACHE_TTL_SECONDS * 6)
    if cache is not None and len(cache) >= 3:
        return cache
    df = ak.stock_zh_index_daily(symbol=symbol)
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna(subset=["date", "close"]).set_index("date").sort_index()
    _save_cache(f"idx_{symbol}", df)
    return df


# ---------------------------------------------------------------- 涨跌停 + 市场宽度
def collect_activity() -> Dict:
    """涨跌家数/涨停跌停家数(乐咕)。失败返回空 dict,由上层降级。"""
    try:
        from app.data.market import get_market_activity
        return _retry(lambda: get_market_activity(use_cache=True))
    except Exception:
        return {}


# ---------------------------------------------------------------- 涨停池明细
def collect_limit_up(date: dt.date) -> Dict:
    """涨停池:连板高度/炸板率/封板资金/行业分布。"""
    import akshare as ak
    d = date
    result = {"date": str(d), "ok": False}
    for _ in range(5):
        try:
            df = _retry(lambda: ak.stock_zt_pool_em(date=d.strftime("%Y%m%d")), n=2)
            if df is not None and len(df):
                result.update(_summarize_zt(df))
                result["date"] = str(d)
                return result
        except Exception:
            pass
        d -= dt.timedelta(days=1)
    return result


def _summarize_zt(df: pd.DataFrame) -> Dict:
    col_map = {c: c for c in df.columns}
    def g(*names):
        for n in names:
            for c in df.columns:
                if n in str(c):
                    return c
        return None
    c_lian = g("连板数") or g("连续涨停天数")
    c_stat = g("涨停统计")
    c_zhadan = g("炸板次数")
    c_money = g("封板资金")
    c_ind = g("所属行业")
    c_pct = g("涨跌幅")

    lians = pd.to_numeric(df[c_lian], errors="coerce") if c_lian else pd.Series(dtype=float)
    stats = pd.to_numeric(df[c_stat], errors="coerce") if c_stat else pd.Series(dtype=float)
    total = int(len(df))
    max_lian = int(lians.max()) if len(lians) and pd.notna(lians.max()) else 0
    max_stat = int(stats.max()) if len(stats) and pd.notna(stats.max()) else 0
    zhadan_total = int(pd.to_numeric(df[c_zhadan], errors="coerce").sum()) if c_zhadan else 0
    inds = df[c_ind].value_counts().head(8).to_dict() if c_ind else {}
    money = pd.to_numeric(df[c_money], errors="coerce") if c_money else pd.Series(dtype=float)
    total_money = float(money.sum()) if len(money) else 0.0
    top_money = (df.assign(_m=money).sort_values("_m", ascending=False)
                 .head(5) if c_money else df.head(0))
    leaders = []
    if c_money and len(top_money):
        name_col = g("名称") or g("代码")
        pct_col = c_pct
        for _, r in top_money.iterrows():
            leaders.append({
                "code": str(r.get("代码", r.get("名称", ""))),
                "name": str(r.get("名称", "")),
                "lian": int(r.get(c_lian, 0)) if c_lian and pd.notna(r.get(c_lian, 0)) else 0,
                "money_yi": round(float(r.get(c_money, 0)) / 1e8, 2),
            })
    return {
        "ok": True, "total": total, "max_lian": max_lian, "max_stat": max_stat,
        "zhadan_total": zhadan_total, "total_money_yi": round(total_money / 1e8, 2),
        "industries": {str(k): int(v) for k, v in list(inds.items())[:8]},
        "leaders": leaders,
    }


# ---------------------------------------------------------------- 板块资金流(同花顺概念板块)
_SF_FLOW_TTL = 120   # 秒:单日概念资金流快照缓存有效期(短,保证盘中较新)
_SF_FLOW5_TTL = 300  # 秒:5日概念资金流缓存有效期(5日数据盘中变化小)

def _sf_load(key: str, ttl: int) -> Optional[list]:
    """读取板块资金流缓存;命中则直接返回(不触发网络)。"""
    return _load_cache(key, ttl=ttl)


def _sf_save(key: str, rows: list) -> None:
    try:
        _save_cache(key, rows)
    except Exception:  # noqa: BLE001
        pass


def _sf_fallback(key: str, src: str, err) -> list:
    """实时抓取失败时回退最近一次成功快照(任意年龄,含盘中),并告警。"""
    stale = _load_cache(key, ttl=None)
    if stale:
        print(f"[review] {src} 实时抓取失败,回退最近快照({len(stale)} 条): {err}")
        return stale
    raise err


def collect_sector_flow(use_cache: bool = True) -> list:
    """概念板块资金净流入(净额,亿元)+ 涨跌幅 + 领涨股。

    带本地快照缓存(_SF_FLOW_TTL)+ 失败回退最近快照:同花顺限流/抖动时
    不阻塞整个轮询周期(稳定器依赖此数据,失败会整轮失败)。
    """
    if use_cache:
        hit = _sf_load("sector_flow", _SF_FLOW_TTL)
        if hit is not None:
            return hit
    import akshare as ak
    try:
        df = _retry(lambda: ak.stock_fund_flow_concept(symbol="即时"), n=2, slp=0.8)
    except Exception as e:  # noqa: BLE001
        return _sf_fallback("sector_flow", "同花顺概念资金流", e)
    df = df.rename(columns={df.columns[i]: c for i, c in enumerate(
        ["no", "industry", "index_name", "pct", "inflow", "outflow",
         "net", "num", "leader", "leader_pct", "leader_price"])})
    net = pd.to_numeric(df["net"], errors="coerce")   # 单位:亿元
    pct = pd.to_numeric(df["pct"], errors="coerce")
    rows = []
    for _, r in df.iterrows():
        rows.append({
            "industry": str(r["industry"]),
            "pct_chg": float(pct.get(r.name, 0)) if pd.notna(pct.get(r.name)) else 0.0,
            "net_yi": round(float(net.get(r.name, 0) or 0), 2),   # 已是亿元
            "inflow_yi": round(float(r.get("inflow", 0) or 0), 2),
            "outflow_yi": round(float(r.get("outflow", 0) or 0), 2),
            "num": int(r["num"]) if pd.notna(r["num"]) else 0,
            "leader": str(r["leader"]),
            "leader_pct": float(r["leader_pct"]) if pd.notna(r["leader_pct"]) else 0.0,
        })
    rows.sort(key=lambda x: x["net_yi"], reverse=True)
    _sf_save("sector_flow", rows)
    return rows


def collect_sector_flow_5d(use_cache: bool = True) -> list:
    """概念板块 5 日主力资金流(5日累计净流入/流入/流出,亿元)+ 5日累计涨跌幅。"""
    if use_cache:
        hit = _sf_load("sector_flow_5d", _SF_FLOW5_TTL)
        if hit is not None:
            return hit
    import akshare as ak
    try:
        df = _retry(lambda: ak.stock_fund_flow_concept(symbol="5日排行"), n=2, slp=0.8)
    except Exception as e:  # noqa: BLE001
        return _sf_fallback("sector_flow_5d", "同花顺5日概念资金流", e)
    df = df.rename(columns={df.columns[i]: c for i, c in enumerate(
        ["no", "industry", "num", "index_name", "pct", "inflow", "outflow", "net"])})
    net = pd.to_numeric(df["net"], errors="coerce")
    inflow = pd.to_numeric(df["inflow"], errors="coerce")
    outflow = pd.to_numeric(df["outflow"], errors="coerce")
    pct = pd.to_numeric(df["pct"].astype(str).str.replace("%", "", regex=False), errors="coerce")
    rows = []
    for _, r in df.iterrows():
        rows.append({
            "industry": str(r["industry"]),
            "pct_5d": round(float(pct.get(r.name, 0) or 0), 2),      # 5日累计涨跌幅(%)
            "inflow_5d_yi": round(float(inflow.get(r.name, 0) or 0), 2),
            "outflow_5d_yi": round(float(outflow.get(r.name, 0) or 0), 2),
            "net_5d_yi": round(float(net.get(r.name, 0) or 0), 2),   # 5日累计净流入(亿元)
        })
    rows.sort(key=lambda x: x["net_5d_yi"], reverse=True)
    _sf_save("sector_flow_5d", rows)
    return rows


# ---------------------------------------------------------------- 北向资金
def collect_north(date: dt.date = None) -> Dict:
    """沪深港通北向/南向资金。注:2024 年起交易所停止披露北向单日实时净买入,
    东财该字段为 0,故以北向是否可得作标记,并保留南向作参考。单位:亿元。"""
    import akshare as ak
    df = _retry(lambda: ak.stock_hsgt_fund_flow_summary_em())
    north, south = [], []
    for _, r in df.iterrows():
        direction = str(r.get("资金方向", ""))
        block = str(r.get("板块", ""))
        try:
            net = float(r.get("成交净买额", r.get("资金净流入", 0)) or 0)
        except (TypeError, ValueError):
            net = 0.0
        target = north if direction == "北向" else south if direction == "南向" else None
        if target is not None and block:
            target.append({"block": block, "net_yi": round(net, 2)})
    return {
        "north_total_yi": round(sum(b["net_yi"] for b in north), 2),
        "north_available": any(b["net_yi"] != 0 for b in north),
        "north_blocks": north,
        "south_total_yi": round(sum(b["net_yi"] for b in south), 2),
        "south_blocks": south,
    }


# ---------------------------------------------------------------- 大盘资金(可选)
def collect_market_fund() -> Dict:
    try:
        import akshare as ak
        df = _retry(lambda: ak.stock_market_fund_flow(), n=2)
        rows = []
        for _, r in df.iterrows():
            try:
                net = float(r.get("主力净流入-净额", 0) or 0)
                pct = str(r.get("主力净流入-净占比", "0")).replace("%", "")
                rows.append({"name": str(r["名称"]), "net_yi": round(net / 1e8, 2),
                             "net_pct": float(pct or 0)})
            except (TypeError, ValueError):
                continue
        return {"ok": True, "rows": rows}
    except Exception:
        return {"ok": False, "rows": []}


# ---------------------------------------------------------------- 近 N 日市场日度数据
_EM_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"),
    "Referer": "http://quote.eastmoney.com/",
}
_HIST_FILE = os.path.join(_REVIEW_DIR, "daily_history.json")
_MD_CACHE = {"t": 0.0, "data": None}


def _em_json(url: str, n: int = 3) -> Dict:
    """东财 push2his 直连 JSON(指数日线/资金流历史)。"""
    last = None
    for i in range(n):
        try:
            r = requests.get(url, headers=_EM_HEADERS, timeout=12)
            j = r.json()
            if j is not None and j.get("data") is not None:
                return j
            last = ValueError(f"eastmoney 返回空数据: rc={j.get('rc') if j else None}")
        except Exception as e:  # noqa: BLE001
            last = e
        time.sleep(1.0)
    raise last


def _em_kline_rows(secid: str, lmt: int = 12) -> Dict[str, Dict]:
    """东财指数日线: {date: {close, pct_chg, amount}}。fields2: 日期/开/收/高/低/量/额/振幅/涨跌幅。"""
    url = ("http://push2his.eastmoney.com/api/qt/stock/kline/get"
           f"?secid={secid}&klt=101&fqt=1&lmt={lmt}&end=20500101"
           "&fields1=f1,f2,f3,f4,f5,f6&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59")
    j = _em_json(url)
    out = {}
    for line in (j.get("data") or {}).get("klines") or []:
        p = line.split(",")
        if len(p) < 9:
            continue
        try:
            out[p[0]] = {"close": float(p[2]), "pct_chg": float(p[8]), "amount": float(p[6])}
        except (TypeError, ValueError):
            continue
    return out


def _em_fflow_rows(secid: str, lmt: int = 12) -> Dict[str, float]:
    """东财指数主力资金流历史: {date: 主力净流入(元)}。f52 为当日主力净流入。"""
    url = ("http://push2his.eastmoney.com/api/qt/stock/fflow/kline/get"
           f"?lmt={lmt}&klt=101&secid={secid}"
           "&fields1=f1,f2,f3,f7&fields2=f51,f52,f53,f54,f55,f56"
           "&ut=b2884a393a59ad64002292a3e90d46a5")
    j = _em_json(url)
    out = {}
    for line in (j.get("data") or {}).get("klines") or []:
        p = line.split(",")
        if len(p) < 6:
            continue
        try:
            out[p[0]] = float(p[1])
        except (TypeError, ValueError):
            continue
    return out


def _zt_pool_count(date_str: str) -> Optional[int]:
    """东财涨停池家数(支持历史交易日)。"""
    try:
        import akshare as ak
        df = _retry(lambda: ak.stock_zt_pool_em(date=date_str.replace("-", "")), n=2)
        return int(len(df))
    except Exception:  # noqa: BLE001
        return None


def _load_history() -> Dict:
    try:
        with open(_HIST_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return {}


def _save_history(h: Dict) -> None:
    try:
        with open(_HIST_FILE, "w", encoding="utf-8") as f:
            json.dump(h, f, ensure_ascii=False, indent=1)
    except OSError:
        pass


def collect_market_daily(days: int = 10) -> List[Dict]:
    """近 N 个交易日的市场日度数据(旧→新)。

    每行: date/close(上证)/pct_chg(上证)/amount_yi(沪深两市成交额)/main_yi(沪+深指数主力净流入)/
           advance/decline/limit_up/limit_down(涨跌家数取自乐咕 + 每日存档累积,涨停家数取自东财涨停池)。
    """
    if _MD_CACHE["data"] is not None and time.time() - _MD_CACHE["t"] < 1800:
        return _MD_CACHE["data"]
    try:
        sh = _em_kline_rows("1.000001", days + 2)
        sz = _em_kline_rows("0.399001", days + 2)
    except Exception:  # noqa: BLE001
        sh, sz = {}, {}
    try:
        fsh = _em_fflow_rows("1.000001", days + 2)
        fsz = _em_fflow_rows("0.399001", days + 2)
    except Exception:  # noqa: BLE001
        fsh, fsz = {}, {}

    dates = sorted((set(sh) & set(sz)) or list(sh))[-days:]
    hist = _load_history()

    rows = []
    for d in dates:
        s, z = sh.get(d, {}), sz.get(d, {})
        row = {
            "date": d,
            "close": s.get("close"),
            "pct_chg": s.get("pct_chg"),
            "amount_yi": round(((s.get("amount") or 0) + (z.get("amount") or 0)) / 1e8, 1),
            "main_yi": round(((fsh.get(d) or 0) + (fsz.get(d) or 0)) / 1e8, 2),
            "advance": None, "decline": None,
            "limit_up": None, "limit_down": None,
        }
        h = hist.get(d)
        if h:
            for k in ("advance", "decline", "limit_up", "limit_down", "main_yi", "amount_yi"):
                if h.get(k) is not None:
                    row[k] = h[k]
        else:
            row["limit_up"] = _zt_pool_count(d)
        rows.append(row)

    # 当日数据存档(乐咕涨跌家数 + 当日量能/资金),用于未来回补近10日历史
    try:
        from app.data.market import get_market_activity
        act = get_market_activity(use_cache=True)
    except Exception:  # noqa: BLE001
        act = {}
    if act:
        today = str(act.get("date") or dt.date.today().isoformat())[:10]
        today_row = next((r for r in rows if r["date"] == today), None)
        cur = {
            "advance": act.get("advance"), "decline": act.get("decline"),
            "limit_down": act.get("limit_down"),
            "limit_up": (today_row.get("limit_up") if today_row else None) or act.get("limit_up"),
            "real_limit_up": act.get("real_limit_up"),
            "real_limit_down": act.get("real_limit_down"),
        }
        if today_row:
            cur["main_yi"] = today_row["main_yi"]
            cur["amount_yi"] = today_row["amount_yi"]
            cur["close"] = today_row["close"]
            cur["pct_chg"] = today_row["pct_chg"]
        hist[today] = {**hist.get(today, {}), **cur}
        _save_history(hist)
        if today_row:
            for k in ("advance", "decline", "limit_up", "limit_down"):
                if cur.get(k) is not None:
                    today_row[k] = cur[k]

    _MD_CACHE["data"] = rows
    _MD_CACHE["t"] = time.time()
    return rows


# ---------------------------------------------------------------- 事件(要闻 + 联播)
def collect_events(date: dt.date = None) -> Dict:
    import akshare as ak
    news = []
    try:
        df = _retry(lambda: ak.stock_info_global_em(), n=2)
        for _, r in df.iterrows():
            news.append({
                "title": str(r.get("标题", "")),
                "summary": str(r.get("摘要", "")),
                "time": str(r.get("发布时间", ""))[:16],
            })
    except Exception:
        pass
    cctv = []
    try:
        dfc = _retry(lambda: ak.news_cctv(), n=2)
        for _, r in dfc.iterrows():
            cctv.append({"title": str(r.get("title", "")),
                         "date": str(r.get("date", ""))})
    except Exception:
        pass
    # 筛选当日相关 + 关键词
    if date is not None:
        ds = str(date)
        news = [n for n in news if n["time"].startswith(ds)]
    hot = []
    for n in news:
        text = n["title"] + " " + n["summary"]
        hits = [k for k in _KEYWORDS if k in text]
        if hits:
            hot.append({**n, "keywords": hits})
    return {"news_total": len(news), "news": news[:15], "hot": hot[:10],
            "cctv": cctv[:6]}


# ---------------------------------------------------------------- 汇总
def collect_review(date: dt.date = None, use_cache: bool = True) -> Dict:
    """采集全部复盘数据(每块独立容错)。"""
    date = date or review_date()
    if use_cache:
        cached = _load_cache(f"review_{date}", ttl=config.CACHE_TTL_SECONDS)
        if cached is not None:
            return cached
    data = {
        "date": str(date),
        "indices": collect_indices(date),
        "activity": collect_activity(),
        "limit_up": collect_limit_up(date),
        "sector_flow": collect_sector_flow(),
        "north": collect_north(date),
        "market_fund": collect_market_fund(),
        "market_daily": collect_market_daily(10),
        "events": collect_events(date),
    }
    _save_cache(f"review_{date}", data)
    return data


if __name__ == "__main__":
    import json as _json
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    d = collect_review(use_cache=False)
    print(_json.dumps(d, ensure_ascii=False, indent=1, default=str)[:6000])
