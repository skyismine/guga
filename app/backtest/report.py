# -*- coding: utf-8 -*-
"""回测报告生成(6.4): Markdown 报告(权益曲线/回撤曲线/指标/成交明细)。"""
import datetime as dt
import os


def ascii_curve(values, width: int = 60) -> list:
    """简单 ASCII 权益曲线(文字版, 不依赖图表库)。"""
    if not values:
        return []
    lo, hi = min(values), max(values)
    rng = (hi - lo) or 1.0
    idx = len(values) - 1
    out = []
    step = max(1, len(values) // width)
    pts = values[::step]
    if pts[-1] != values[-1]:
        pts.append(values[-1])
    for p in pts:
        bar = int((p - lo) / rng * 20)
        out.append(f"{'#' * bar}{' ' * (20 - bar)} {p:,.0f}")
    return out


def markdown(result: dict, title: str = "回测报告") -> str:
    """生成 Markdown 报告。result 为 backtest.engine.run() 输出。"""
    m = result.get("metrics") or {}
    trades = result.get("trades") or []
    eq = [e["equity"] for e in result.get("equity") or []]
    p = result.get("params") or {}
    lines = [
        f"# {title}",
        "",
        f"> 标的: {result.get('code')} · 初始资金 {p.get('capital', 0):,.0f} 元"
        f" · 佣金 {p.get('commission', 0):.4%} / 印花税 {p.get('stamp_tax', 0):.4%}"
        f" / 滑点 {p.get('slippage', 0):.4%} / 涨跌停 {p.get('limit', 0):.0%}"
        f" · 仓位 {p.get('position_pct', 0):.0%} · 止损 {p.get('stop_pct', 0):.0%}"
        f"{' · 移动止损' if p.get('trail') else ''}",
        "",
        "## 权益曲线",
        "```",
        *ascii_curve(eq),
        "```",
        "",
        "## 指标",
        "",
        "| 指标 | 值 |",
        "|---|---|",
        f"| 累计收益率 | {m.get('cum_return', 0):.2%} |",
        f"| 年化收益率 | {m.get('annual_return', 0):.2%} |",
        f"| 超额收益率 | {m.get('excess_return', '-') if m.get('excess_return') is not None else '-'} |",
        f"| 最大回撤 | {m.get('max_drawdown', 0):.2%} |",
        f"| 夏普比率 | {m.get('sharpe', '-')} |",
        f"| 索提诺比率 | {m.get('sortino', '-')} |",
        f"| 卡尔玛比率 | {m.get('calmar', '-')} |",
        f"| 胜率 | {m.get('win_rate', 0):.1%} |",
        f"| 盈亏比(利润因子) | {m.get('profit_factor', '-')} |",
        f"| 平均持仓周期 | {m.get('avg_holding_days', '-')} 天 |",
        f"| 交易次数 | {m.get('trade_count', 0)} |",
        f"| 止损占比 | {m.get('stop_ratio', 0):.1%} |",
        "",
    ]
    if trades:
        lines += ["## 成交明细", "",
                  "| 买入日 | 卖出日 | 买入价 | 卖出价 | 股数 | 盈亏 | 原因 |",
                  "|---|---|---|---|---|---|---|"]
        for t in trades[-30:]:
            lines.append(f"| {t.get('entry')} | {t.get('exit')} | {t.get('entry_px')} | "
                         f"{t.get('exit_px')} | {t.get('shares'):,.0f} | {t.get('pnl'):,.0f} | {t.get('reason')} |")
        lines.append("")
    from app.backtest import metrics
    lines.append("## 分层统计(按平仓原因)")
    lines.append("")
    lines.append("| 类别 | 次数 | 胜率 | 累计盈亏 |")
    lines.append("|---|---|---|---|")
    for k, v in metrics.by_signal_group(trades).items():
        lines.append(f"| {k} | {v['count']} | {v['win_rate']:.1%} | {v['total_pnl']:,.0f} |")
    lines.append("")
    lines.append(f"> 生成时间 {dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')} · 仅供研究参考,不构成投资建议")
    return "\n".join(lines)


def save(result: dict, path: str = None, title: str = "回测报告") -> str:
    """生成并保存 Markdown 报告。默认存 data_cache/reports/backtest_{时间}.md。"""
    if path is None:
        path = os.path.join(_report_dir(), f"backtest_{dt.datetime.now().strftime('%Y%m%d_%H%M%S')}.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(markdown(result, title))
    return path


def _report_dir() -> str:
    from app import config
    d = os.path.join(config.DATA_DIR, "reports")
    os.makedirs(d, exist_ok=True)
    return d
