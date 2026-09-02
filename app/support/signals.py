# -*- coding: utf-8 -*-
"""5 档操作信号口径与纯信号工具(供 decision.engine / support.target_match 等共用)。

把这些无状态常量/纯函数从 engine 抽离, 避免 target_match 跨模块调用 engine 内部函数:
- _SIGNAL_LEVELS / _SIGNAL_RANK: 统一 5 档信号档位(强度升序);
- _ACTION_TO_SIGNAL: advisor 原始动作 -> 5 档信号;
- shift_signal: 档位位移(纯函数, 越界封顶);
- _TRIGGER_TPL: 各标的类型的触发文案模板(纯常量)。
本模块不依赖 engine, 无循环。
"""
_SIGNAL_LEVELS = ["观望", "减仓兑现", "持有观察", "突破跟进", "关注低吸"]  # 强度升序
_SIGNAL_RANK = {s: i for i, s in enumerate(_SIGNAL_LEVELS)}
# advisor 原始动作(action_key) -> 5 档信号
_ACTION_TO_SIGNAL = {
    "buy": "关注低吸",
    "add": "突破跟进",
    "hold": "持有观察",
    "reduce": "减仓兑现",
    "sell": "观望",
    "wait": "观望",
}

_TRIGGER_TPL = {
    "aggressive": "回踩支撑位 {support} 企稳(缩量)关注,或放量突破压力位 {resistance} 启动信号",
    "steady": "回踩 {entry_low}~{support} 区间分批低吸关注",
    "repair": "补涨优选:回踩 {support} 企稳低吸,放量启动确认后跟进",
    "etf": "板块异动期折价/平价时关注,回调至 {support} 附近分批观察",
}


def shift_signal(sig: str, delta: int) -> str:
    """信号档位移(正=上修/更积极, 负=下修/更保守),越界封顶。"""
    idx = _SIGNAL_RANK.get(sig, 0) + delta
    return _SIGNAL_LEVELS[max(0, min(len(_SIGNAL_LEVELS) - 1, idx))]
