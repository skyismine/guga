"""模块3 每日复盘报告:自动生成 Markdown 复盘文档(十大模块聚合,交易决策闭环)。

- 数据采集: `app.review.data.collect_review`(8 类市场数据,独立容错,按日期缓存)
- 规则生成: `app.review.generator.generate_review`(十大模块结构化 items,含 30秒速览/主线三层分级/
  明日观察标的池/决策效果验证/明日交易策略)
- 文案组装: LLM 增强优先(输入追加近3日趋势,输出经数值一致性校验),失败降级为结构化完整结论;
- 输出扩展: 同时返回纯文本摘要(summary)供快速复制分享;save=True 落盘 review_YYYYMMDD.md;
- P3 落库: 每次生成后将当日决策快照 + 核心指标写入 review_archive.jsonl,供次日决策验证与长周期统计。

支持定时: 交易日 16:00(settings.auto_report_time,默认收盘后确保最终收盘数据)自动生成,
统一由 Web 端 `_start_auto_report` 调度(页面内展示),落盘由 settings.need_save_report 控制。
"""
import datetime as dt
import os
import re
import time

from app import config
from app.support import settings as _st


def generate(use_cache: bool = True, save: bool = True, use_llm: bool = None,
             on_stage=None) -> dict:
    """生成复盘报告(规则主体 + LLM 叙事段)。

    架构: 全部表格/清单/方案/校验/纪律由规则层渲染(主体,数值准确);
    LLM 仅在启用时补一段「核心结论」叙事(归因/定性),不触碰数值表格;
    LLM 未启用/失败/数值校验未通过时,报告仍为完整规则主体(不降级不缺失)。
    on_stage 接收阶段文本,供页面进度展示。
    """
    from app.review.data import collect_review
    from app.review.generator import generate_review
    from app.review.snapshot import plain_summary
    from app.review import archive
    from app.review.verify import build_archive_record
    from app.support import llm as _llm

    cfg = _st.load()
    if on_stage:
        on_stage("数据采集中")
    d = collect_review(use_cache=use_cache)
    if on_stage:
        on_stage("规则生成中")
    r = generate_review(d)
    if on_stage:
        on_stage("文案组装中")
    date = str(r.get("date") or d.get("date") or dt.date.today())
    ctx = r.get("snapshot_ctx") or {}

    lines = [f"# 每日深度复盘  {date}", ""]
    llm_on = (use_llm if use_llm is not None else bool((cfg.get("llm") or {}).get("enable")))
    if llm_on:
        # LLM 只写「核心结论」叙事段: 输入含 速览指标/近3日趋势/决策验证/持仓与合规摘要
        extra = {
            "trend_3d": (d.get("market_daily") or [])[-3:],
            "decision_verify": _verify_short(r),
            "key_indicators": ctx,
            "positions_audit": _positions_short(r),
        }
        out = _llm.generate_strategy_cached(d, extra=extra)
        if out["ok"] and _llm_check(out["text"], ctx):
            lines.append(out["text"])
            lines.append("")
            lines.append(f"> 核心结论叙事由大模型基于当日盘面+持仓+操作数据生成,已通过关键数值校验"
                        f"(模型:{(_cfg_llm().get('model') or '-')})。")
            lines.append("")
        else:
            print(f"[report] LLM 核心结论降级: {out.get('reason') or '数值校验未通过'}")
    # 规则主体: 渲染全部结构化 sections(表格/清单/方案/校验/纪律/来源)
    _append_rule_fallback(lines, r)

    lines.append("")
    lines.append(f"> **免责声明**:{cfg.get('disclaimer')}")

    md = "\n".join(lines)
    path = None
    if save:
        out_dir = os.path.join(config.DATA_DIR, "reports")
        os.makedirs(out_dir, exist_ok=True)
        path = os.path.join(out_dir, f"review_{date.replace('-', '')}.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write(md)
    # P3: 决策快照 + 核心指标落库(供次日决策验证与长周期统计)
    try:
        rec = build_archive_record(d)
        archive.save_day(rec)
    except Exception as e:  # noqa: BLE001
        print(f"[report] 决策快照落库失败: {e}")
    if on_stage:
        on_stage("完成")
    return {"date": date, "path": path, "markdown": md,
            "summary": plain_summary(ctx, {"date": date})}


def _positions_short(r: dict) -> str:
    """持仓/合规摘要(供 LLM 核心结论叙事引用),缺失返回空串。"""
    parts = []
    for it in (r.get("sections") or {}).get("positions", {}).get("items", []):
        t = it.get("t", "")
        if "合规得分" in t or "总资产" in t:
            parts.append(t)
    return "；".join(parts[:3])


def _verify_short(r: dict) -> str:
    """决策验证结论摘要(供 LLM 归因),缺失返回空串。"""
    parts = []
    for it in (r.get("sections") or {}).get("verify", {}).get("items", []):
        if it.get("t") and ("命中率" in it["t"] or "准确率" in it["t"] or "达标" in it["t"]):
            parts.append(it["t"])
    return "；".join(parts[:3])


def _llm_check(text: str, ctx: dict) -> bool:
    """LLM 输出关键数值一致性校验: 恐贪指数与大盘评级须命中,否则视为异常(降级规则)。

    恐贪口径兼容两种写法: 模型可能引用整数部分(42)或四舍五入(43),任一命中即通过;
    评级字母(如 C)必须出现。设计取舍: 只校验「易产生幻觉的高价值标量」,
    板块名/文案措辞不做硬校验,避免过度约束大模型表达自由度。
    """
    if not text or len(text) < 60:
        return False
    mkt = ctx.get("market") or {}
    fg = mkt.get("fear_greed")
    grade = mkt.get("grade")
    if fg is not None:
        cands = {str(int(fg)), str(round(fg))}
        if not any(c in text for c in cands):
            return False
    if grade and grade not in text:
        return False
    return True


def _cfg_llm() -> dict:
    return _st.load().get("llm") or {}


def _append_rule_fallback(lines: list, r: dict) -> None:
    """规则兜底: 输出结构化完整结论(全部模块 items 渲染为 Markdown),而非简单文本堆砌。"""
    for sec in (r.get("sections") or {}).values():
        lines.append(f"## {sec['title']}")
        for it in sec.get("items", []):
            if "t" in it:
                lines.append(f"- {it['t']}")
            elif "head" in it:
                lines.append(f"**{it['head']}**")
            elif "table" in it:
                t = it["table"]
                if t.get("title"):
                    lines.append(f"**{t['title']}**")
                cols = t["cols"]
                lines.append("| " + " | ".join(cols) + " |")
                lines.append("|" + "|".join(["---"] * len(cols)) + "|")
                for row in t["rows"]:
                    cells = []
                    for c in row:
                        cells.append(str(c.get("v", "")) if isinstance(c, dict) else str(c))
                    lines.append("| " + " | ".join(cells) + " |")
            lines.append("")


def _last_report_date() -> str:
    d = os.path.join(config.DATA_DIR, "reports")
    if not os.path.isdir(d):
        return ""
    files = sorted(f for f in os.listdir(d) if f.startswith("review_"))
    return files[-1][7:15] if files else ""


if __name__ == "__main__":
    import json
    res = generate(use_cache=False)
    print(json.dumps({"path": res["path"], "chars": len(res["markdown"]),
                      "summary_chars": len(res["summary"])}, ensure_ascii=False))
