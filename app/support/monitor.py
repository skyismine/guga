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


def _fault(e: BaseException, note: str = ""):
    """记录一次被降级吞掉的异常(接入 fault 统一日志, 不再静默)。"""
    try:
        from app.support import fault as _flt
        _flt.warning("monitor", note or "处理降级(按缺省继续)", exc=e)
    except Exception as _e:  # noqa: BLE001
        _fault(_e)


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


# ---------------------------------------------------------------- 动态止损盘中跟踪
# code -> {"hi": 持仓期最高价, "stage": 0初始/1保本/2锁定/3跟踪, "alerted": 已提醒的最高阶段}
# 止损价「只上移不下移」: stage 只增不减, hi 只取新高 → 止损只会上移
_DS_STATE = {}
_DS_DEFAULTS = {"breakeven_pct": 0.05, "lock_pct": 0.10, "trail_pct": 0.20, "trail_drawdown": 0.08}


def _monitor_atype(code: str, category) -> str:
    """持仓标的主推断类型(供动态止损按类型取参数): ETF/补涨/情绪/其余默认中军。"""
    c = str(code)
    if c.startswith(("5", "159", "588")) or (category and ("ETF" in str(category) or "基金" in str(category))):
        return "etf"
    if category and "补涨" in str(category):
        return "repair"
    if category and ("情绪" in str(category) or "激进" in str(category)):
        return "mood"
    return "mid"


def _check_dynamic_stop(code, price, cost, category, cfg) -> list:
    """动态止损盘中跟踪(第2块): 浮盈跨档→建议上移止损(info); 跌破当前阶段止损→预警(warning)。

    只上移不下移: 阶段随浮盈/最高价只增不减, 止损价只升不降。
    """
    dsm = (cfg.get("monitor") or {}).get("dynamic_stop") or {}
    ep = ((cfg.get("decision") or {}).get("exec_param") or {})
    dscfg = (ep.get("dynamic_stop") or {})
    if not dsm.get("enabled", True) or not cost or cost <= 0 or not dscfg.get("enabled", True):
        return []
    _atype = _monitor_atype(code, category)
    d = {**_DS_DEFAULTS, **(dscfg.get(_atype) or dscfg.get("mid") or {})}
    _be = float(d["breakeven_pct"]); _lk = float(d["lock_pct"])
    _tr = float(d["trail_pct"]); _dd = float(d["trail_drawdown"])
    pnl = float(price) / float(cost) - 1
    st = _DS_STATE.setdefault(str(code), {"hi": float(price), "stage": 0, "alerted": 0})
    if float(price) > st["hi"]:
        st["hi"] = float(price)
    if pnl >= _tr:
        _stage = 3
    elif pnl >= _lk:
        _stage = 2
    elif pnl >= _be:
        _stage = 1
    else:
        _stage = 0
    if _stage > st["stage"]:
        st["stage"] = _stage        # 只上移不下移
    if st["stage"] == 0:
        return []                    # 初始阶段由静态止损/价位规则覆盖
    _lbl = {1: "保本", 2: "锁定浮盈", 3: "跟踪"}[st["stage"]]
    if st["stage"] == 3:
        _stop = round(st["hi"] * (1 - _dd), 2)
    elif st["stage"] == 2:
        _stop = round(float(cost) * (1 + _be), 2)
    else:
        _stop = round(float(cost), 2)
    out = []
    if float(price) <= _stop:
        out.append({"rule": "dyn_stop", "level": "warning",
                    "msg": f"跌破{_lbl}止损 {_stop:.2f}(浮盈 {pnl * 100:+.1f}%,成本 {cost:.2f}),建议执行止损/减仓"})
    elif st["stage"] > st["alerted"]:
        st["alerted"] = st["stage"]
        out.append({"rule": "dyn_stop", "level": "info",
                    "msg": f"浮盈已达 {pnl * 100:.1f}%,进入{_lbl}阶段,建议止损上移至 {_stop:.2f}(只上移不下移)"})
    return out


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
        except Exception as _e:  # noqa: BLE001
            _fault(_e)
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
        except Exception as _e:  # noqa: BLE001
            _fault(_e)
    if not codes:
        return []

    quotes = get_spot_quotes(codes)
    out = []
    predictor = Predictor()
    pos_map = {str(p.get("code", "")).zfill(6): p for p in positions}

    for c in codes:
        q = quotes.get(c)
        if not q or not q["price"]:
            continue
        base = {"code": c}
        for a in _check_position(c, q["price"], q["pct_chg"], q["amount"], cfg, predictor, quotes):
            out.append({**base, **a})
        # 动态止损盘中跟踪(持仓标的, 需成本; 只上移不下移)
        _p = pos_map.get(str(c).zfill(6))
        if _p and _p.get("cost"):
            for a in _check_dynamic_stop(c, q["price"], _p["cost"], _p.get("category"), cfg):
                out.append({**base, **a})

    if rules.get("sector", True):
        try:
            from app.review.data import collect_sector_flow
            for f in collect_sector_flow(use_cache=True):
                if f["net_yi"] >= mon.get("sector_net_yi", 5.0) and f["pct_chg"] >= mon.get("sector_pct", 2.0):
                    out.append({"rule": "sector", "level": "info",
                                "msg": f"板块异动:{f['industry']} 涨 {f['pct_chg']:+.2f}%,净流入 {f['net_yi']:.1f} 亿"})
        except Exception as _e:  # noqa: BLE001
            _fault(_e)

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
        except Exception as _e:  # noqa: BLE001
            _fault(_e)
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
