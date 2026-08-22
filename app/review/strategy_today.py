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

    items.append({"head": "2. 超跌标的操作策略(子池2)"})
    items.append({"t": "定位短线套利、轻仓参与。进场条件:次日**确认企稳且量能有效放大**再进,不提前抄底;"
                       "止盈:上方压力位分批兑现;止损:跌破近期新低无条件离场。"})

    items.append({"head": "3. 整体仓位与风控"})
    items.append({"t": f"大盘评级 **{_cell(grade)}**、许可仓位上限 **{cap or '-':.0%}**、主线强度 **{strength}**,"
                       f"建议次日总仓位区间 **{pos or '-':.0%}**(上限×强度系数)。"})
    items.append({"t": "**风控红线**:① 核心标的竞价低开闷杀(低开超 3% 且无承接)→ 当日降仓至半仓以下;"
                       "② 开盘梯队断层(核心板块涨停家数骤减)→ 暂停接力,规避情绪退潮。"})
    items.append({"t": "**禁止操作**:高位退潮主线不接力;无承接超跌不盲目抄底;大盘评级 C/D 时不新增仓位。"})
    return items
