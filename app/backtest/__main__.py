# -*- coding: utf-8 -*-
"""CLI: python -m app.backtest --code 600519 [--fast 5 --slow 20] [--save]

示例: 用本地日线缓存对 600519 跑 5/20 均线交叉回测并输出报告。
"""
import argparse
import sys


def main() -> int:
    ap = argparse.ArgumentParser(description="回测框架 CLI(6.4)")
    ap.add_argument("--code", default="600519", help="标的代码(默认 600519)")
    ap.add_argument("--fast", type=int, default=5)
    ap.add_argument("--slow", type=int, default=20)
    ap.add_argument("--capital", type=float, default=1_000_000)
    ap.add_argument("--stop", type=float, default=0.05)
    ap.add_argument("--position", type=float, default=0.5)
    ap.add_argument("--days", type=int, default=600)
    ap.add_argument("--save", default=None, help="报告保存路径(默认仅打印)")
    args = ap.parse_args()

    from app.backtest import engine, metrics, report

    df = _load_bars(args.code, args.days)
    if df is None:
        print(f"标的 {args.code} 日线数据不可用(缓存缺失,请先触发抓取)")
        return 1
    sig = engine.simple_ma_signal(df, args.fast, args.slow)
    res = engine.run(df, sig, {"capital": args.capital, "stop_pct": args.stop,
                               "position_pct": args.position}, code=args.code)
    print(report.markdown(res, title=f"{args.code} MA{args.fast}/{args.slow} 回测"))
    if args.save:
        path = report.save(res, args.save, title=f"{args.code} 回测")
        print(f"\n报告已保存: {path}")
    return 0


def _load_bars(code: str, days: int):
    try:
        from app.data.fetcher import get_daily_history
        df = get_daily_history(code, days=days)
        if df is not None and len(df) and "close" in df.columns:
            return df
    except Exception as e:  # noqa: BLE001
        print(f"日线加载失败: {e}")
    return None


if __name__ == "__main__":
    sys.exit(main())
