"""交易流水 + 今日合规审计(P0-1)。

数据模型: data_cache/operations.jsonl, 每行一条交易:
  {date, code, qty, price, action: buy|sell, reason}

合规审计(规则来自 settings.discipline,可配置):
- no_new_position : 默认不开新仓(今日买入且非既有持仓 = 违规)
- chase_pct       : 买入价相对昨收(prev_price)涨幅阈值,超限 = 追高违规
- single_cap      : 单票市值/总资产上限(超限 = 违规)
- sector_cap      : 板块合计仓位上限(可选,按 sector 归属)
- stop_discipline : 破位未止损(今日最低价 < 止损位且当日未卖出该仓 = 违规)
审计数据: 今日低点/昨收取 fuyao 行情快照(单次批量),缺失时对应检查降级为"无法判定"。
合规得分 = 10 - 违规项数(每项 2 分),并输出检查明细供报告展示。
"""
import json
import os

from app import config
from app.support import settings as _st

_OP_PATH = os.path.join(config.DATA_DIR, "operations.jsonl")


def load_operations(date=None) -> list:
    if not os.path.exists(_OP_PATH):
        return []
    rows = []
    try:
        with open(_OP_PATH, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        return []
    if date:
        rows = [r for r in rows if str(r.get("date", "")).startswith(str(date))]
    return rows


def save_operations(rows: list) -> None:
    os.makedirs(config.DATA_DIR, exist_ok=True)
    with open(_OP_PATH, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def add_operation(date, code, qty, price, action, reason="") -> dict:
    code = str(code).zfill(6)
    rows = load_operations()
    rows.append({"date": str(date), "code": code, "qty": float(qty),
                 "price": float(price), "action": action, "reason": reason})
    save_operations(rows)
    return rows[-1]


def apply_op_to_portfolio(op: dict) -> float:
    """把一条交易写回持仓 CSV(买=加权加仓/新开,卖=减仓/平仓),返回该笔已实现盈亏。"""
    from app.support.risk import load_portfolio, save_portfolio
    code = str(op.get("code", "")).zfill(6)
    qty = float(op.get("qty") or 0)
    price = float(op.get("price") or 0)
    action = op.get("action")
    realized = 0.0
    pos = load_portfolio()
    hit = next((p for p in pos if str(p.get("code", "")).zfill(6) == code), None)
    if action == "buy":
        if hit:
            old_q = float(hit["qty"] or 0)
            old_c = float(hit["cost"] or price)
            new_q = old_q + qty
            hit["qty"] = round(new_q, 2)
            hit["cost"] = round((old_q * old_c + qty * price) / new_q, 4) if new_q else price
        else:
            pos.append({"code": code, "qty": qty, "cost": price, "category": "波段"})
    else:  # sell
        if hit:
            old_q = float(hit["qty"] or 0)
            cost = float(hit["cost"] or price)
            q = min(qty, old_q)
            realized = (price - cost) * q
            hit["qty"] = round(old_q - q, 2)
            if hit["qty"] <= 1e-9:
                pos.remove(hit)
    save_portfolio(pos)
    return round(realized, 2)


def realized_pnl_total() -> float:
    """已实现盈亏合计 = Σ 各笔卖出操作的 realized_pnl(落盘在操作记录中)。"""
    return round(sum(float(o.get("realized_pnl") or 0) for o in load_operations()), 2)


def account_overview(positions: list, prices: dict = None) -> dict:
    """账户模型: 总资产 = 本金 + 已实现盈亏 + 未实现浮盈; 仓位 = 市值/总资产。

    prices: {code: {price}};缺省用持仓成本近似。
    本金来源优先级: account.initial_capital → account.total_asset → decision.total_asset
    (与今日决策共用同一资金配置,避免两处维护)。
    """
    from app.support import settings as _st
    cfg = _st.load()
    acc = cfg.get("account") or {}
    decision = cfg.get("decision") or {}
    initial = (acc.get("initial_capital") or acc.get("total_asset")
               or decision.get("total_asset"))
    mv = 0.0
    unreal = 0.0
    for p in positions:
        code = str(p.get("code", "")).zfill(6)
        px = 0.0
        if prices and code in prices:
            px = float((prices[code] or {}).get("price") or 0)
        if not px:
            px = float(p.get("cost") or 0)
        qty = float(p.get("qty") or 0)
        cost = float(p.get("cost") or 0)
        mv += qty * px
        unreal += (px - cost) * qty
    realized = realized_pnl_total()
    if initial:
        total_asset = float(initial) + realized + unreal
    else:
        total_asset = mv  # 无本金配置:退回市值口径(仓位≈100%),累计盈亏仍按浮盈+已实现展示
    avail = total_asset - mv
    total_pct = (mv / total_asset) if total_asset and total_asset > 0 else 0.0
    return {
        "total_asset": round(total_asset, 2), "market_value": round(mv, 2),
        "available": round(avail, 2), "total_pct": round(total_pct, 4),
        "realized": round(realized, 2), "unrealized": round(unreal, 2),
        "cumulative_pnl": round(realized + unreal, 2),
        "initial_set": bool(initial),
    }


def clear_operations(date=None) -> list:
    rows = load_operations()
    if date:
        rows = [r for r in rows if not str(r.get("date", "")).startswith(str(date))]
    save_operations(rows)
    return rows


def _fuyao_snapshot(codes: list) -> dict:
    """今日持仓行情快照 {code: {last_price, low_price, prev_price, ...}}。fuyao 单次批量。

    仅股票走 A 股快照端点;ETF(基金)在该端点不可用,跳过由上层容错(破位/追高检查降级)。
    """
    out = {}
    try:
        from app.data.fetcher import is_etf
        stock_codes = [c for c in codes if not is_etf(c)]
        if not stock_codes:
            return out
        from app.data.fuyao import enabled as _fy_enabled
        from app.data.fuyao import _get, _thscode
        if not _fy_enabled():
            return out
        for i in range(0, len(stock_codes), 50):
            chunk = stock_codes[i:i + 50]
            ths = ",".join(_thscode(c) for c in chunk)
            data = _get("/api/a-share/prices/snapshot", {"thscodes": ths}, ttl=600) or {}
            for it in (data.get("item") or []):
                out[str(it.get("ticker", ""))[-6:]] = {
                    "last_price": it.get("last_price"), "low_price": it.get("low_price"),
                    "high_price": it.get("high_price"), "open_price": it.get("open_price"),
                    "prev_price": it.get("prev_price"),
                    "pct": it.get("price_change_ratio_pct"),
                }
    except Exception as e:  # noqa: BLE001
        print(f"[operations] fuyao 持仓快照失败: {e}")
    return out


def audit_today(today_ops: list, positions: list, levels_map: dict,
                total_asset: float = None) -> dict:
    """今日合规审计。today_ops 已按今日日期过滤;levels_map {code: {stop_loss, resistance,...}}。

    返回 {score, violations, checks, today: [...]}。total_asset 缺失时仓位类检查降级。
    """
    dcfg = _st.load().get("discipline") or {}
    no_new = bool(dcfg.get("no_new_position", False))
    chase_pct = float(dcfg.get("chase_pct", 5.0) or 5.0)
    single_cap = float(dcfg.get("single_cap", 0.02) or 0.02)

    prev_codes = {str(p["code"]).zfill(6) for p in positions}
    violations, checks = [], []

    # 1) 买卖流水 → 新开仓/追高审计
    snapshot = _fuyao_snapshot(list(prev_codes) + [str(o["code"]).zfill(6) for o in today_ops])
    # 操作已写回持仓,「昨日持仓」= 当前持仓 - 今日买入的新增代码
    today_buys = {str(o["code"]).zfill(6) for o in today_ops if o.get("action") == "buy"}
    base_codes = prev_codes - today_buys
    for op in today_ops:
        code = str(op["code"]).zfill(6)
        action = op.get("action")
        if action == "buy":
            if no_new and code not in base_codes:
                violations.append(f"违规新开仓 {code}(默认不开新仓纪律)")
            prev_px = (snapshot.get(code) or {}).get("prev_price")
            if prev_px and prev_px > 0:
                buy_up = (float(op["price"]) / prev_px - 1) * 100
                if buy_up > chase_pct:
                    violations.append(f"{code} 追高买入(较昨收 +{buy_up:.1f}%,超 {chase_pct:.0f}% 阈值)")
            if not (snapshot.get(code) or {}).get("prev_price"):
                checks.append(f"{code} 昨收缺失,追高判定跳过")
    # 2) 仓位红线(单票)
    mv_sum = 0.0
    if total_asset:
        for p in positions:
            qty = float(p.get("qty") or 0)
            px = (snapshot.get(str(p["code"]).zfill(6)) or {}).get("last_price") \
                or float(p.get("cost") or 0)
            mv = qty * px
            mv_sum += mv
            if total_asset > 0 and mv / total_asset > single_cap:
                violations.append(f"{p['code']} 单票仓位 {(mv / total_asset) * 100:.1f}% 超红线 {single_cap * 100:.0f}%")
    # 3) 破位未止损(今日最低 < stop_loss 且当日未卖出)
    sold_today = {str(o["code"]).zfill(6) for o in today_ops if o.get("action") == "sell"}
    for code, lv in levels_map.items():
        stop = lv.get("stop_loss")
        low = (snapshot.get(code) or {}).get("low_price")
        if stop and low and low < stop and code not in sold_today:
            violations.append(f"{code} 破位({low:.2f} < 止损 {stop:.2f})未止损")
    # 4) 汇总
    score = max(0, 10 - 2 * len(violations))
    return {"score": score, "violations": violations, "checks": checks,
            "today": today_ops, "single_cap": single_cap}
