# -*- coding: utf-8 -*-
"""历史决策效果追踪(反馈第5条): 每日决策快照 → 回填次一交易日实际 → 自动统计复盘。

- record(): decision_brief 完成后记录当日决策(评级/阶段/主线/推荐标的与方向)。
- backfill_actuals(): 用本地日线缓存回填「次一交易日」推荐标的方向与大盘涨跌。
- metrics(): 近 window 天统计 大盘评级方向准确率 / 推荐标的方向准确率 / 次日上涨胜率。
数据: data_cache/decision_history.jsonl。全量容错, 不阻塞主流程。
"""
import datetime as dt
import json
import os

from app import config
from app.data import dal

_DEC_FILE = os.path.join(config.DATA_DIR, "decision_history.jsonl")
_TODAY_MEM = {}


def _today() -> str:
    return dt.date.today().isoformat()


def _load_rows() -> list:
    if not os.path.exists(_DEC_FILE):
        return []
    out = {}
    try:
        with open(_DEC_FILE, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                    if r.get("date"):
                        out[r["date"]] = r   # 同日期保留最新
                except Exception:  # noqa: BLE001
                    continue
    except OSError:
        return []
    return list(out.values())


def _save_rows(rows: list) -> None:
    try:
        dal.locked_write(_DEC_FILE, "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n")
    except Exception:  # noqa: BLE001
        pass


def record(decision: dict) -> None:
    """记录当日决策快照(同日期去重覆盖)。decision 为 decision_brief() 输出。"""
    date = str((decision or {}).get("date") or _today())
    p1 = ((decision or {}).get("layers") or {}).get("layer1") or {}
    p2 = ((decision or {}).get("layers") or {}).get("layer2") or {}
    core = p2.get("core") or {}
    defen = p2.get("defensive") or {}
    stocks = []
    plans = (decision or {}).get("plans") or {}
    for sector, seg in plans.items():
        for role, p in (seg or {}).items():
            if p and p.get("ok") and p.get("code"):
                pred_dir = 1 if (p.get("target3") and p.get("price") and p["target3"] > p["price"]) else 0
                stocks.append({"code": str(p["code"]).zfill(6), "name": p.get("name"),
                               "role": role, "sector": sector, "pred": pred_dir})
    rec = {"date": date,
           "grade": p1.get("grade"), "phase": p1.get("market_phase"),
           "core_sector": core.get("name"), "def_sector": defen.get("name"),
           "cap": p1.get("cap"),
           "stocks": stocks,
           "actual_mkt": None, "actual_stock": None}
    rows = _load_rows()
    rows = [r for r in rows if r.get("date") != date] + [rec]
    if len(rows) > 400:
        rows = rows[-400:]
    _save_rows(rows)


def _next_ret(date: str, code: str):
    """date 之后首个交易日的实际涨跌幅(本地日线缓存)。"""
    try:
        from app.data import fetcher as _f
        df = _f._load_cache(str(code).zfill(6))
        if df is None or df.empty or "close" not in df.columns:
            return None
        idx = df.index.astype(str)
        mask = idx > str(date)
        if not mask.any():
            return None
        nxt = df.loc[mask, "close"].astype(float)
        pos = idx.get_loc(nxt.index[0])
        prev = float(df["close"].iloc[pos - 1])
        return float(nxt.iloc[0] / prev - 1) if prev > 0 else None
    except Exception:  # noqa: BLE001
        return None


def _next_market(date: str):
    """date 之后首个交易日的上证涨跌幅(collect_market_daily 行)。"""
    try:
        from app.review.data import collect_market_daily
        rows = collect_market_daily(15)
        for r in rows:
            if str(r.get("date")) > str(date) and r.get("pct_chg") is not None:
                return float(r["pct_chg"])
    except Exception:  # noqa: BLE001
        pass
    return None


def backfill_actuals() -> int:
    """为无 actual 的记录回填: 大盘次日涨跌 + 各推荐标的次日涨跌。返回回填条数。"""
    rows = _load_rows()
    n = 0
    for r in rows:
        if r.get("actual_mkt") is not None:
            continue
        m = _next_market(r["date"])
        if m is None:
            continue
        r["actual_mkt"] = 1 if m > 0 else (-1 if m < 0 else 0)
        st_acts = []
        for s in (r.get("stocks") or []):
            ret = _next_ret(r["date"], s["code"])
            st_acts.append(1 if ret is not None and ret > 0 else (-1 if ret is not None and ret < 0 else 0)
                           if ret is not None else None)
        r["actual_stock"] = st_acts
        n += 1
    if n:
        _save_rows(rows)
    return n


def metrics(window_days: int = 30, min_days: int = 3) -> dict:
    """近 window 天决策效果: 大盘评级方向准确率 / 推荐标的方向准确率 / 次日上涨胜率。"""
    rows = _load_rows()
    if not rows:
        return {"ok": False, "days": 0, "reason": "无决策记录"}
    cutoff = (dt.date.today() - dt.timedelta(days=window_days)).isoformat()
    rs = [r for r in rows if r.get("date", "") >= cutoff and r.get("actual_mkt") is not None]
    if not rs:
        return {"ok": False, "days": 0, "reason": "窗口内无已回填记录"}
    # 大盘方向: 评级 A/B 视为看多, C/D 视为看空
    correct = 0
    for r in rs:
        if r.get("grade") in ("A", "B"):
            if r.get("actual_mkt", 0) >= 0:
                correct += 1
        else:
            if r.get("actual_mkt", 0) <= 0:
                correct += 1
    # 标的方向: pred(1看多) vs actual(次日涨跌)
    stock_total = stock_correct = stock_up = 0
    for r in rs:
        for s, a in zip((r.get("stocks") or []), (r.get("actual_stock") or [])):
            if a is None:
                continue
            stock_total += 1
            if a > 0:
                stock_up += 1
            if (s.get("pred") == 1 and a > 0) or (s.get("pred") == 0 and a <= 0):
                stock_correct += 1
    out = {"ok": True, "days": len(rs),
           "grade_accuracy": round(correct / len(rs), 4),
           "stock_direction_accuracy": round(stock_correct / stock_total, 4) if stock_total else None,
           "stock_up_win_rate": round(stock_up / stock_total, 4) if stock_total else None,
           "stock_samples": stock_total}
    return out


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    print("回填:", backfill_actuals(), "条")
    print("指标:", metrics())
