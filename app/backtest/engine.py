# -*- coding: utf-8 -*-
"""事件驱动日线回测引擎(6.4)。

- 输入: 日线 bars(index=date, 含 close/open/high/low) + signal 序列(1=持仓, 0=空仓);
- 模拟真实交易: 滑点 / 手续费 / 印花税 / 涨跌停限制(涨停不可买入、跌停不可卖出);
- 支持分批建仓(batch 比例 + 分批触发浮盈阈值)与移动止损(trailing, 浮盈后回撤即止);
- 输出: 权益曲线 / 成交明细 / 指标(见 metrics)。

不依赖决策引擎, 信号来源可插拔(如均线交叉 / 模型概率 / 任意规则), 供优化效果量化验证。
"""
import datetime as dt

import pandas as pd

_DEF_PARAMS = {
    "capital": 1_000_000,     # 初始资金(元)
    "commission": 0.00025,    # 佣金(双边)
    "stamp_tax": 0.0005,      # 印花税(仅卖出)
    "slippage": 0.0005,       # 滑点
    "limit": 0.10,            # 涨跌停幅度(10%/20cm 由 limit_map 覆盖)
    "position_pct": 0.5,      # 每次开仓占总资金比例
    "stop_pct": 0.05,         # 初始止损(买入价回撤)
    "trail": True,            # 移动止损(随最高价上移)
    "batch": [1.0],           # 分批比例(合计=1), 如 [0.5, 0.5]
    "batch_trigger_pct": 0.03,  # 分批触发: 浮盈达到此比例加仓下一批
    "limit_map": None,        # {code前缀: 涨跌幅}, 如 {"30": 0.20, "68": 0.20}
}


def run(bars: pd.DataFrame, signal: pd.Series, params: dict = None, code: str = "000000") -> dict:
    """运行回测。bars index=date(升序, 含 open/high/low/close); signal 与 bars 对齐。"""
    p = {**_DEF_PARAMS, **(params or {})}
    df = bars[["open", "high", "low", "close"]].copy() if "open" in bars.columns \
        else bars[["high", "low", "close"]].copy()
    df["signal"] = signal.reindex(df.index).fillna(0).astype(int)
    lim = p["limit"]
    if p.get("limit_map") and code:
        for pre, v in p["limit_map"].items():
            if code.startswith(pre):
                lim = v
                break

    cash = float(p["capital"])
    shares = 0.0
    cost_basis = 0.0           # 加权持仓成本
    highest = 0.0              # 持仓期间最高价(移动止损)
    stop = 0.0                 # 当前止损价
    batch_idx = 0              # 已用批次索引
    equity_curve = []
    trades = []
    open_trade = {"entry_date": None, "entry": 0.0, "shares": 0.0, "cost": 0.0}

    def _buy(price, budget):
        nonlocal cash, shares, cost_basis, batch_idx, highest, stop
        px = price * (1 + p["slippage"])
        fee = px * p["commission"]
        mv = min(budget, cash - fee)
        sh = int(mv / px // 100) * 100
        if sh <= 0:
            return
        total = sh * px + fee
        if total > cash:
            sh = int((cash - fee) / px // 100) * 100
            total = sh * px + fee
        if sh <= 0:
            return
        if shares == 0:
            cost_basis = px
            highest = px
            stop = px * (1 - p["stop_pct"])
            open_trade.update(entry_date=df.index[len(equity_curve) - 1] if equity_curve else df.index[0],
                              entry=px)
        else:
            cost_basis = (cost_basis * shares + px * sh) / (shares + sh)
        shares += sh
        cash -= total
        batch_idx += 1

    for i in range(len(df)):
        r = df.iloc[i]
        date = df.index[i]
        if i > 0:
            prev_close = float(df["close"].iloc[i - 1])
        else:
            prev_close = float(r["close"])
        open_px = float(r["open"] if "open" in df.columns else r["close"])
        # 分批加仓: 浮盈达到阈值买入下一批
        if shares > 0 and batch_idx < len(p["batch"]):
            if float(r["close"]) >= cost_basis * (1 + p["batch_trigger_pct"]):
                budget = float(p["capital"]) * p["position_pct"] * p["batch"][batch_idx]
                _buy(float(r["close"]), budget)
        # 开仓: 空仓 + 信号为多 + 昨收非涨停(涨停不可买入)
        if shares == 0 and int(r["signal"]) == 1 and prev_close * (1 + lim) > open_px * 1.001:
            budget = float(p["capital"]) * p["position_pct"] * p["batch"][0]
            _buy(open_px, budget)
        # 移动止损
        if shares > 0:
            highest = max(highest, float(r["high"]))
            if p["trail"] and highest > cost_basis * (1 + p["stop_pct"]):
                stop = max(stop, highest * (1 - p["stop_pct"]))
        # 止损触发: 最低价 <= 止损(跌停不可卖出则次日)
        if shares > 0 and float(r["low"]) <= stop:
            px = min(stop, open_px) * (1 - p["slippage"])
            fee = px * shares * (p["commission"] + p["stamp_tax"])
            cash += shares * px - fee
            trades.append({"entry": open_trade["entry_date"], "exit": date,
                           "entry_px": round(open_trade["entry"], 3), "exit_px": round(px, 3),
                           "shares": shares, "pnl": round(shares * (px - cost_basis) - fee, 2),
                           "reason": "止损"})
            shares = 0.0
            batch_idx = 0
            cost_basis = 0.0
            highest = 0.0
            stop = 0.0
        # 平仓: 信号转空
        elif shares > 0 and int(r["signal"]) == 0:
            px = float(r["close"]) * (1 - p["slippage"])
            fee = px * shares * (p["commission"] + p["stamp_tax"])
            cash += shares * px - fee
            trades.append({"entry": open_trade["entry_date"], "exit": date,
                           "entry_px": round(open_trade["entry"], 3), "exit_px": round(px, 3),
                           "shares": shares, "pnl": round(shares * (px - cost_basis) - fee, 2),
                           "reason": "信号"})
            shares = 0.0
            batch_idx = 0
            cost_basis = 0.0
            highest = 0.0
            stop = 0.0
        equity_curve.append({"date": str(date), "equity": round(cash + shares * float(r["close"]), 2)})

    from app.backtest import metrics
    eq = pd.Series([e["equity"] for e in equity_curve],
                   index=pd.to_datetime([e["date"] for e in equity_curve]))
    return {"equity": equity_curve, "trades": trades, "metrics": metrics.compute(eq, trades),
            "params": p, "code": code}


def simple_ma_signal(bars: pd.DataFrame, fast: int = 5, slow: int = 20) -> pd.Series:
    """简单均线交叉信号(示例策略): MA_fast > MA_slow → 1, 否则 0。"""
    c = bars["close"].astype(float)
    ma_f = c.rolling(fast).mean()
    ma_s = c.rolling(slow).mean()
    sig = (ma_f > ma_s).astype(int)
    sig[sig.isna()] = 0
    return sig
