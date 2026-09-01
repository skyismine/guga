"""复盘情绪定级统一口径: 单一函数供各模块复用, 消除 中性/高温/亢奋/多头 标签打架。

设计原则:
- 温度以恐贪指数为主(缺失时以涨停家数兜底), 保证 30秒速览 / 大盘综述 / 资金情绪面 三处口径一致;
- 高位分歧修正: 炸板次数 > 涨停家数 且 涨停家数 >= 50 时, 情绪温度高但兑现/分歧大,
  追加「·高位分歧」标注, 避免仅凭涨停家数误判为单纯高温/亢奋。
"""


def emotion_zone(fg=None, lu_n=None, zhadan_n=None, max_lian=None):
    """统一情绪定级。返回 (zone, diverge)。

    zone: 寒冷/低温/中性/高温/过热(恐贪优先,涨停家数兜底);
    diverge: True 表示高位分歧(炸板 > 涨停 且 涨停家数偏高),需在展示处追加标注。
    """
    if fg is not None:
        if fg >= 75:
            zone = "过热"
        elif fg >= 60:
            zone = "高温"
        elif fg >= 40:
            zone = "中性"
        elif fg >= 25:
            zone = "低温"
        else:
            zone = "寒冷"
    elif lu_n is not None:
        zone = "高温" if lu_n >= 60 else ("中性" if lu_n >= 30 else "低温")
    else:
        zone = "中性"
    diverge = (zhadan_n is not None and lu_n is not None
               and zhadan_n > lu_n and lu_n >= 50)
    return zone, diverge


def emotion_label(fg=None, lu_n=None, zhadan_n=None, max_lian=None) -> str:
    """最终展示标签(zone 或 zone·高位分歧), 供速览/情绪行直接引用。"""
    zone, diverge = emotion_zone(fg=fg, lu_n=lu_n, zhadan_n=zhadan_n, max_lian=max_lian)
    return f"{zone}·高位分歧" if diverge else zone


def vol_label(amount_yi, prev_yi=None, percentile=None) -> str:
    """统一量能定级(放量/缩量/平量), 消除「较昨日缩量」与「近5日均量平量」两套口径打架。

    口径: 优先较昨日环比(±3% 为放量/缩量阈值, 与「较昨日缩量/放量 N 亿元」表述一致);
    昨日数据缺失时回退近30日成交分位(≥80% 放量 / ≤25% 缩量), 再缺返回平量。
    """
    if amount_yi and prev_yi:
        chg = amount_yi / prev_yi - 1
        if chg >= 0.03:
            return "放量"
        if chg <= -0.03:
            return "缩量"
        return "平量"
    if percentile is not None:
        if percentile >= 0.8:
            return "放量"
        if percentile <= 0.25:
            return "缩量"
        return "平量"
    return "平量"
