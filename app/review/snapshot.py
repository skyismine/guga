"""P2 体验与文案: 30秒速览 + 纯文本摘要输出。

- `build_snapshot(ctx)`: 报告开头浓缩 4 项核心信息(大盘定性/核心主线/操作要点/核心风险),
  由 generator 汇总各模块结论填充 ctx 后生成,兼顾速读与深度阅读。
- `plain_summary(ctx, d)`: 纯文本摘要(Markdown 剥离),供快速复制/分享(页面端一键复制)。
"""
import datetime as dt


def _cell(v) -> str:
    return str(v) if v is not None else "-"


def build_snapshot(ctx: dict) -> list:
    """生成「30秒速览」结构化 items(4 项核心信息)。"""
    mkt = ctx.get("market") or {}
    items = []
    head = ctx.get("headline") or {}
    items.append({"t": f"**大盘定性**:{_cell(mkt.get('qualify', '数据暂缺'))}"
                       f"(评级 {_cell(mkt.get('grade'))} · 量能 {_cell(mkt.get('vol_tag'))} · "
                       f"情绪 {_cell(mkt.get('emotion_tag'))} · 风格 {_cell(mkt.get('style_tag'))})"})
    items.append({"t": f"**核心主线**:{_cell(ctx.get('core_names')) or '暂无明确主线'}"
                       f",强度 **{_cell(ctx.get('strength'))}**;{_cell(ctx.get('pattern'))}"})
    items.append({"t": f"**操作要点**:{_cell(ctx.get('action'))}"})
    items.append({"t": f"**核心风险**:{_cell(ctx.get('risk'))}"})
    return items


def plain_summary(ctx: dict, d: dict = None) -> str:
    """纯文本摘要(Markdown 剥离),支持快速复制分享。"""
    date = (d or {}).get("date") or str(dt.date.today())
    mkt = ctx.get("market") or {}
    lines = [
        f"【A股每日复盘速览 {date}】",
        f"大盘: {_cell(mkt.get('qualify'))} (评级{_cell(mkt.get('grade'))}/量能{_cell(mkt.get('vol_tag'))}/情绪{_cell(mkt.get('emotion_tag'))})",
        f"核心主线: {_cell(ctx.get('core_names')) or '暂无'} (强度{_cell(ctx.get('strength'))})",
        f"操作要点: {_cell(ctx.get('action'))}",
        f"核心风险: {_cell(ctx.get('risk'))}",
        "",
        "免责声明: 以上内容仅供研究参考,不构成投资建议。股市有风险,入市需谨慎。",
    ]
    return "\n".join(lines)
