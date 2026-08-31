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

    # ---- 持仓明细(核心/ETF/观察 分层, 市值降序, 观察类折叠)
    items.append({"head": "持仓明细与盈亏状态(核心/ETF/观察 分层,市值降序)"})
    _ETF_PFX = ("5", "159", "588")
    def _bucket(r: dict, mv_pct: float) -> str:
        code = str(r["code"])
        if code.startswith(_ETF_PFX) or (r.get("category") or "") in ("ETF", "基金"):
            return "ETF"
        if mv_pct < 0.01:  # 小额遗留仓(市值<1%)→观察折叠,避免逐仓方案淹没重点
            return "观察"
        if (r.get("plan_kind") in ("核心", "中军")) or (r.get("category") in ("核心", "中军")):
            return "核心"
        return "观察"
    rows_sorted = sorted(rows, key=lambda r: -(r.get("qty") or 0) * (r.get("price") or 0))
    buckets = {"核心": [], "ETF": [], "观察": []}
    for r in rows_sorted:
        _mv = r.get("market_value") or ((r.get("qty") or 0) * (r.get("price") or 0))
        buckets[_bucket(r, _mv / total_asset if total_asset else 0)].append(r)
    for bname in ("核心", "ETF", "观察"):
        grp = buckets[bname]
        if not grp:
            continue
        grp_mv = sum((r.get("qty") or 0) * (r.get("price") or 0) for r in grp)
        if bname == "观察" and len(grp) > 5:
            items.append({"t": f"**观察类持仓 {len(grp)} 只**(市值 {_fmt_asset(grp_mv)})已折叠,明细见持仓页;"
                               f"核心/ETF 之外标的统一按观察对待,不在复盘页逐一展示。"})
            continue
        tbl = []
        for r in grp:
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
        items.append({"table": {"title": f"{bname}(市值 {_fmt_asset(grp_mv)})",
                                "cols": ["名称", "代码", "分类", "持仓数", "成本", "收盘", "当日涨跌", "持仓盈亏", "属性"],
                                "rows": tbl}})

    # ---- 今日合规校验
    items.append({"head": "今日操作合规校验"})
    try:
        from app.decision.engine import phase_cfg
        _p = phase_cfg()
        items.append({"t": f"市场阶段 **{_p.get('label')}**:总仓位上限 **{_p.get('cap', 0) * 100:.0f}%**"
                           f" · 单票上限 **{_p.get('single_cap', 0) * 100:.0f}%**"
                           f" · 单次新增 **{_p.get('add_cap', 0) * 100:.0f}%**"
                           f" · 盈亏比门槛 **≥{_p.get('rr_left', 0)}:1**(左侧)"})
    except Exception:  # noqa: BLE001
        pass
    today = str(d.get("date") or dt.date.today())
    levels_map = {r["code"]: (r.get("levels") or {}) for r in rows if r.get("levels")}
    from app.review.operations import load_operations, audit_today
    ops = load_operations(today)
    audit = audit_today(ops, positions, levels_map, total_asset=total_asset)
    score = audit["score"]
    # 区分「新开仓合规」与「存量持仓合规」: 新开仓=今日操作相关(新开仓/追高/盈亏比), 存量=超仓/破位
    _NEW_KW = ("新开仓", "追高", "盈亏比")
    new_viol = [v for v in audit["violations"] if any(k in v for k in _NEW_KW)]
    hold_viol = [v for v in audit["violations"] if not any(k in v for k in _NEW_KW)]
    items.append({"t": f"今日操作 **{len(ops)}** 笔,合规得分 **{score}/10**(每项违规扣1分,0分=严重违规)"
                       + (f",违规 {len(audit['violations'])} 项" if audit["violations"] else ",无违规。" )})
    if new_viol:
        items.append({"t": "**新开仓合规**:" + "；".join(f"⚠ {v}" for v in new_viol[:4])})
    if hold_viol:
        items.append({"t": "**存量持仓合规**:" + "；".join(f"⚠ {v}" for v in hold_viol[:4])})
    for c in audit["checks"][:3]:
        items.append({"t": f"· {c}"})
    if not ops:
        items.append({"t": "今日无交易记录(合规按持仓状态审计)。"})
    # 违规整改建议: 超仓标的 → 减仓目标位与优先级(针对存量超仓)
    _fixes = _remediation(rows, spot, ov.get("total_asset"))
    if _fixes:
        items.append({"head": "违规整改建议(超仓减仓目标位)"})
        for f in _fixes:
            items.append({"t": f"· **{f['name']}({f['code']})** 仓位 **{f['pct']:.1%}**,建议反弹至 **{f['target_price']:.2f}** 时减仓至 **{f['target_pct']:.0%}** 以内"})

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


def _remediation(rows: list, spot: dict, total_asset: float) -> list:
    """超仓整改建议: 仓位超阶段单票上限的标的 → 减仓目标位(反弹至压力位)与目标仓位。

    设计目的: 存量超仓给可执行减仓方案(而非仅提示违规), 对齐「合规→整改→闭环」。
    """
    fixes = []
    try:
        from app.decision.engine import phase_cfg
        cap = phase_cfg()["single_cap"]
    except Exception:  # noqa: BLE001
        cap = 0.02
    if not total_asset:
        return fixes
    for r in rows:
        mv = r.get("market_value") or ((r.get("qty") or 0) * (r.get("price") or 0))
        pct = mv / total_asset if total_asset else 0
        if pct <= cap + 1e-6:
            continue
        lv = r.get("levels") or {}
        target_price = lv.get("resistance") or lv.get("target") or (r.get("price") or 0)
        fixes.append({"name": r.get("name") or r["code"], "code": r["code"],
                      "pct": pct, "target_price": float(target_price), "target_pct": cap})
    fixes.sort(key=lambda x: -x["pct"])
    return fixes
