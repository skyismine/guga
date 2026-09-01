# -*- coding: utf-8 -*-
"""回测指标体系(6.4): 收益 / 风险 / 交易 / 分层。"""
import math

import pandas as pd

_TRADING_DAYS = 252


def compute(equity: pd.Series, trades: list, benchmark: pd.Series = None) -> dict:
    """计算回测指标。equity: 权益曲线(index=datetime, 值=资金); trades: 成交明细列表。"""
    m = {}
    if equity is None or len(equity) < 2:
        return {"error": "权益曲线过短"}
    rets = equity.pct_change().dropna()
    total = float(equity.iloc[-1] / equity.iloc[0] - 1)
    years = max(len(equity) / _TRADING_DAYS, 1e-9)
    ann = (1 + total) ** (1 / years) - 1
    m["cum_return"] = round(total, 4)
    m["annual_return"] = round(ann, 4)
    # 最大回撤
    peak = equity.cummax()
    dd = equity / peak - 1
    m["max_drawdown"] = round(float(dd.min()), 4)
    m["calmar"] = round(ann / abs(m["max_drawdown"]), 3) if m["max_drawdown"] < 0 else None
    # 夏普 / 索提诺
    std = float(rets.std()) if len(rets) > 1 else 0.0
    m["sharpe"] = round(ann / std, 3) if std > 0 else None
    downside = rets[rets < 0]
    dstd = float(downside.std()) if len(downside) > 1 else 0.0
    m["sortino"] = round(ann / dstd, 3) if dstd > 0 else None
    # 超额(相对基准)
    if benchmark is not None and len(benchmark) == len(equity):
        bret = benchmark.pct_change().dropna()
        r = rets.reindex(bret.index).dropna()
        b = bret.reindex(r.index)
        if len(r) > 1 and float(b.std()) > 0:
            m["excess_return"] = round(float(r.mean() - b.mean()) * _TRADING_DAYS, 4)
    # 交易指标
    n = len(trades)
    m["trade_count"] = n
    if n:
        wins = [t for t in trades if t.get("pnl", 0) > 0]
        losses = [t for t in trades if t.get("pnl", 0) <= 0]
        m["win_rate"] = round(len(wins) / n, 4)
        gp = sum(t["pnl"] for t in wins) or 0.0
        gl = abs(sum(t["pnl"] for t in losses)) or 1e-9
        m["profit_factor"] = round(gp / gl, 3)
        m["avg_pnl"] = round(sum(t["pnl"] for t in trades) / n, 2)
        days = []
        for t in trades:
            try:
                d = (pd.to_datetime(t["exit"]) - pd.to_datetime(t["entry"])).days
                days.append(max(d, 0))
            except Exception:  # noqa: BLE001
                continue
        m["avg_holding_days"] = round(sum(days) / len(days), 1) if days else None
        m["total_pnl"] = round(sum(t["pnl"] for t in trades), 2)
        m["stop_ratio"] = round(sum(1 for t in trades if t.get("reason") == "止损") / n, 4)
    return m


def by_signal_group(trades: list, key="reason") -> dict:
    """按原因/标的类型分层统计。"""
    from collections import defaultdict
    groups = defaultdict(list)
    for t in trades:
        groups[t.get(key, "未知")].append(t)
    out = {}
    for k, ts in groups.items():
        w = [t for t in ts if t.get("pnl", 0) > 0]
        out[k] = {"count": len(ts), "win_rate": round(len(w) / len(ts), 3),
                  "total_pnl": round(sum(t.get("pnl", 0) for t in ts), 2)}
    return out
