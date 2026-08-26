"""持仓与交易体系复盘(P0-2): 持仓明细 / 账户概览 / 今日合规校验 / 明日逐仓操作方案。

数据源:
- 持仓: risk.load_portfolio(代码/数量/成本/分类)
- 逐仓行情与方案: portfolio.diagnose(复用持仓诊断页的逐仓预测/盈亏/levels/plan)
- 今日操作: operations.load_operations(今日) + operations.audit_today(合规审计)
- 名称/当日涨跌: _a_spot_map; 昨收/最低: fuyao 行情快照(合规审计用)

容错: 任一步骤失败降级为「暂缺」,不阻塞整体报告。
"""
import datetime as dt

from app.support import settings as _st


def _cell(v) -> str:
    return str(v) if v is not None else "-"


def _fmt_asset(v) -> str:
    return f"{v / 1e4:.2f}万" if v is not None and abs(v) >= 1e4 else str(v)


def positions_review(d: dict) -> list:
    """生成「持仓与交易体系」结构化 items(账户概览/持仓明细/合规校验/逐仓方案/交易纪律)。"""
    from app.support.risk import load_portfolio
    positions = load_portfolio()
    items = []
    if not positions:
        items.append({"t": "当前无持仓记录(可在持仓诊断页导入持仓 CSV)。"})
        return items

    acc = _st.load().get("account") or {}
    diag = _diagnose(positions, acc.get("initial_capital"))
    rows = diag.get("positions", [])
    summary = diag.get("summary", {})
    spot = _spot_map()
    ov = _account_overview(positions, spot)      # 账户模型(总资产=本金+已实现+浮盈)
    total_asset = ov.get("total_asset")
    mv = ov.get("market_value")
    avail = ov.get("available")

    fy = _fy_snapshot(positions)
    pnl_total = ov.get("cumulative_pnl")

    # ---- 账户概览
    items.append({"head": "账户整体概览"})
    items.append({"t": (f"总资产 **{_fmt_asset(total_asset)}** · 持仓市值 **{_fmt_asset(mv)}**"
                        f" · 可用资金 **{_fmt_asset(avail)}** · 总仓位 **{ov.get('total_pct', 0) * 100:.1f}%**"
                        f" · 累计持仓盈亏 **{_fmt_asset(pnl_total)}**"
                        f"(已实现 **{_fmt_asset(ov.get('realized'))}** + 未实现 **{_fmt_asset(ov.get('unrealized'))}**)"
                        + (f" · 风险评级 **{_cell(summary.get('risk_rating'))}**" if summary.get("risk_rating") else ""))})

    # ---- 持仓明细
    items.append({"head": "持仓明细与盈亏状态"})
    tbl = []
    for r in rows:
        code = r["code"]
        fy_i = fy.get(code) or {}
        pct = fy_i.get("pct")
        if pct is None:
            s = spot.get(code) or {}
            pct = s.get("pct_chg")
        tbl.append([
            _cell(r.get("name") or code), code, _cell(r.get("category") or "-"),
            f"{r.get('qty') or 0:.0f}",
            f"{r.get('cost') or 0:.3f}", f"{r.get('price') or 0:.3f}",
            _cell(f"{pct:+.2f}%" if pct is not None else "-"),
            _cell(f"{r.get('pnl_pct', 0) * 100:+.1f}%"),
            _cell(f"{r.get('plan_kind') or '-'}"),
        ])
    items.append({"table": {"title": "",
                            "cols": ["名称", "代码", "分类", "持仓数", "成本", "收盘", "当日涨跌", "持仓盈亏", "属性"],
                            "rows": tbl}})

    # ---- 今日合规校验
    items.append({"head": "今日操作合规校验"})
    today = str(d.get("date") or dt.date.today())
    levels_map = {r["code"]: (r.get("levels") or {}) for r in rows if r.get("levels")}
    from app.review.operations import load_operations, audit_today
    ops = load_operations(today)
    audit = audit_today(ops, positions, levels_map, total_asset=total_asset)
    score = audit["score"]
    items.append({"t": f"今日操作 **{len(ops)}** 笔,合规得分 **{score}/10**"
                       + (f",违规 {len(audit['violations'])} 项" if audit["violations"] else ",无违规。" )})
    for v in audit["violations"][:5]:
        items.append({"t": f"⚠ {v}"})
    for c in audit["checks"][:3]:
        items.append({"t": f"· {c}"})
    if not ops:
        items.append({"t": "今日无交易记录(合规按持仓状态审计)。"})

    # ---- 明日逐仓操作方案
    items.append({"head": "明日持仓操作方案"})
    half = "半小时不收回" if (_st.load().get("discipline") or {}).get("half_hour_stop", True) else ""
    plan_rows = []
    for r in rows:
        code = r["code"]
        lv = r.get("levels") or {}
        res = lv.get("resistance")
        stop = lv.get("stop_loss")
        tgt = lv.get("target")
        if not (res or stop):
            continue
        trig = []
        if stop:
            trig.append(f"跌破 {stop:.2f} {half}止损/减仓")
        if res:
            trig.append(f"反弹至 {res:.2f} 减仓")
        plan_rows.append([
            _cell(r.get("name") or code), code,
            _cell(f"{res:.2f}" if res else "-"),
            _cell(f"{stop:.2f}" if stop else "-"),
            _cell(f"{tgt:.2f}" if tgt else "-"),
            "；".join(trig),
        ])
    if plan_rows:
        items.append({"table": {"title": "",
                                "cols": ["标的", "代码", "反弹减仓位", "止损位", "目标位", "触发条件"],
                                "rows": plan_rows}})
    else:
        items.append({"t": "逐仓方案暂缺(持仓行情/模型数据未就绪)。"})

    # ---- 统一交易纪律
    rules = (_st.load().get("discipline") or {}).get("rules") or []
    if rules:
        items.append({"head": "统一交易纪律重申"})
        for r_ in rules:
            items.append({"t": f"· {r_}"})
    return items


def _diagnose(positions, total_asset):
    """复用持仓诊断页逐仓计算(预测/盈亏/levels/plan),独立容错。"""
    try:
        from app.support import portfolio as pf
        return pf.diagnose(positions, total_asset=total_asset)
    except Exception as e:  # noqa: BLE001
        print(f"[positions] 持仓诊断失败: {e}")
        return {"positions": [], "summary": {}}


def _spot_map() -> dict:
    try:
        from app.support.mainline import _a_spot_map
        return _a_spot_map()
    except Exception:  # noqa: BLE001
        return {}


def _account_overview(positions, spot: dict) -> dict:
    """账户模型封装: spot {code:{price,...}} → prices 口径。"""
    try:
        from app.review.operations import account_overview
        prices = {code: {"price": (v or {}).get("price")} for code, v in spot.items()}
        return account_overview(positions, prices=prices)
    except Exception as e:  # noqa: BLE001
        print(f"[positions] 账户模型失败: {e}")
        return {"total_asset": 0, "market_value": 0, "available": 0,
                "total_pct": 0, "realized": 0, "unrealized": 0, "cumulative_pnl": 0}


def _fy_snapshot(positions) -> dict:
    try:
        from app.review.operations import _fuyao_snapshot
        return _fuyao_snapshot([str(p["code"]).zfill(6) for p in positions])
    except Exception:  # noqa: BLE001
        return {}
