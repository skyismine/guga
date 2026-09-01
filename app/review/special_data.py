"""P3 复盘增强: 同花顺特色数据(连板梯队 / 热榜 / 龙虎榜 / 个股异动)。

复用 fuyao 官方 API(settings.fuyao.enabled),全部模块独立容错:
- fuyao 未启用/任一接口失败 → 该子块降级为「暂缺」,不阻塞报告。
- 数据语义: 热榜/飙升榜为当日人气与热度排名(短线情绪);龙虎榜为资金行为(机构/游资);
  连板天梯为近30日连板矩阵(梯队完整度与次日晋级);异动原因为当日异动解读(题材归因)。
"""
import datetime as dt


def _cell(v) -> str:
    """单元格文本: None→-,并折叠换行/连续空白(防拆坏 Markdown 表格行)。"""
    return " ".join(str(v).split()) if v is not None else "-"


_BOARD_LABEL = {"two_board": "2连板", "three_board": "3连板", "four_board": "4连板",
                "five_board": "5连板", "six_board": "6连板", "seven_over": "7板及以上"}
_BOARD_NUM = {"two_board": 2, "three_board": 3, "four_board": 4,
              "five_board": 5, "six_board": 6, "seven_over": 7}


def _ladder_review() -> dict:
    """连板梯队摘要: 最近交易日各板位家数 + 最高板 + 次日晋级观察(seal_nextday)。

    注意: fuyao limit-up-ladder 返回近30日矩阵且按日期「新→旧」排列,
    最近一个交易日必须取最新日期(取列表末尾会把旧日期当今日)。
    """
    from app.data.fuyao import get_limit_up_ladder, enabled
    if not enabled():
        return {}
    lad = get_limit_up_ladder()
    if not lad:
        return {}
    last = max(lad, key=lambda x: x.get("date", ""))   # 最近交易日(列表新→旧,末尾是最旧的一天)
    date = last.get("date", "")
    boards = last.get("boards") or {}
    counts = {}
    promote = []
    for key in _BOARD_NUM:
        arr = boards.get(key) or []
        if arr:
            counts[key] = len(arr)
            for s in arr[:4]:
                if s.get("seal_nextday") is True:
                    promote.append(s.get("name"))
    max_board = max([_BOARD_NUM[k] for k in counts], default=0)
    return {"date": date, "counts": counts, "max_board": max_board,
            "promote": promote[:6]}


def _hot_review() -> list:
    """热股榜 Top(人气题材方向)。"""
    from app.data.fuyao import get_hot_stock_list, enabled
    if not enabled():
        return []
    return get_hot_stock_list("day")[:10]


def _dragon_review() -> list:
    """龙虎榜: 净买入居前标的(资金行为)。"""
    from app.data.fuyao import get_dragon_tiger_list, enabled
    if not enabled():
        return []
    d = get_dragon_tiger_list("all")
    items = d.get("stock_items") or []
    return sorted([s for s in items if s.get("net_value")],
                  key=lambda x: -float(x.get("net_value") or 0))[:8]


def _anomaly_review() -> list:
    """当日异动原因(题材归因,按异动标签聚拢)。"""
    from app.data.fuyao import get_anomaly_analysis_list, enabled
    if not enabled():
        return []
    return get_anomaly_analysis_list()[:8]


def special_data_review(d: dict) -> list:
    """生成「同花顺特色数据」结构化 items(连板梯队 / 热榜 / 龙虎榜 / 异动)。"""
    items = []

    ladder = _ladder_review()
    if ladder:
        items.append({"head": "连板梯队(近30日矩阵,收盘)"})
        counts_txt = ("、".join(f"{_BOARD_LABEL.get(k, k)} {v} 只" for k, v in ladder.get("counts", {}).items())
                      or "无连板")
        items.append({"t": f"梯队日期 {ladder.get('date', '')}(最近交易日,最高 **{ladder.get('max_board', 0)} 板**):{counts_txt};"
                           f"次日晋级观察(今日封板/明日确认):"
                           + ("、".join(f"**{n}**" for n in ladder.get("promote", [])) or "无") + "。"})
    else:
        items.append({"head": "连板梯队(近30日矩阵,收盘)"})
        items.append({"t": "连板梯队数据暂缺(fuyao 未启用或接口不可用)。"})

    hot = _hot_review()
    if hot:
        items.append({"head": "同花顺热股榜 Top10(人气题材方向)"})
        rows = [[str(i + 1), it.get("name"), _cell(it.get("rank")),
                 _cell(f"{it.get('rank_change', 0):+d}" if isinstance(it.get("rank_change"), int) else "-"),
                 it.get("rank_trend", "-")] for i, it in enumerate(hot)]
        items.append({"table": {"title": "", "cols": ["#", "热股", "热度排名", "排名变动", "趋势"],
                                "rows": rows}})
    else:
        items.append({"head": "同花顺热股榜 Top10"})
        items.append({"t": "热股榜数据暂缺。"})

    dragon = _dragon_review()
    if dragon:
        items.append({"head": "龙虎榜净买入居前(资金行为)"})
        rows = [[it.get("name"), it.get("ticker"),
                 _cell(f"{it.get('net_value', 0) / 1e8:.2f}亿" if it.get("net_value") else "-"),
                 _cell(f"{float(it.get('net_rate') or 0) * 100:.1f}%"),
                 _cell((it.get("limit_reason") or "-")[:14])] for it in dragon]
        items.append({"table": {"title": "", "cols": ["标的", "代码", "龙虎榜净买入", "净买占比", "上榜原因"],
                                "rows": rows}})
    else:
        items.append({"head": "龙虎榜净买入居前"})
        items.append({"t": "龙虎榜数据暂缺。"})

    anomaly = _anomaly_review()
    if anomaly:
        items.append({"head": "当日个股异动(题材归因)"})
        rows = [[it.get("stock_name"), _cell(it.get("tag_name")),
                 _cell((it.get("keyword_list") or []) and "、".join(it.get("keyword_list")[:4])),
                 _cell(it.get("analysis_content") or "-")] for it in anomaly[:6]]
        items.append({"table": {"title": "", "cols": ["标的", "异动", "题材关键词", "解读"],
                                "rows": rows}})
    else:
        items.append({"head": "当日个股异动"})
        items.append({"t": "异动数据暂缺。"})
    return items
