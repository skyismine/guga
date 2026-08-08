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


def generate(use_cache: bool = True, save: bool = True, use_llm: bool = None) -> dict:
    """把当日盘面核心数据交给大模型 API,生成专业深度复盘文案并直接输出。

    save=True 落盘,否则仅返回内容(供页面直接展示)。
    use_llm=None 表示按 settings.llm.enable 自动决定;大模型未启用或失败时降级为规则复盘结论。
    """
    from app.review.data import collect_review
    from app.review.generator import generate_review
    from app.support import llm as _llm

    cfg = _st.load()
    d = collect_review(use_cache=use_cache)
    r = generate_review(d)
    date = str(r.get("date") or d.get("date") or dt.date.today())

    lines = [f"# 每日深度复盘  {date}", ""]
    llm_on = (use_llm if use_llm is not None else bool((cfg.get("llm") or {}).get("enable")))
    if llm_on:
        out = _llm.generate_strategy_cached(d)
        if out["ok"]:
            lines.append(out["text"])
            lines.append("")
            lines.append(f"> 文案由大模型基于当日盘面核心数据自动生成(模型:{(_cfg_llm().get('model') or '-')})。")
        else:
            lines.append("> 大模型文案生成失败,已降级为规则复盘结论:")
            lines.append(f"> ({out['reason'] or '未知原因'})")
            lines.append("")
            _append_rule_fallback(lines, r)
    else:
        lines.append("> 大模型文案未启用(可在系统设置开启并填写 API 配置),当前展示规则复盘结论。")
        lines.append("")
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
    return {"date": date, "path": path, "markdown": md}


def _cfg_llm() -> dict:
    return _st.load().get("llm") or {}


def _append_rule_fallback(lines: list, r: dict) -> None:
    """规则兜底:直接复用深度复盘中的「盘面核心结论 + 操作策略」作为结论文案。"""
    for key in ("conclusion", "strategy"):
        sec = (r.get("sections") or {}).get(key)
        if sec:
            lines.append(f"**{sec['title']}**")
            for it in sec["items"]:
                if "head" in it:
                    lines.append(f"- {it['head']}")
                elif "t" in it:
                    lines.append(f"- {it['t']}")
            lines.append("")


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
