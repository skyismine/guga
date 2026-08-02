"""操作建议引擎:融合 ML 预测概率与技术面,生成可执行的操作建议。

输出:方向、概率、置信度、买卖/持有动作(区分持仓与空仓)、
价位区间(建议买入价/目标价/止损价/支撑/压力)、风险提示与理由。
叠加市场情绪信号:ATR 波动、恐贪指标、期指基差资金、涨跌家数宽度。
"""
import math
from typing import Dict, Optional

import numpy as np
import pandas as pd

from app import config
from app.ml.dataset import LABEL_NAME_CN
from app.features.market_features import fear_greed_label, basis_label

ACTION_CN = {
    "buy": "买入",
    "add": "加仓",
    "hold": "持有",
    "reduce": "减仓",
    "sell": "卖出",
    "wait": "观望",
}


def _f(x) -> Optional[float]:
    try:
        v = float(x)
        return None if (math.isnan(v) or math.isinf(v)) else round(v, 3)
    except (TypeError, ValueError):
        return None


def latest_values(features: pd.DataFrame) -> dict:
    row = features.iloc[-1]
    return {c: _f(row[c]) for c in features.columns}


def _recent_low_high(df: pd.DataFrame, n: int = 20) -> tuple:
    tail = df.tail(n)
    return float(tail["low"].min()), float(tail["high"].max())


def _atr(df: pd.DataFrame) -> float:
    h, l, c = df["high"], df["low"], df["close"]
    tr = pd.concat([h - l, (h - c.shift(1)).abs(), (l - c.shift(1)).abs()], axis=1).max(axis=1)
    return float(tr.tail(14).mean())


def generate_advice(df: pd.DataFrame, features: pd.DataFrame, pred: dict,
                    quote: Optional[dict] = None, market: Optional[dict] = None) -> Dict:
    """生成综合操作建议。market: market_features.market_snapshot() 的快照。"""
    v = latest_values(features)
    p_up, p_flat, p_down = pred["p_up"], pred["p_flat"], pred["p_down"]
    price = quote.get("price") if quote else None
    if not price:
        price = float(df["close"].iloc[-1])
    prev_close = float(df["close"].iloc[-1])

    atr = _atr(df)
    support, resistance = _recent_low_high(df, 20)
    ma20, ma60 = v.get("ma20"), v.get("ma60")
    rsi = v.get("rsi14")
    macd_hist = v.get("macd_hist")
    vol_ratio = v.get("volume_ratio")
    bb_pos = v.get("bb_position")
    atr_pct = v.get("atr_pct")

    # ---- 市场情绪(恐贪/期指基差/涨跌家数)
    mkt = (market or {}).get("market") or {}
    fg = mkt.get("market_fear_greed")
    basis_avg = mkt.get("market_basis_avg")
    basis_im = mkt.get("market_basis_im")
    adv_ratio = mkt.get("market_adv_ratio")
    activity = market.get("activity") if market else None
    advance, decline, limit_up = None, None, None
    if activity:
        advance = activity.get("advance")
        decline = activity.get("decline")
        limit_up = activity.get("limit_up")
    live_basis = None
    if market and market.get("futures"):
        b = market["futures"].get("if")
        live_basis = b.get("basis") if b else None

    # ---- 技术面信号
    trend_up = (ma20 is not None and ma60 is not None and price > ma20 > ma60)
    trend_down = (ma20 is not None and ma60 is not None and price < ma20 < ma60)
    rsi_ob = rsi is not None and rsi >= 70
    rsi_os = rsi is not None and rsi <= 30
    macd_bull = macd_hist is not None and macd_hist > 0
    macd_bear = macd_hist is not None and macd_hist < 0
    vol_hot = vol_ratio is not None and vol_ratio > 1.5
    vol_cold = vol_ratio is not None and vol_ratio < 0.6
    near_resistance = resistance and price >= resistance * 0.98
    near_support = support and price <= support * 1.02

    # ---- 新增信号
    atr_high = atr_pct is not None and atr_pct >= config.ATR_HIGH_PCT
    atr_low = atr_pct is not None and atr_pct <= config.ATR_LOW_PCT
    fg_extreme_fear = fg is not None and fg <= config.FG_EXTREME_FEAR
    fg_fear = fg is not None and fg <= config.FG_FEAR
    fg_greed = fg is not None and fg >= config.FG_GREED
    fg_extreme_greed = fg is not None and fg >= config.FG_EXTREME_GREED
    breadth_up = adv_ratio is not None and adv_ratio >= 0.7
    breadth_down = adv_ratio is not None and adv_ratio <= 0.3
    basis_discount = basis_avg is not None and basis_avg <= config.BASIS_DEEP_DISCOUNT
    basis_premium = basis_avg is not None and basis_avg >= config.BASIS_PREMIUM
    basis_im_discount = basis_im is not None and basis_im <= config.BASIS_IM_DISCOUNT
    market_limit_hot = limit_up is not None and limit_up >= 80

    # ---- 综合评分
    score = (p_up - p_down)                      # 模型倾向
    if trend_up:   score += 0.10
    if trend_down: score -= 0.10
    if macd_bull:  score += 0.08
    if macd_bear:  score -= 0.08
    if rsi_ob:     score -= 0.15                 # 超买回调风险
    if rsi_os:     score += 0.10                 # 超卖反弹机会
    if vol_hot:    score += 0.05 if p_up > p_down else -0.05
    if near_resistance: score -= 0.08
    if near_support:    score += 0.08
    # 市场情绪修正
    if fg_extreme_fear:  score += 0.12           # 极度恐慌:情绪修复机会
    elif fg_fear:        score += 0.06
    if fg_extreme_greed: score -= 0.12           # 极度贪婪:情绪反转风险
    elif fg_greed:       score -= 0.06
    if breadth_up:       score += 0.08
    if breadth_down:     score -= 0.08
    if basis_discount:   score -= 0.10           # 期指深贴水:机构偏空/对冲盘重
    elif basis_premium:  score += 0.08
    if basis_im_discount: score -= 0.05          # 中证1000深贴水:小盘避险
    if market_limit_hot: score += 0.04           # 涨停潮:短线情绪强
    if atr_high:         score -= 0.05           # 高波动:信号噪声大
    score = max(-1.0, min(1.0, score))

    # ---- 动作判定
    def decide(direction):
        if direction == "up" and score >= config.BUY_P_UP - 0.4:
            return "buy"
        if direction == "down" and score <= -config.SELL_P_DOWN + 0.4:
            return "sell"
        if score >= config.BUY_P_UP:
            return "buy"
        if score <= -config.SELL_P_DOWN:
            return "sell"
        if p_flat >= 0.45 or abs(score) < 0.15:
            return "hold"
        return "wait"

    direction = pred["direction"] if pred["direction"] in ("up", "down") else "flat"
    if p_flat >= p_up and p_flat >= p_down:
        direction = "flat"
    action = decide(direction)

    # 修正:强超买时买入降级为观望;极度贪婪时买入降级;深贴水时买入降级
    if action == "buy" and (rsi_ob or fg_extreme_greed):
        action = "wait"
    # 强超卖时卖出降级为减仓;极度恐慌时卖出降级
    if action == "sell" and (rsi_os or fg_extreme_fear):
        action = "reduce"

    if action == "buy":
        entry_action, hold_action = "买入", "持有"
    elif action == "sell":
        entry_action, hold_action = "观望", "卖出"
    elif action == "reduce":
        entry_action, hold_action = "观望", "减仓"
    else:
        entry_action, hold_action = "观望", "持有"

    # ---- 价位
    stop = max(support, price - atr * 3) if atr else support
    target = price + max(atr * 2.5, (resistance - price) * 0.6) if (atr or resistance) else price * 1.05
    entry_low = price * (1 - 0.008)
    entry_high = price * (1 + 0.008)
    if action in ("sell", "reduce"):
        target = price
        stop = price * 0.97

    confidence = max(p_up, p_down, p_flat)
    strong = confidence >= config.STRONG

    reasons = []
    reasons.append(f"模型预测未来{config.PREDICT_HORIZON}个交易日: "
                   f"上涨{p_up:.0%}/震荡{p_flat:.0%}/下跌{p_down:.0%},主导方向: {LABEL_NAME_CN.get({'up':2,'flat':1,'down':0}[direction])}")
    if trend_up:
        reasons.append("均线多头排列(价>MA20>MA60),中期趋势向上")
    elif trend_down:
        reasons.append("均线空头排列(价<MA20<MA60),中期趋势向下")
    else:
        reasons.append("均线纠缠,方向不明,趋势信号中性")
    reasons.append(f"MACD柱状值{'为正' if macd_bull else ('为负' if macd_bear else '趋平')},量比{_f(vol_ratio) or 0}")
    if rsi is not None:
        reasons.append(f"RSI(14)={rsi:.1f},{'超买区间,追高风险大' if rsi_ob else ('超卖区间,存在反弹需求' if rsi_os else '中性区间')}")
    reasons.append(f"ATR(14)={_f(atr) or 0}(占价{_f(atr_pct * 100) if atr_pct is not None else None}%),"
                   f"{'波动率偏高,建议控制仓位' if atr_high else ('波动率偏低,走势平稳' if atr_low else '波动适中')}")
    if fg is not None:
        reasons.append(f"恐贪指标 {fg:.0f}({fear_greed_label(fg)}),"
                       f"{'情绪极端,警惕反转风险' if (fg_extreme_greed or fg_extreme_fear) else '情绪正常'}")
    if basis_avg is not None:
        reasons.append(f"期指基差 {basis_avg:+.2%}({basis_label(basis_avg)}),"
                       f"{'机构套保/偏空迹象' if basis_discount else ('期指资金偏多' if basis_premium else '资金面中性')}")
    if basis_im is not None and basis_im_discount:
        reasons.append(f"中证1000基差 {basis_im:+.2%},中小盘避险情绪明显")
    if advance is not None and decline is not None:
        reasons.append(f"涨跌家数: 上涨 {advance:.0f} / 下跌 {decline:.0f} 家"
                       f"(涨停 {limit_up:.0f}),{'普涨行情,赚钱效应强' if breadth_up else ('普跌行情,注意防守' if breadth_down else '分化行情')}")
    elif adv_ratio is not None:
        reasons.append(f"样本篮子上涨占比 {adv_ratio:.0%},{'偏强' if breadth_up else ('偏弱' if breadth_down else '中性')}")
    reasons.append(f"20日波动区间 [{support:.2f}, {resistance:.2f}]")

    risk = []
    if rsi_ob:
        risk.append(f"RSI={rsi:.0f} 超买,谨防短期回调")
    if near_resistance:
        risk.append(f"价格接近 20 日压力位 {resistance:.2f},突破需放量确认")
    if trend_down:
        risk.append("中期趋势向下,逆势操作风险高")
    if vol_cold:
        risk.append("成交量萎缩,市场关注度低,信号可靠性下降")
    if atr_high:
        risk.append(f"波动率偏高(ATR占价 {atr_pct:.1%}),建议降低仓位、放宽止损")
    if fg_extreme_greed:
        risk.append("市场极度贪婪,谨防情绪退潮引发回落")
    if fg_extreme_fear:
        risk.append("市场极度恐慌,情绪化抛售可能延续,勿盲目抄底")
    if basis_discount:
        risk.append(f"期指深贴水({basis_avg:+.2%}),机构对冲盘偏重,大盘承压")
    if breadth_down:
        risk.append("市场普跌,个股逆势上涨难度大")
    if not strong:
        risk.append("模型置信度偏低,建议轻仓/分批")

    # ---- ATR 波动率建议仓位(单票占总资金比例)
    if atr_pct is not None:
        if atr_pct >= 0.05:
            pos_hint = "≤30%(高波动)"
        elif atr_pct >= config.ATR_HIGH_PCT:
            pos_hint = "≤50%(波动偏高)"
        elif atr_pct >= 0.025:
            pos_hint = "≤70%(波动中等)"
        else:
            pos_hint = "≤100%(低波动)"
    else:
        pos_hint = "≤50%(默认)"
    if fg_extreme_fear or fg_extreme_greed:
        pos_hint += ",分批建/减仓"

    return {
        "price": round(price, 3),
        "prev_close": round(prev_close, 3),
        "pct_chg": round((price - prev_close) / prev_close, 4) if prev_close else 0.0,
        "direction": direction,
        "direction_cn": LABEL_NAME_CN[{"up": 2, "flat": 1, "down": 0}[direction]],
        "p_up": round(p_up, 4),
        "p_flat": round(p_flat, 4),
        "p_down": round(p_down, 4),
        "confidence": round(confidence, 4),
        "strong": strong,
        "score": round(score, 4),
        "action": action,
        "action_cn": ACTION_CN[action],
        "entry_action": entry_action,
        "hold_action": hold_action,
        "levels": {
            "entry_low": round(entry_low, 2),
            "entry_high": round(entry_high, 2),
            "target": round(target, 2),
            "stop_loss": round(stop, 2),
            "support": round(support, 2),
            "resistance": round(resistance, 2),
        },
        "technical": {
            "ma20": _f(ma20), "ma60": _f(ma60), "rsi14": _f(rsi),
            "macd_hist": _f(macd_hist), "atr14": _f(atr),
            "atr_pct": _f(atr_pct), "volume_ratio": _f(vol_ratio), "bb_position": _f(bb_pos),
        },
        "market": {
            "fear_greed": _f(fg),
            "fear_greed_label": fear_greed_label(fg) if fg is not None else None,
            "basis_avg": _f(basis_avg),
            "basis_im": _f(basis_im),
            "basis_label": basis_label(basis_avg) if basis_avg is not None else None,
            "basis_if_live": _f(live_basis),
            "adv_ratio": _f(adv_ratio),
            "advance": _f(advance), "decline": _f(decline), "limit_up": _f(limit_up),
        },
        "position_hint": pos_hint,
        "reasons": reasons,
        "risks": risk,
    }
