"""模块4 盘中监控与预警:价位 / 板块异动 / 情绪极值 / 模型信号 / 量能异常。

支持后台线程轮询(Web 启动时可选开启),预警写入 data_cache/alerts_YYYY-MM-DD.json,
Web「盘中预警」页实时展示与确认。
"""
import datetime as dt
import json
import os
import threading
import time

from app import config
from app.data.fetcher import get_daily_history, get_spot_quotes
from app.features.market_features import market_snapshot
from app.ml.predictor import Predictor
from app.support import settings as _st

_STATE = {"running": False, "thread": None, "last_check": None, "last_count": 0}
_alerts_loaded = {}


def _alerts_path(date: str = None) -> str:
    date = date or dt.date.today().strftime("%Y-%m-%d")
    return os.path.join(config.DATA_DIR, f"alerts_{date}.json")


def load_alerts(date: str = None) -> list:
    p = _alerts_path(date)
    if p in _alerts_loaded:
        return _alerts_loaded[p]
    try:
        with open(p, encoding="utf-8") as f:
            _alerts_loaded[p] = json.load(f)
    except (OSError, ValueError):
        _alerts_loaded[p] = []
    return _alerts_loaded[p]


def append_alerts(items: list) -> None:
    date = dt.date.today().strftime("%Y-%m-%d")
    p = _alerts_path(date)
    cur = load_alerts(date)
    seen = {tuple(a.items()) for a in cur}
    for a in items:
        key = (a["rule"], a.get("code", ""), a.get("msg", ""))
        if key in seen:
            continue
        seen.add(key)
        a.setdefault("time", dt.datetime.now().strftime("%H:%M:%S"))
        a.setdefault("date", date)
        cur.append(a)
    try:
        with open(p, "w", encoding="utf-8") as f:
            json.dump(cur, f, ensure_ascii=False, indent=2)
    except OSError:
        pass
    _alerts_loaded[p] = cur


def clear_alerts(date: str = None) -> None:
    date = date or dt.date.today().strftime("%Y-%m-%d")
    p = _alerts_path(date)
    try:
        if os.path.exists(p):
            os.remove(p)
    except OSError:
        pass
    _alerts_loaded.pop(p, None)


def _fast_levels(code: str) -> dict:
    """轻量支撑/压力(不跑模型),供高频轮询使用。"""
    df = get_daily_history(code, days=60, adjust="qfq")
    if len(df) < 20:
        return {}
    hi20 = float(df["high"].tail(20).max())
    lo20 = float(df["low"].tail(20).min())
    return {
        "support": round(lo20, 2), "resistance": round(hi20, 2),
        "ma20": round(float(df["close"].tail(20).mean()), 2),
        "yest_amount": float(df["volume"].iloc[-2] * df["close"].iloc[-2]),
    }


def _check_position(code, price, pct_chg, amount, cfg, predictor, quotes) -> list:
    mon = cfg["monitor"]
    rules = mon["rules"]
    out = []
    lev = _fast_levels(code)
    if not lev:
        return out
    if rules.get("price", True):
        if price <= lev["support"] * 1.01:
            out.append({"rule": "price", "level": "warning",
                        "msg": f"现价 {price:.2f} 逼近/跌破支撑 {lev['support']:.2f}"})
        if price >= lev["resistance"] * 0.99:
            out.append({"rule": "price", "level": "info",
                        "msg": f"现价 {price:.2f} 逼近压力 {lev['resistance']:.2f}"})
    if rules.get("volume", True):
        ratio = 0.0
        if lev.get("yest_amount"):
            ratio = amount / lev["yest_amount"]
        if ratio > mon.get("volume_yesterday_ratio", 1.0) and pct_chg > 3:
            out.append({"rule": "volume", "level": "warning",
                        "msg": f"放量 {ratio:.1f} 倍于昨日,涨幅 {pct_chg:+.2f}%"})
    if rules.get("signal", True) and code and code in quotes:
        try:
            from app.support.portfolio import _one
            _, pred, adv = _one(code, predictor, quotes, None, cfg)
            if adv["action"] in ("sell", "reduce"):
                out.append({"rule": "signal", "level": "warning",
                            "msg": f"模型建议:{adv['action_cn']} ({pred['direction_cn']})"})
        except Exception:  # noqa: BLE001
            pass
    return out


def check_once(positions: list = None) -> list:
    """执行一轮检查,返回新预警。持仓为空时监控涨停池 + 主线标的。"""
    from app.support.risk import load_portfolio
    cfg = _st.load()
    mon = cfg["monitor"]
    rules = mon["rules"]
    positions = positions if positions is not None else load_portfolio()
    codes = [p["code"] for p in positions]
    if not codes:
        try:
            from app.support.mainline import _zt_pool
            codes = [z["code"] for z in _zt_pool()[:20]]
        except Exception:  # noqa: BLE001
            pass
    if not codes:
        return []

    quotes = get_spot_quotes(codes)
    out = []
    predictor = Predictor()

    for c in codes:
        q = quotes.get(c)
        if not q or not q["price"]:
            continue
        base = {"code": c}
        for a in _check_position(c, q["price"], q["pct_chg"], q["amount"], cfg, predictor, quotes):
            out.append({**base, **a})

    if rules.get("sector", True):
        try:
            from app.review.data import collect_sector_flow
            for f in collect_sector_flow(use_cache=True):
                if f["net_yi"] >= mon.get("sector_net_yi", 5.0) and f["pct_chg"] >= mon.get("sector_pct", 2.0):
                    out.append({"rule": "sector", "level": "info",
                                "msg": f"板块异动:{f['industry']} 涨 {f['pct_chg']:+.2f}%,净流入 {f['net_yi']:.1f} 亿"})
        except Exception:  # noqa: BLE001
            pass

    if rules.get("mood", True):
        try:
            snap = market_snapshot()
            fg = (snap or {}).get("market", {}).get("market_fear_greed")
            if fg is not None and fg <= mon.get("fg_extreme_low", 20):
                out.append({"rule": "mood", "level": "warning",
                            "msg": f"恐贪指数 {fg} 极度恐慌,注意系统性风险(总仓位上限收紧)"})
            elif fg is not None and fg >= mon.get("fg_extreme_high", 80):
                out.append({"rule": "mood", "level": "info",
                            "msg": f"恐贪指数 {fg} 极度贪婪,谨防冲高回落,注意止盈纪律"})
        except Exception:  # noqa: BLE001
            pass
    return out


def _loop(interval: int, on_alert) -> None:
    while _STATE["running"]:
        try:
            items = check_once()
            if items:
                append_alerts(items)
                if on_alert:
                    on_alert(items)
            _STATE["last_check"] = dt.datetime.now().strftime("%H:%M:%S")
            _STATE["last_count"] = len(items)
        except Exception as e:  # noqa: BLE001
            print(f"[monitor] 轮询异常: {e}")
        time.sleep(interval)


def start(interval: int = None, on_alert=None) -> bool:
    if _STATE["running"]:
        return False
    cfg = _st.load().get("monitor", {})
    if not cfg.get("enable", True):
        return False
    interval = interval or cfg.get("refresh_sec", 300)
    _STATE["running"] = True
    _STATE["thread"] = threading.Thread(target=_loop, args=(interval, on_alert),
                                        daemon=True, name="monitor")
    _STATE["thread"].start()
    return True


def stop() -> None:
    _STATE["running"] = False


def status() -> dict:
    return {"running": _STATE["running"],
            "last_check": _STATE["last_check"], "last_count": _STATE["last_count"]}
