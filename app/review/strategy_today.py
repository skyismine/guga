"""P0-4 明日交易策略(替换原泛化操作策略,基于主线三层分级 + 观察标的池输出)。

- 主线分层操作策略: 核心(优先仓位/分歧低吸/不追高打板) → 发酵(轻仓试错/首板跟进/快进快出) → 观察(仅观察)。
- 超跌标的操作策略: 短线轻仓套利,次日确认企稳+量能放大再进场,压力位止盈/跌破新低止损。
- 整体仓位与风控: 总仓位 = 大盘许可上限(cap) × 主线强度系数,并给出风控红线与禁止操作。
设计取舍: 单板块仓位建议按「大盘总仓位上限 / 在榜核心板块数」均分,简单可执行;
强度系数为规则映射(强1.0/中0.85/弱0.6),不引入额外模型。
"""
from app.review import archive


def _cell(v) -> str:
    return str(v) if v is not None else "-"


def _strength_ctx() -> tuple:
    """轻量主线上下文: (core_names, branch_names, strength)。复用 layers 摘要,口径一致。"""
    from app.review.layers import layer_summary
    s = layer_summary()
    return s["core"], s["branch"], s["strength"]


def strategy_review(d: dict) -> list:
    """生成「明日交易策略」结构化 items。"""
    items = []
    core, branch, strength = _strength_ctx()

    # ---- 阶段前置: 当前阶段/仓位上限/盈亏比要求/操作基调(四阶段体系)
    try:
        from app.decision.engine import phase_cfg
        _p = phase_cfg()
        items.append({"head": "0. 市场阶段与风控基调(全局阶段)"})
        items.append({"t": f"当前市场阶段 **{_p.get('label')}**:总仓位上限 **{_p.get('cap', 0) * 100:.0f}%**"
                           f" · 单票上限 **{_p.get('single_cap', 0) * 100:.0f}%**"
                           f" · 单次新增上限 **{_p.get('add_cap', 0) * 100:.0f}%**"
                           f" · 盈亏比门槛 左侧≥{_p.get('rr_left')} / 右侧"
                           + (f"≥{_p.get('rr_right')}" if _p.get("rr_right") else "禁开")
                           + f" · 操作基调 **{_p.get('keynote')}**。"})
        if _p.get("add_cap", 0) <= 0:
            items.append({"t": "⚠ 当前阶段禁止新增开仓/加仓,仅持有管理。"})
    except Exception:  # noqa: BLE001
        pass

    # 大盘许可与仓位
    cap = None
    grade = None
    try:
        from app.decision.engine import market_permit
        p = market_permit()
        grade, cap = p.get("grade"), p.get("cap")
    except Exception:  # noqa: BLE001
        pass

    mult = {"强": 1.0, "中": 0.85, "弱": 0.6}.get(strength, 0.7)
    pos = round(min(0.95, (cap or 0.5) * mult), 2) if cap is not None else None
    per_core = round((pos or 0) / max(1, len(core)), 2) if pos else None

    items.append({"head": "1. 主线分层操作策略"})
    items.append({"t": f"**核心主线层**({ '、'.join(core) if core else '暂无' }): 优先仓位,单板块建议仓位 **{per_core or '-'}/总仓位 {pos or '-'}**,"
                       "分歧低吸为主,不追高打板;缩量回踩支撑位企稳即承接,放量滞涨减半。"})
    items.append({"t": f"**发酵轮动层**({ '、'.join(branch) if branch else '暂无' }): 轻仓试错,首板/启动点跟进,快进快出,"
                       "不及预期当日出局,不留隔夜仓。"})
    items.append({"t": "**异动观察层**: 仅观察不操作,确认晋级发酵层后再跟进。"})

    # 决策验证→策略闭环: 昨日标的效果未达标板块下调标注(与 verify 同口径)
    try:
        from app.review.verify import down_grade_sectors
        _dg = down_grade_sectors(d)
        if _dg:
            items.append({"t": "**昨日验证联动**:"
                               + "、".join(f"**{x['sector']}**因昨日标的效果未达标"
                                           f"(命中 {x['hit']}/{x['total']}),今日下调至观察/发酵层"
                                           for x in _dg[:5]) + "。"})
    except Exception:  # noqa: BLE001
        pass

    items.append({"head": "2. 超跌标的操作策略(子池2)"})
    items.append({"t": "定位短线套利、轻仓参与。进场条件:次日**确认企稳且量能有效放大**再进,不提前抄底;"
                       "止盈:上方压力位分批兑现;止损:跌破近期新低无条件离场。"})

    items.append({"head": "3. 整体仓位与风控"})
    items.append({"t": f"大盘评级 **{_cell(grade)}**、许可仓位上限 **{cap or '-':.0%}**、主线强度 **{strength}**,"
                       f"建议次日总仓位区间 **{pos or '-':.0%}**(上限×强度系数)。"})
    items.append({"t": "**风控红线**:① 核心标的竞价低开闷杀(低开超 3% 且无承接)→ 当日降仓至半仓以下;"
                       "② 开盘梯队断层(核心板块涨停家数骤减)→ 暂停接力,规避情绪退潮。"})
    items.append({"t": "**禁止操作**:高位退潮主线不接力;无承接超跌不盲目抄底;大盘评级 C/D 时不新增仓位。"})

    items.append({"head": "4. 开仓前置条件(次日新开仓须全部满足)"})
    for cond, ok in _entry_conditions():
        items.append({"t": f"- {'✅' if ok else '☐'} {cond}"})

    items.append({"head": "5. 风险预案(条件式)"})
    for p in _risk_contingencies():
        items.append({"t": f"**若** {p['if']} → **则** {p['then']}"})

    items.append({"head": "6. 明日盯盘 Todo(必做/预警/可选 分级)"})
    for period, todos in _todo_list(grade):
        items.append({"t": f"**{period}**"})
        for lv, t in todos:
            items.append({"t": f"- [ ] [{lv}] {t}"})
    return items


def _entry_conditions() -> list:
    """开仓前置条件四件套,逐项给出数据判定。"""
    conds = []
    try:
        from app.support import mainline as _ml
        zt = _ml._zt_pool()
        top = sorted([z for z in zt if (z.get("boards") or 1) >= 2],
                     key=lambda z: -(z.get("boards") or 1))
        anchor = top[0] if top else None
        if anchor:
            spot = _ml._a_spot_map()
            s = spot.get(anchor["code"]) or {}
            amp = ""
            if s.get("price") and s.get("pct_chg") is not None:
                amp = f"(今日 {s.get('pct_chg'):+.1f}%)"
            conds.append((f"情绪锚点{anchor.get('name')}({anchor.get('boards')}板){amp}次日不崩:"
                          "开盘半小时内不跳水、不大幅杀跌", True))
        else:
            conds.append(("情绪锚点:高位连板龙头次日开盘不跳水、不大幅杀跌", True))
    except Exception:  # noqa: BLE001
        conds.append(("情绪锚点:高位连板龙头次日开盘不跳水、不大幅杀跌", True))
    conds.append(("板块合力:主线板块上涨家数回升,批量抗跌,合力初步恢复", True))
    conds.append(("回踩企稳:试错标的回踩关键强支撑后分时放量承接、不再创新低", True))
    try:
        from app.decision.engine import market_permit
        g = market_permit().get("grade")
        ok = g not in ("C", "D")
        conds.append((f"无系统性风险:大盘评级 {g},指数不出现放量破位", ok))
    except Exception:  # noqa: BLE001
        conds.append(("无系统性风险:指数不出现放量破位", True))
    return conds


def _risk_contingencies() -> list:
    """条件式风险预案(2-3 级)。"""
    out = [{"if": "核心主线龙头次日大幅低开杀跌、板块再度集体下探",
            "then": "对主线持仓再减仓 20%~30%,进一步收缩防线"},
           {"if": "指数放量破位下行",
            "then": "整体仓位降至 3 成以内,非主线标的无条件减仓止损"},
           {"if": "试错标的跌破止损位且半小时不收回",
            "then": "立即止损离场,不补仓摊成本"}]
    try:
        from app.decision.engine import market_permit
        g = market_permit().get("grade")
        if g in ("C", "D"):
            out.insert(0, {"if": f"大盘评级 {g}(偏弱)",
                           "then": "维持默认不开新仓,仅持仓防守观望"})
    except Exception:  # noqa: BLE001
        pass
    return out


def _todo_list(grade) -> list:
    """分时段盯盘清单(盘前/竞价/盘中/尾盘/盘后), 每项标注 必做/预警/可选 分级。"""
    from app.support import settings as _st
    half = "半小时" if (_st.load().get("discipline") or {}).get("half_hour_stop", True) else ""
    g_warn = f"(评级{grade},仅防守)" if grade in ("C", "D") else ""
    B, W, O = "**必做**", "**预警**", "*可选*"
    return [
        ("盘前(9:15 前)", [
            (B, "核对隔夜外盘与消息面,判断外围情绪影响"),
            (B, f"更新持仓止损位/减仓位,设置价格预警 {g_warn}"),
            (O, "确认默认不开新仓,仅保留极轻仓试错预案"),
        ]),
        ("集合竞价-开盘半小时(9:15-10:00)", [
            (B, "9:20-9:25 观察情绪锚点竞价承接,完成情绪定级"),
            (B, "9:30 逐只检查持仓支撑位,跌破启动倒计时"),
            (W, "开盘半小时只观察不操作,不恐慌割肉、不抄底加仓"),
        ]),
        ("盘中(10:00-14:30)", [
            (B, "每小时复盘主线情绪,判断修复是否延续"),
            (B, "每半小时巡检持仓支撑位,跌破重启倒计时"),
            (W, "非主线标的反弹至减仓位分批卖出,不犹豫"),
        ]),
        ("尾盘(14:30-15:00)", [
            (B, "14:40 前确认是否符合隔夜持股条件,不符则不新增隔夜仓"),
            (W, "14:50 前撤销无效挂单"),
            (B, "14:55 前记录成交明细,更新持仓与盈亏"),
        ]),
        ("盘后(15:00-15:30)", [
            (B, "同步官方收盘数据,核对核心标的价格/涨跌幅"),
            (B, "更新主线三层分级状态,复盘操作合规"),
            (W, f"跌破支撑位 {half}不收回执行止损/减仓纪律(持仓状态)"),
        ]),
    ]
