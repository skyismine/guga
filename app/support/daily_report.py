"""模块3 每日复盘报告:自动生成 Markdown 复盘文档(五大模块聚合)。

- 大盘与情绪复盘(复用已有复盘引擎)
- 主线板块与龙头/ETF
- 持仓诊断与操作建议
- 风控校验结果
- 明日操作计划 + 免责声明

支持定时:Web 启动后可调用 schedule() 开启后台线程,交易日 15:30 自动生成。
"""
import datetime as dt
import os
import threading
import time

from app import config
from app.support import settings as _st


def _md_table(headers: list, rows: list) -> str:
    if not rows:
        return ""
    lines = ["| " + " | ".join(str(h) for h in headers) + " |",
             "|" + "---|" * len(headers)]
    for r in rows:
        lines.append("| " + " | ".join(str(r.get(h, "")) for h in headers) + " |")
    return "\n".join(lines)


def _fmt_pct(v, digits=1):
    try:
        return f"{float(v):+.{digits}f}%"
    except (TypeError, ValueError):
        return str(v or "")


def generate(use_cache: bool = True, save: bool = True) -> dict:
    """生成当日复盘 Markdown。save=True 落盘,否则仅返回内容(供页面直接展示)。"""
    from app.review.data import collect_review
    from app.review.generator import generate_review
    from app.support import mainline as ml
    from app.support import portfolio as pf

    cfg = _st.load()
    d = collect_review(use_cache=use_cache)
    r = generate_review(d)
    date = str(r.get("date") or d.get("date") or dt.date.today())

    lines = [f"# 每日量化复盘报告  {date}", ""]
    lines.append("> 生成方式:五大模块(复盘/主线/持仓/报告/风控)自动聚合,仅供参考,不构成投资建议。")
    lines.append("")

    # ---- 一、大盘与情绪复盘
    lines.append("## 一、大盘与情绪复盘")
    lines.append("")
    act = r["tables"].get("activity", {})
    if act:
        lines.append(f"- 上涨 {act.get('advance', '-')} 家 / 下跌 {act.get('decline', '-')} 家 / 涨停 {act.get('limit_up', '-')} 家 / 跌停 {act.get('limit_down', '-')} 家,赚钱效应 {act.get('activity_pct', '-')}%")
        lines.append("")
    top = r["tables"].get("sector_in_top", [])
    if top:
        lines.append("**主力净流入前 10 板块:**")
        lines.append(_md_table(["板块", "涨幅", "净流入(亿)", "领涨股"],
                               [{"板块": s["industry"], "涨幅": _fmt_pct(s["pct_chg"]),
                                 "净流入(亿)": s["net_yi"], "领涨股": s["leader"]} for s in top]))
        lines.append("")
    for sec in ("events", "conclusion", "strategy"):
        if sec in r["sections"]:
            lines.append(f"**{r['sections'][sec]['title']}**")
            for line in r["sections"][sec]["lines"]:
                lines.append(f"- {line}")
            lines.append("")

    # ---- 二、主线板块与龙头
    lines.append("## 二、主线板块识别与龙头/ETF")
    lines.append("")
    try:
        msum = ml.mainline_summary()
        fgl = msum.get("fear_greed")
        lines.append(f"- 市场恐贪指数: **{fgl}** ({msum.get('fear_greed_label', '-')}),Top {msum.get('top_n')} 为潜在核心主线。")
        lines.append("")
        for it in msum["items"]:
            tag = {"core": "核心主线", "branch": "补涨支线", "watch": "观察"}.get(it["level"], "观察")
            lines.append(f"### {it['rank']}. {it['name']} 【{tag}】 score={it['score']}")
            lines.append(f"- 板块涨幅 {_fmt_pct(it['pct_chg'])},净流入 {it['net_yi']} 亿,涨停 {it['zt_count']} 家,领涨 {it.get('leader', '-')}")
            if it.get("news_hits"):
                lines.append(f"- 消息催化:电报命中 {it['news_hits']} 次")
            t = it.get("targets") or {}
            for s in t.get("stocks", []):
                role = s.get("role")
                extra = f",涨 {_fmt_pct(s.get('pct_chg'))}" if s.get("pct_chg") is not None else ""
                extra += f",成交 {s.get('amount_yi', '')} 亿" if s.get("amount_yi") else ""
                if s.get("p_up") is not None:
                    extra += f",上涨概率 {s['p_up']:.0%},建议 {s.get('action', '')}"
                if s.get("levels"):
                    extra += f",支撑 {s['levels'].get('support','')} / 压力 {s['levels'].get('resistance','')}"
                lines.append(f"  - **{role}**: {s.get('name')} ({s.get('code','')}){extra}")
            lines.append("")
        pool = msum.get("oversold") or []
        if pool:
            lines.append("### 超跌强承接候选(近30日超跌 + 放量企稳)")
            lines.append(_md_table(
                ["名称", "代码", "30日跌幅", "当日涨幅", "量比", "ATR%", "上涨概率", "建议"],
                [{"名称": p.get("name"), "代码": p.get("code"),
                  "30日跌幅": _fmt_pct(p.get("ret30", 0) * 100),
                  "当日涨幅": _fmt_pct(p.get("pct_chg")),
                  "量比": p.get("vol_ratio"), "ATR%": f"{p.get('atr_pct',0)*100:.1f}",
                  "上涨概率": f"{p.get('p_up', 0):.0%}" if p.get("p_up") is not None else "",
                  "建议": p.get("action", "")} for p in pool]))
            lines.append("")
    except Exception as e:  # noqa: BLE001
        lines.append(f"- 主线识别失败: {e}")
        lines.append("")

    # ---- 三、持仓诊断
    lines.append("## 三、持仓诊断与操作建议")
    lines.append("")
    try:
        diag = pf.diagnose()
        rows = diag["positions"]
        lines.append(f"- 共 {diag['summary']['count']} 只持仓,总市值 {diag['summary']['total_market_value']:,.0f} 元,风险评级 **{diag['summary']['risk_rating']}**。")
        for tip in diag["summary"].get("risk_tips", []):
            lines.append(f"  - ⚠ {tip}")
        lines.append("")
        for p in rows:
            if not p.get("ok"):
                lines.append(f"- {p['code']} 诊断失败: {p.get('error')}")
                continue
            lines.append(f"### {p['name']} ({p['code']}) {p['category']}")
            lines.append(f"- 现价 {p['price']},浮盈 **{p['pnl_pct']*100:+.1f}%**,市值 {p['market_value']:,.0f} 元(占 {p['weight']*100:.1f}%),板块:{p.get('sector') or '未知'}")
            if p.get("levels"):
                lines.append(f"- 支撑 {p['levels'].get('support','-')} / 压力 {p['levels'].get('resistance','-')} / 止损 {p['levels'].get('stop_loss','-')} / 目标 {p['levels'].get('target','-')}")
            lines.append(f"- 模型:上涨概率 {p['prediction']['p_up']:.0%},建议 **{p['advice_action_cn']}**")
            lines.append(f"- **操作方案**: {p['plan']}")
            for reason in p.get("reasons", [])[:3]:
                lines.append(f"  - 依据:{reason}")
            lines.append("")
    except Exception as e:  # noqa: BLE001
        lines.append(f"- 持仓诊断失败: {e}")
        lines.append("")

    # ---- 四、风控
    lines.append("## 四、风控校验")
    lines.append("")
    try:
        from app.support.risk import position_rating
        res = position_rating()
        lines.append(f"- 总仓位 {res['total_pct']*100:.1f}% (上限 {res['max_total_pct']*100:.0f}%),风险评级 **{res['rating']}**,恐贪指数 {res.get('fear_greed')}")
        lines.append(f"- 单只超限 {len(res['single_violations'])} 项,板块超限 {len(res['sector_violations'])} 项,加仓受限 {len(res['add_violations'])} 项")
        for tip in res["tips"]:
            lines.append(f"  - ⚠ {tip}")
        if not res["tips"]:
            lines.append("- 无风控违规,仓位结构健康。")
        lines.append("")
    except Exception as e:  # noqa: BLE001
        lines.append(f"- 风控校验失败: {e}")
        lines.append("")

    # ---- 五、明日操作计划
    lines.append("## 五、明日操作计划(建议)")
    lines.append("")
    lines.append("1. **主线**以资金/涨停家数最强的 1-2 条为核心,优先在情绪龙头回调与中军低吸中做确定性;补涨支线仅做快进快出。")
    lines.append("2. **持仓**按第三节方案执行:深度套牢做差价、盈利持仓执行移动止损止盈、观望持仓等待触发条件。")
    lines.append("3. **风控**严格执行:单只 ≤ 总资金 10%,单一板块 ≤ 30%,总仓位 ≤ 恐贪档位上限;浮亏超 20% 禁止补仓摊薄。")
    lines.append("4. 主线标的开仓前用风控 `max_position` 校验可买仓位,禁止情绪化满仓。")
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
    return {"date": date, "path": path, "markdown": md}


def _last_report_date() -> str:
    d = os.path.join(config.DATA_DIR, "reports")
    if not os.path.isdir(d):
        return ""
    files = sorted(f for f in os.listdir(d) if f.startswith("review_"))
    return files[-1][7:15] if files else ""


def schedule(interval_sec: int = 60) -> threading.Thread:
    """后台线程:交易日到点(settings.auto_report_time)自动生成报告(当日仅一次)。"""
    cfg = _st.load()

    def _run():
        while True:
            now = dt.datetime.now()
            target = cfg.get("auto_report_time", "15:30")
            try:
                hh, mm = map(int, target.split(":"))
            except ValueError:
                hh, mm = 15, 30
            if (now.weekday() < 5 and now.hour == hh and now.minute == mm
                    and _last_report_date() != now.strftime("%Y%m%d")):
                try:
                    res = generate(use_cache=True)
                    print(f"[report] 已生成复盘: {res['path']}")
                except Exception as e:  # noqa: BLE001
                    print(f"[report] 生成失败: {e}")
            time.sleep(interval_sec)

    t = threading.Thread(target=_run, daemon=True, name="daily-report")
    t.start()
    return t


if __name__ == "__main__":
    import json
    res = generate(use_cache=False)
    print(json.dumps({"path": res["path"], "chars": len(res["markdown"])}, ensure_ascii=False))
