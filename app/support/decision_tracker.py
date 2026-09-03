# -*- coding: utf-8 -*-
"""历史决策效果追踪: 每日决策快照 → 回填次日起多周期实际(1/3/5交易日) → 分层统计复盘。

- record(): decision_brief 完成后记录当日决策(评级/阶段/主线/推荐标的, 含价格/止损/三档目标/类型/模式)。
- backfill_actuals(): 用本地日线缓存回填各标的 1/3/5 交易日涨跌幅、止损/目标价理论触发、最大涨幅/回撤, 及大盘多周期涨跌。
- metrics(): 简洁版(首页/健康检查)。
- metrics_detailed(): 分层统计(按大盘评级/市场阶段/标的角色/板块), 供复盘页面。
数据: data_cache/decision_history.jsonl。全量容错, 不阻塞主流程。

口径说明:
- 回填以「决策日之后第 1/3/5 个交易日」计算(跳过缺失日, 缓存缺则置 None 下次再补);
- 止损/目标价触发 = 决策日后 5 个交易日内 low/high 相对止损价/目标价的理论触发(标注理论值);
- 推荐方向: 仅记录「可操作买点计划(ok)」, pred=1(看多推荐); 观望/持有/减仓不在 plans 中, 不污染方向统计;
- 分层每组样本 < min_sample(默认3) 标注 enough=False, 避免小样本误读。
"""
import datetime as dt
import json
import os

from app import config
from app.data import dal

_DEC_FILE = os.path.join(config.DATA_DIR, "decision_history.jsonl")
_TODAY_MEM = {}
# 各标的日线缓存本次进程内复用(回填多行/多次调用不重复读盘)
_DF_MEM = {}


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


_ROLE_ATYPE = {"aggressive": "mood", "steady": "mid", "repair": "repair", "etf": "etf"}


def record(decision: dict) -> None:
    """记录当日决策快照(同日期去重覆盖)。decision 含 date/layers/plans(engine 传入)。"""
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
                atype = p.get("asset_type") or _ROLE_ATYPE.get(role, "mid")
                if atype == "def_etf":
                    atype = "etf"
                price = p.get("price") or 0
                stocks.append({
                    "code": str(p["code"]).zfill(6), "name": p.get("name"),
                    "role": role, "asset_type": atype, "sector": sector,
                    "pred": 1,                      # 仅可操作买点计划, 一律看多推荐
                    "price": round(price, 3) if price else None,
                    "stop": p.get("stop"), "t1": p.get("target1"),
                    "t2": p.get("target2"), "t3": p.get("target3"),
                    "mode": p.get("mode"),          # breakout(突破跟进)/pullback(回踩低吸)
                    "actual": None})
    rec = {"date": date,
           "grade": p1.get("grade"), "phase": p1.get("market_phase"),
           "core_sector": core.get("name"), "def_sector": defen.get("name"),
           "cap": p1.get("cap"),
           "stocks": stocks,
           "actual_mkt": None, "mkt_ret": None}
    rows = _load_rows()
    rows = [r for r in rows if r.get("date") != date] + [rec]
    if len(rows) > 400:
        rows = rows[-400:]
    _save_rows(rows)


def _df_cache(code: str):
    """取个股本地日线缓存(进程内复用)。"""
    code = str(code).zfill(6)
    if code not in _DF_MEM:
        try:
            from app.data.fetcher import _load_cache as _lc
            _DF_MEM[code] = _lc(code)
        except Exception:  # noqa: BLE001
            _DF_MEM[code] = None
    return _DF_MEM[code]


def _mkt_after_pct(date: str) -> list:
    """date 之后的首批交易日涨跌幅列表(升序, 最多5)。collect_market_daily 行升序。
    md 行 pct_chg 统一为「百分值」(0.43=+0.43%, 东财/校准后 fuyao 一致), 复利需换算小数 ÷100。
    """
    try:
        from app.review.data import collect_market_daily
        rows = collect_market_daily(20)
    except Exception:  # noqa: BLE001
        return []
    aft = []
    for r in rows:
        d = str(r.get("date") or "")[:10]
        if d > str(date) and r.get("pct_chg") is not None:
            aft.append(float(r["pct_chg"]) / 100.0)
        if len(aft) >= 5:
            break
    return aft


def _market_actual(date: str):
    """大盘 1/3/5 交易日涨跌幅(基于上证 pct 序列)。返回 dict 或 None(数据不足)。"""
    aft = _mkt_after_pct(date)
    if not aft:
        return None
    out = {"ret_1d": None, "ret_3d": None, "ret_5d": None}
    for k in (1, 3, 5):
        if len(aft) >= k:
            v = 1.0
            for x in aft[:k]:
                v *= 1 + x
            out[f"ret_{k}d"] = round(v - 1, 4)
    return out if any(v is not None for v in out.values()) else None


def _stock_actual(code: str, date: str, anchor, stop, t1, t2, t3):
    """单标的回填: 决策日后第1/3/5交易日涨跌幅(锚=决策日bar收盘,缺失用记录价)
    + 5日内 止损/目标价 理论触发 + max_gain/max_dd。数据不足返回 None(下次再补)。
    """
    if not anchor:
        return None
    df = _df_cache(code)
    if df is None or df.empty or "close" not in df.columns:
        return None
    try:
        close = df["close"].astype(float)
        idx = df.index.astype(str)
        base = None
        try:
            base = float(close[idx == str(date)].iloc[0])
        except Exception:  # noqa: BLE001
            base = None
        base = base or float(anchor)
        if base <= 0:
            return None
        sub = df[idx > str(date)]
        if sub.empty:
            return None
        cl = sub["close"].astype(float)
        hi = sub["high"].astype(float) if "high" in sub.columns else None
        lo = sub["low"].astype(float) if "low" in sub.columns else None
        out = {"ret_1d": None, "ret_3d": None, "ret_5d": None,
               "hit_stop": None, "hit_t1": None, "hit_t2": None, "hit_t3": None,
               "max_gain": None, "max_dd": None}
        for k in (1, 3, 5):
            if len(cl) >= k:
                out[f"ret_{k}d"] = round(float(cl.iloc[k - 1] / base - 1), 4)
        if len(sub) >= 5 and hi is not None and lo is not None:
            h5, l5 = hi.head(5), lo.head(5)
            if stop:
                out["hit_stop"] = bool(float(l5.min()) <= float(stop))
            for nm, tgt in (("hit_t1", t1), ("hit_t2", t2), ("hit_t3", t3)):
                if tgt:
                    out[nm] = bool(float(h5.max()) >= float(tgt))
            out["max_gain"] = round(float(h5.max() / base - 1), 4)
            out["max_dd"] = round(float(l5.min() / base - 1), 4)
        if not any(v is not None for v in out.values()):
            return None
        return out
    except Exception:  # noqa: BLE001
        return None


def backfill_actuals() -> int:
    """为无 actual 的记录回填: 大盘 1/3/5 交易日涨跌 + 各标的 1/3/5 涨跌/止损目标触发。
    数据不足的字段保持 None 下次重试; 返回本轮新增完成(actual_mkt)的记录数。"""
    rows = _load_rows()
    n = 0
    changed = False
    for r in rows:
        ma = _market_actual(r["date"])
        if ma is None:
            continue
        if r.get("actual_mkt") is None:
            r["actual_mkt"] = 1 if ma["ret_1d"] and ma["ret_1d"] > 0 else (-1 if ma["ret_1d"] and ma["ret_1d"] < 0 else 0)
            r["mkt_ret"] = ma
            n += 1
            changed = True
        elif r.get("mkt_ret") != ma:
            r["mkt_ret"] = ma
            changed = True
        for s in (r.get("stocks") or []):
            cur = s.get("actual")
            act = _stock_actual(s["code"], r["date"], s.get("price"),
                                s.get("stop"), s.get("t1"), s.get("t2"), s.get("t3"))
            if act is not None and (cur is None or act.get("ret_5d") != cur.get("ret_5d")):
                s["actual"] = act
                changed = True
    if changed:
        _save_rows(rows)
    return n


def metrics(window_days: int = 30, min_days: int = 3) -> dict:
    """简洁版(近 window 天): 大盘评级方向准确率 / 推荐标的方向准确率(=上涨胜率)。"""
    rows = _load_rows()
    if not rows:
        return {"ok": False, "days": 0, "reason": "无决策记录"}
    cutoff = (dt.date.today() - dt.timedelta(days=window_days)).isoformat()
    rs = [r for r in rows if r.get("date", "") >= cutoff and r.get("actual_mkt") is not None]
    if not rs:
        return {"ok": False, "days": 0, "reason": "窗口内无已回填记录"}
    correct = 0
    for r in rs:
        if r.get("grade") in ("A", "B"):
            if r.get("actual_mkt", 0) >= 0:
                correct += 1
        else:
            if r.get("actual_mkt", 0) <= 0:
                correct += 1
    stock_total = stock_up = 0
    for r in rs:
        for s in (r.get("stocks") or []):
            a = s.get("actual") or {}
            if a.get("ret_1d") is None:
                continue
            stock_total += 1
            if a["ret_1d"] > 0:
                stock_up += 1
    out = {"ok": True, "days": len(rs),
           "grade_accuracy": round(correct / len(rs), 4),
           "stock_direction_accuracy": round(stock_up / stock_total, 4) if stock_total else None,
           "stock_up_win_rate": round(stock_up / stock_total, 4) if stock_total else None,
           "stock_samples": stock_total,
           "mkt_avg_ret_1d": round(sum((r.get("mkt_ret") or {}).get("ret_1d") or 0 for r in rs) / len(rs), 4)}
    return out


def _sample_stat(samples: list, min_sample: int) -> dict:
    """一组(每个为含 ret_1d/3d/5d 的 actual dict)的统计。"""
    def _avg(key):
        vals = [x[key] for x in samples if x.get(key) is not None]
        return round(sum(vals) / len(vals), 4) if vals else None
    n = len(samples)
    wins = sum(1 for x in samples if (x.get("ret_1d") or 0) > 0)
    return {"count": n, "enough": n >= min_sample,
            "win_rate": round(wins / n, 4) if n else None,
            "avg_ret_1d": _avg("ret_1d"), "avg_ret_3d": _avg("ret_3d"),
            "avg_ret_5d": _avg("ret_5d"),
            "stop_hit_rate": round(sum(1 for x in samples if x.get("hit_stop")) / n, 4) if n else None,
            "t1_hit_rate": round(sum(1 for x in samples if x.get("hit_t1")) / n, 4) if n else None,
            "t3_hit_rate": round(sum(1 for x in samples if x.get("hit_t3")) / n, 4) if n else None}


def metrics_detailed(window_days: int = 30, min_sample: int = 3) -> dict:
    """分层统计(近 window 天): 按大盘评级 / 市场阶段 / 标的角色 / 板块 + 整体。
    每组 count<min_sample 标 enough=False(样本不足,仅供参考)。"""
    rows = _load_rows()
    if not rows:
        return {"ok": False, "days": 0, "reason": "无决策记录"}
    cutoff = (dt.date.today() - dt.timedelta(days=window_days)).isoformat()
    rs = [r for r in rows if r.get("date", "") >= cutoff and r.get("actual_mkt") is not None]
    if not rs:
        return {"ok": False, "days": 0, "reason": "窗口内无已回填记录"}
    from collections import defaultdict
    by_grade, by_phase, by_role, by_sector = defaultdict(list), defaultdict(list), defaultdict(list), defaultdict(list)
    overall = []
    for r in rs:
        for s in (r.get("stocks") or []):
            a = s.get("actual") or {}
            if a.get("ret_1d") is None:
                continue
            overall.append(a)
            by_grade[r.get("grade") or "?"].append(a)
            by_phase[r.get("phase") or "?"].append(a)
            by_role[s.get("asset_type") or s.get("role") or "?"].append(a)
            by_sector[s.get("sector") or "?"].append(a)

    def _group(dct):
        out = []
        for k, v in dct.items():
            st = _sample_stat(v, min_sample)
            st["key"] = k
            out.append(st)
        out.sort(key=lambda x: (x.get("avg_ret_5d") is None, -(x.get("avg_ret_5d") or 0)))
        return out
    overall_stat = _sample_stat(overall, min_sample) if overall else None
    return {"ok": True, "days": len(rs), "window_days": window_days, "min_sample": min_sample,
            "overall": overall_stat, "by_grade": _group(by_grade),
            "by_phase": _group(by_phase), "by_role": _group(by_role),
            "by_sector": _group(by_sector)}


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    print("回填:", backfill_actuals(), "条")
    print("指标:", metrics())
    print("分层:", metrics_detailed())
