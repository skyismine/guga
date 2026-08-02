"""基于 SilverQuant 架构的预测策略入口(对齐 run_ai_gen.py 模式)。

用法:
  python run_predict.py            # 历史回放验证(虚拟账户)
  python run_predict.py live       # 交易时段实盘模拟(虚拟账户)
  python run_predict.py 600519     # 单只股票命令行分析

实盘切换:将 PaperDelegate 替换为 SilverQuant 的
  XtDelegate(QMT 实盘) 或 GmDelegate(掘金模拟盘) 即可。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.strategy.runner import run_live, run_replay

DEFAULT_POOL = ["600519", "601318", "600036", "000001", "300750", "000858", "601012"]


def main():
    args = sys.argv[1:]
    if args and args[0] == "live":
        interval = int(args[1]) if len(args) > 1 else 60
        run_live(DEFAULT_POOL, interval_sec=interval)
    elif args and args[0] not in ("replay",):
        from app.cli.analyze import main as cli_main
        sys.exit(cli_main(args))
    else:
        res = run_replay(DEFAULT_POOL)
        print(f"\n[回放] 池 {res['pool']}  周期 {res['period']}")
        print(f"[回放] 期末净值 {res['final_equity']:,.2f}  收益 {res['total_return']:.2%}  "
              f"基准 {res['benchmark_return']:.2%}  最大回撤 {res['max_drawdown']:.2%}  "
              f"交易 {res['n_trades']} 次")


if __name__ == "__main__":
    main()
