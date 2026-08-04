"""CLI 命令行分析工具。

用法:
  python run_analyze.py 600519            # 单只股票完整分析
  python run_analyze.py 600519 000001     # 多只
  python run_analyze.py 600519 --save     # 额外保存 K 线图
  python run_analyze.py --train           # 训练模型
  python run_analyze.py --backtest        # 回测验证
"""
import argparse
import datetime as dt
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from app.analysis import analyze_light
from app import config


def _fmt(x, digits=2):
    if x is None:
        return "-"
    return f"{x:.{digits}f}"


def _color(text, code):
    return f"\033[{code}m{text}\033[0m"


def print_report(r: dict) -> None:
    name = f"{r['name']} ({r['code']})"
    print("\n" + "=" * 68)
    print(f"  {name}  · 分析时间 {r['analyzed_at']}")
    print("=" * 68)

    p = r["prediction"]
    a = r["advice"]
    print(f"\n【走势预测】未来 {config.PREDICT_HORIZON} 个交易日")
    print(f"  方向: {p['direction_cn']}    概率: {p['prob']:.1%}")
    bar = "█" * int(p["prob"] * 20)
    print(f"  上涨 {p['p_up']:.1%}  |{bar:<20}| 下跌 {p['p_down']:.1%}   (震荡 {p['p_flat']:.1%})")
    if p.get("expected_return") is not None:
        er = p["expected_return"]
        color = 31 if er > 0 else (32 if er < 0 else 33)
        print(f"  预期涨跌幅: {_color(f'{er:+.2%}', color)}"
              f"   ({'盈利' if er >= 0 else '亏损'})")
    if p.get("reward_risk") is not None:
        rr = p["reward_risk"]
        print(f"  盈亏比: {_color(f'{rr:.2f}', 31 if rr >= 1 else 32)}"
              f"   (期望盈利/期望亏损{' ≥1 划算' if rr >= 1 else ' <1 不划算'})")

    q = r.get("quote") or {}
    if q:
        print(f"\n【实时行情】{q.get('datetime', '-')}")
        print(f"  现价 {q.get('price', '-')}  涨跌 {q.get('pct_chg', 0)*100:+.2f}%  "
              f"今开 {q.get('open', '-')}  最高 {q.get('high', '-')}  最低 {q.get('low', '-')}")

    lv = a["levels"]
    print(f"\n【操作建议】{_color(a['action_cn'], 32 if a['action'] in ('buy','add') else (31 if a['action'] in ('sell','reduce') else 33))}"
          f"   (置信度 {a['confidence']:.1%}{'· 强信号' if a['strong'] else ''})")
    print(f"  空仓者: {a['entry_action']}   持仓者: {a['hold_action']}")
    print(f"  建议买入区 {lv['entry_low']} ~ {lv['entry_high']}   "
          f"目标价 {lv['target']}  止损 {lv['stop_loss']}")
    print(f"  支撑 {lv['support']}  压力 {lv['resistance']}")

    t = a["technical"]
    print(f"\n【技术面】MA20 {_fmt(t['ma20'])}  MA60 {_fmt(t['ma60'])}  RSI {_fmt(t['rsi14'])}  "
          f"MACD柱 {_fmt(t['macd_hist'], 3)}  ATR {_fmt(t['atr14'])}  量比 {_fmt(t['volume_ratio'])}")

    mk = a.get("market") or {}
    if mk.get("fear_greed") is not None or mk.get("basis_avg") is not None:
        print("\n【市场情绪】")
        if mk.get("fear_greed") is not None:
            print(f"  恐贪指标 {mk['fear_greed']:.0f}/100  ({mk.get('fear_greed_label', '-')})")
        if mk.get("basis_avg") is not None:
            print(f"  期指基差 {mk['basis_avg']:+.2%}  ({mk.get('basis_label', '-')})"
                  + (f"  IF实时基差 {mk['basis_if_live']:+.2%}" if mk.get('basis_if_live') is not None else ""))
        if mk.get("advance") is not None and mk.get("decline") is not None:
            print(f"  涨跌家数: 涨 {mk['advance']:.0f} / 跌 {mk['decline']:.0f}  (涨停 {mk['limit_up']:.0f})")
        if a.get("position_hint"):
            print(f"  建议仓位: {a['position_hint']}")

    print("\n【依据】")
    for s in a["reasons"]:
        print(f"  · {s}")
    if a["risks"]:
        print("\n【风险提示】")
        for s in a["risks"]:
            print(f"  ! {s}")

    mi = r.get("model_info") or {}
    if mi:
        mm = mi.get("metrics", {})
        print(f"\n【模型】{mi.get('model_name')}  horizon={mi.get('horizon')}  "
              f"训练于 {str(mi.get('trained_at'))[:10]}  验证准确率 {mm.get('accuracy', '-')}")
    print("=" * 68 + "\n")
    print("声明:以上为量化模型分析结果,仅供参考,不构成投资建议。股市有风险,入市需谨慎。\n")


def save_kline_chart(r: dict, path: str = None) -> str:
    """保存 K 线 + 预测概率图(matplotlib)。"""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
    from matplotlib import font_manager

    for font in ("C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/simhei.ttf"):
        try:
            font_manager.fontManager.addfont(font)
        except Exception:  # noqa: BLE001
            continue
    plt.rcParams["font.family"] = ["Microsoft YaHei", "SimHei"]
    plt.rcParams["axes.unicode_minus"] = False

    df = r["history"].tail(90)
    series = r["series"].tail(90)
    close = df["close"]
    fig, axes = plt.subplots(3, 1, figsize=(13, 9), sharex=True,
                             gridspec_kw={"height_ratios": [3, 1.2, 1.2]})
    ax = axes[0]
    ax.plot(df.index, close, color="#1f77b4", lw=1.2, label="收盘")
    for col, color in (("ma5", "#e8a33d"), ("ma20", "#d62728"), ("ma60", "#9467bd")):
        if col in df.columns:
            ax.plot(df.index, df[col], lw=0.9, color=color, label=col)
    lv = r["advice"]["levels"]
    ax.axhline(lv["target"], color="green", ls="--", lw=1, label=f"目标 {lv['target']}")
    ax.axhline(lv["stop_loss"], color="red", ls="--", lw=1, label=f"止损 {lv['stop_loss']}")
    ax.axhline(lv["support"], color="orange", ls=":", lw=1, label=f"支撑 {lv['support']}")
    ax.axhline(lv["resistance"], color="purple", ls=":", lw=1, label=f"压力 {lv['resistance']}")
    ax.set_title(f"{r['name']}({r['code']}) 走势与预测  建议:{r['advice']['action_cn']}", fontsize=13)
    ax.legend(loc="upper left", fontsize=8, ncol=4)
    ax.grid(alpha=0.3)

    ax2 = axes[1]
    ax2.plot(series.index, series["up"] * 100, color="red", lw=1, label="P(上涨)%")
    ax2.plot(series.index, series["down"] * 100, color="green", lw=1, label="P(下跌)%")
    ax2.axhline(config.BUY_P_UP * 100, color="red", ls="--", lw=0.7, alpha=0.6)
    ax2.axhline(config.SELL_P_DOWN * 100, color="green", ls="--", lw=0.7, alpha=0.6)
    ax2.set_ylim(0, 100)
    ax2.set_ylabel("概率%")
    ax2.legend(loc="upper left", fontsize=8)
    ax2.grid(alpha=0.3)

    ax3 = axes[2]
    vol = df["volume"].tail(90) / 1e6
    ax3.bar(vol.index, vol, color="#7f7f7f", width=0.8)
    ax3.set_ylabel("成交量(百万手)")
    ax3.grid(alpha=0.3)

    ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d"))
    fig.autofmt_xdate()
    fig.tight_layout()
    path = path or os.path.join(config.REPORT_DIR, f"{r['code']}_predict.png")
    fig.savefig(path, dpi=110)
    plt.close(fig)
    return path


def main(argv=None):
    parser = argparse.ArgumentParser(prog="guga", description="SilverQuant + Akshare + VectorBT 量化预测分析")
    parser.add_argument("codes", nargs="*", help="股票代码,如 600519")
    parser.add_argument("--train", action="store_true", help="训练模型")
    parser.add_argument("--backtest", action="store_true", help="对训练样本集回测")
    parser.add_argument("--save", action="store_true", help="保存 K 线预测图")
    parser.add_argument("--no-quote", action="store_true", help="不拉取实时行情")
    args = parser.parse_args(argv)

    if args.train:
        from app.ml.trainer import train_all
        train_all()
        return 0

    if args.backtest:
        from app.backtest.vbt_validate import backtest_universe
        res = backtest_universe()
        cols = ["code", "name", "total_return", "sharpe", "max_drawdown", "win_rate", "trades"]
        print(res[cols].to_string(index=False))
        print("\n平均:")
        print(res[["total_return", "sharpe", "max_drawdown", "win_rate"]].mean().round(2).to_string())
        return 0

    if not args.codes:
        parser.print_help()
        return 1

    for code in args.codes:
        try:
            r = analyze_light(code)
            print_report(r)
            if args.save:
                from app.analysis import analyze
                full = analyze(code, with_quote=not args.no_quote)
                p = save_kline_chart(full)
                print(f"图表已保存: {p}")
        except Exception as e:  # noqa: BLE001
            print(f"[错误] {code}: {e}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
