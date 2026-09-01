# -*- coding: utf-8 -*-
"""共享个股数据工具(解耦 tier_select ↔ target_match 的循环依赖)。

统一封装基于本地日线缓存的基础统计: 上市天数 / 20日均成交额 / 历史DataFrame。
所有函数只读本地缓存, 不触发网络抓取(由 fetcher 写缓存)。
"""
import os


def cached_hist(code: str):
    """读取本地日线缓存(不触发网络抓取)。"""
    try:
        from app.data import fetcher as _f
        return _f._load_cache(str(code).zfill(6))
    except Exception:  # noqa: BLE001
        return None


def list_days(code: str):
    """上市交易天数(用历史行数近似)。数据不可用返回 None(不参与过滤)。"""
    try:
        df = cached_hist(code)
        if df is None or df.empty:
            return None
        return int(len(df))
    except Exception:  # noqa: BLE001
        return None


def avg_amount20(code: str):
    """20日日均成交额(元), 基于本地日线缓存。不可用返回 None。"""
    try:
        df = cached_hist(code)
        if df is None or df.empty or "amount" not in df.columns:
            return None
        amt = df["amount"].tail(20).astype(float)
        if amt.empty:
            return None
        return float(amt.mean())
    except Exception:  # noqa: BLE001
        return None
