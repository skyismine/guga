"""运行器:实盘轮询模式 + 历史回放验证模式。

实盘模式:交易时段内按间隔轮询实时行情,驱动 scan_buy/scan_sell(虚拟账户)。
回放模式:用历史日线按日回放策略逻辑(预测序列预先计算,避免前视),
验证预测信号接入 SilverQuant 流程后的组合表现。
"""
import datetime as dt
import math
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pandas as pd

from app import config
from app.data.fetcher import (get_daily_history, get_spot_quotes,
                              is_trading_time)
from app.features.indicators import compute_features
from app.features.market_features import attach_market_features
from app.features.industry_features import prepare_features
from app.ml.predictor import Predictor
from app.strategy.paper_delegate import PaperDelegate
from app.strategy.prediction_strategy import PredictionPool, PredictionStrategy


def run_live(pool_codes, interval_sec: int = 60, horizon=None, stop_loss_pct: float = 0.06):
    """交易时段内循环: 轮询行情 -> 执行买卖 -> 盘后结算。"""
    pool = PredictionPool(pool_codes)
    delegate = PaperDelegate(strategy_name="预测策略[模拟]")
    strategy = PredictionStrategy(pool, delegate, horizon=horizon)
    print(f"[启动] 虚拟账户余额 {delegate.check_asset().cash:.0f} 元,"
          f"持仓 {len(delegate.check_positions())} 只")

    while True:
        now = dt.datetime.now()
        if now.weekday() >= 5:
            print("[等待] 非交易日...")
            time.sleep(600)
            continue

        if is_trading_time(now):
            curr_date = now.strftime("%Y%m%d")
            curr_time = now.strftime("%H:%M")
            try:
                quotes = get_spot_quotes(pool.get_code_list())
            except Exception as e:  # noqa: BLE001
                print(f"[行情] 获取失败: {e}")
                time.sleep(interval_sec)
                continue
            n_buy = strategy.scan_buy(quotes, curr_date)
            n_sell = strategy.scan_sell(quotes, curr_date)
            total = delegate.mark_to_market(quotes)
            print(f"[{curr_time}] 买入 {n_buy} / 卖出 {n_sell}  净值 {total:,.0f} 元")
        elif now.time() >= dt.time(15, 5):
            try:
                quotes = get_spot_quotes(pool.get_code_list())
            except Exception:  # noqa: BLE001
                quotes = {}
            equity = delegate.daily_settle(quotes)
            print(f"[结算] 今日净值 {equity:,.0f} 元,持仓 {len(delegate.check_positions())} 只")
            print("[结束] 当日模拟结束,明日 9:30 自动恢复。")
            return
        time.sleep(interval_sec)


def run_replay(pool_codes, init_cash: float = 100_000.0, horizon=None,
               buy_threshold: float = None, sell_threshold: float = None,
               stop_loss_pct: float = 0.06) -> dict:
    """历史回放:逐日驱动虚拟账户,输出收益曲线与绩效指标。"""
    buy_threshold = buy_threshold or config.BUY_P_UP
    sell_threshold = sell_threshold or config.SELL_P_DOWN
    predictor = Predictor(horizon)
    delegate = PaperDelegate(init_cash=init_cash, strategy_name="回放验证")
    pool = PredictionPool(pool_codes)
    slot_capacity = pool.slot_capacity

    history = {}
    series = {}
    for code in pool.get_code_list():
        df = get_daily_history(code, days=config.HIST_DAYS, adjust="qfq")
        features = prepare_features(df, code)
        history[code] = df
        series[code] = predictor.predict_series(features).dropna()

    # 对齐到公共交易日
    common_dates = None
    for s in series.values():
        common_dates = s.index if common_dates is None else common_dates.intersection(s.index)
    common_dates = list(common_dates)

    equity_curve = {}
    trades = []
    for i, date in enumerate(common_dates):
        for code in pool.get_code_list():
            df, s = history[code], series[code]
            if date not in s.index or date not in df.index:
                continue
            row = s.loc[date]
            close = float(df.loc[date, "close"])
            prev_close = float(df["close"].shift(1).loc[date])
            held = next((p for p in delegate.check_positions() if p.stock_code == code), None)

            if held is None:
                if row["up"] >= buy_threshold and row["up"] > row["down"]:
                    volume = math.floor(slot_capacity / close / 100) * 100
                    if volume > 0 and volume * close <= delegate.check_asset().cash:
                        delegate.order_market_open(code, close, volume, "回放买入")
                        trades.append({"date": date, "code": code, "side": "买",
                                       "price": close, "volume": volume})
            else:
                sell_reason = None
                if row["down"] >= sell_threshold and row["down"] > row["up"]:
                    sell_reason = "模型看跌"
                elif close < held.avg_price * (1 - stop_loss_pct):
                    sell_reason = "止损"
                if sell_reason and held.can_use_volume > 0:
                    delegate.order_market_close(code, close, held.can_use_volume, sell_reason)
                    trades.append({"date": date, "code": code, "side": "卖",
                                   "price": close, "volume": held.can_use_volume,
                                   "reason": sell_reason})
        # 模拟 T+1 结算
        quotes = {code: {"price": float(history[code].loc[date, "close"])} for code in pool.get_code_list()
                  if date in history[code].index}
        equity_curve[date] = delegate.daily_settle(quotes)

    # 收尾清仓
    for p in delegate.check_positions():
        if p.can_use_volume > 0:
            code = p.stock_code
            last_close = float(history[code]["close"].iloc[-1])
            delegate.order_market_close(code, last_close, p.can_use_volume, "回放结束")
            trades.append({"date": common_dates[-1], "code": code, "side": "卖",
                           "price": last_close, "volume": p.can_use_volume, "reason": "回放结束"})

    equity = pd.Series(equity_curve).sort_index()
    final_equity = float(equity.iloc[-1]) if len(equity) else init_cash
    total_ret = final_equity / init_cash - 1

    # 基准(等权买入持有)
    bench = None
    for code in pool.get_code_list():
        df = history[code].reindex(equity.index).ffill()
        r = df["close"] / df["close"].iloc[0] - 1
        bench = r if bench is None else bench + r
    bench_ret = float((bench / len(pool.get_code_list())).iloc[-1]) if bench is not None else None

    drawdown = float(((equity / equity.cummax()) - 1).min()) if len(equity) else 0.0
    wins = [t for t in trades if t["side"] == "卖" and t.get("reason") != "回放结束"]

    return {
        "pool": pool.get_code_list(),
        "period": f"{equity.index[0].date()} ~ {equity.index[-1].date()}" if len(equity) else "-",
        "days": len(equity),
        "init_cash": init_cash,
        "final_equity": round(final_equity, 2),
        "total_return": round(total_ret, 4),
        "benchmark_return": round(bench_ret, 4) if bench_ret is not None else None,
        "max_drawdown": round(drawdown, 4),
        "trades": trades,
        "n_trades": len(trades),
        "equity_curve": equity,
    }


if __name__ == "__main__":
    codes = os.environ.get("GUGA_POOL", "600519,601318,600036,000001,300750").split(",")
    mode = sys.argv[1] if len(sys.argv) > 1 else "replay"
    if mode == "live":
        run_live(codes, interval_sec=int(sys.argv[2]) if len(sys.argv) > 2 else 60)
    else:
        res = run_replay(codes)
        print(f"\n[回放] 池: {res['pool']}")
        print(f"[回放] 周期 {res['period']} ({res['days']} 交易日)")
        print(f"[回放] 期末净值 {res['final_equity']:,.2f}  收益 {res['total_return']:.2%}  "
              f"基准(等权买入持有) {res['benchmark_return']:.2%}")
        print(f"[回放] 最大回撤 {res['max_drawdown']:.2%}  交易次数 {res['n_trades']}")
