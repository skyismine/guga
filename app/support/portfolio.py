"""模块2 个性化持仓诊断:每只持仓输出一对一操作建议。

复用现有预测/建议引擎(analysis + advisor),叠加持仓盈亏、板块归属与
三类场景(深度套牢做差价 / 盈利止盈移动止损 / 观望触发条件)方案。
"""
from app.data.fetcher import get_daily_history, get_spot_quotes, get_stock_name
from app.features.indicators import compute_features
from app.features.industry_features import prepare_features
from app.features.concept_features import get_concepts, main_concept_sw
from app.features.market_features import market_snapshot
from app.advice.advisor import generate_advice
from app.ml.predictor import Predictor
from app.support import settings as _st
from app.support.risk import load_portfolio


def _one(code, predictor, quotes, market, cfg):
    code = str(code).zfill(6)
    quote = quotes.get(code)
    df = get_daily_history(code, days=600, adjust="qfq")
    if len(df) < 120:
        raise ValueError(f"{code} 历史数据不足")
    raw = compute_features(df)
    features = prepare_features(df, code)
    pred = predictor.predict_latest(features)
    advice = generate_advice(df, raw, pred, quote, market=market)
    return df, pred, advice


def diagnose(positions: list = None, total_asset: float = None) -> dict:
    """诊断全部持仓。返回 {date, positions: [...], summary}。"""
    cfg = _st.load()
    positions = positions if positions is not None else load_portfolio()
    codes = [p["code"] for p in positions]
    try:
        quotes = get_spot_quotes(codes)
    except Exception:  # noqa: BLE001
        quotes = {}
    try:
        market = market_snapshot()
    except Exception:  # noqa: BLE001
        market = None
    predictor = Predictor()

    rows = []
    total_mv = 0.0
    for p in positions:
        row = {"code": p["code"], "qty": p["qty"], "cost": p["cost"],
               "category": p.get("category", "观察"), "ok": False, "error": None}
        try:
            df, pred, advice = _one(p["code"], predictor, quotes, market, cfg)
            q = quotes.get(p["code"]) or {}
            price = float(q.get("price") or df["close"].iloc[-1])
            pnl = (price - p["cost"]) / p["cost"] if p["cost"] else None
            mv = price * p["qty"]
            total_mv += mv
            mc = main_concept_sw(p["code"])
            sector = mc or (get_concepts(p["code"]) or [None])[0]
            plan, plan_kind = _plan(p, price, pnl, pred, advice, cfg)
            row.update({
                "ok": True,
                "name": q.get("name") or get_stock_name(p["code"]),
                "price": round(price, 2),
                "pnl_pct": round(pnl, 4) if pnl is not None else None,
                "market_value": round(mv, 2),
                "prediction": {"p_up": round(pred["p_up"], 4),
                               "p_flat": round(pred["p_flat"], 4),
                               "p_down": round(pred["p_down"], 4),
                               "direction_cn": pred["direction_cn"]},
                "advice_action": advice["action"], "advice_action_cn": advice["action_cn"],
                "levels": advice["levels"],
                "sector": sector,
                "plan": plan, "plan_kind": plan_kind,
                "reasons": advice["reasons"][:4],
                "risks": advice["risks"][:3],
            })
        except Exception as e:  # noqa: BLE001
            row["error"] = f"{type(e).__name__}: {e}"
        rows.append(row)

    for r in rows:
        r["weight"] = round(r["market_value"] / total_mv, 4) if total_mv else 0.0

    from app.support.risk import validate
    from app.review.operations import account_overview
    # 账户模型: 总资产 = 初始本金 + 已实现盈亏 + 未实现浮盈(未配置本金时退回市值口径)
    ov = account_overview(positions, prices=quotes)
    eff_total = ov["total_asset"] if ov.get("initial_set") else None
    risk = validate(positions, total_asset=eff_total, prices=quotes,
                    fg=(market or {}).get("market", {}).get("market_fear_greed"))
    fg = (market or {}).get("market", {}).get("market_fear_greed")
    return {
        "date": str((market or {}).get("market_date", "")),
        "positions": rows,
        "summary": {
            "count": len(rows), "total_market_value": round(total_mv, 2),
            "total_asset": ov["total_asset"] or risk["total_asset"],
            "available_cash": ov.get("available"),
            "cumulative_pnl": ov.get("cumulative_pnl"),
            "realized_pnl": ov.get("realized"),
            "fear_greed": fg,
            "risk_rating": risk["rating"],
            "risk_tips": risk["tips"],
            "total_pct": ov.get("total_pct") or risk["total_pct"],
        },
    }


def _plan(p, price, pnl, pred, advice, cfg) -> tuple:
    """三类场景操作方案。返回 (方案文本, 方案类型)。"""
    levels = advice["levels"]
    deep = pnl is not None and pnl <= -0.20
    profit = pnl is not None and pnl >= 0
    if deep and pred["p_up"] >= 0.30:
        hi = levels.get("resistance") or price * (1 + cfg.get("band_diff_pct", 0.06))
        lo = levels.get("support") or price * (1 - cfg.get("band_diff_pct", 0.06))
        plan = (f"深度套牢({pnl:.1%}),模型上涨概率 {pred['p_up']:.0%},基本面未见明确恶化。"
                f"建议波段做差价:反弹至 **{hi:.2f}** 附近高抛 {min(0.3, cfg.get('band_diff_pct', 0.06)*5):.0%} 仓位,"
                f"回落至 **{lo:.2f}** 附近回补;仓位控制在现有持仓的 30% 以内,保留底仓。")
        return plan, "band"
    if profit:
        stop = max(p["cost"], price * (1 - cfg.get("move_stop_trail", 0.08)))
        tp = levels.get("target")
        action = advice["action_cn"]
        plan = (f"盈利持仓(+{pnl:.1%})。止盈参考 **{tp:.2f}**,移动止损上移至 **{stop:.2f}** "
                f"(现价回撤 {cfg.get('move_stop_trail', 0.08):.0%});建议操作:**{action}**。"
                + ("主升趋势,可持有让利润奔跑,跌破移动止损即执行。" if advice["direction"] == "up"
                   else "短线承压,建议逢反弹分批兑现利润。"))
        return plan, "profit"
    # 观望
    buy_t = (f"突破 **{levels['resistance']:.2f}** 且放量(量比>1.5)考虑买入/加仓"
             if advice["direction"] == "up" else "暂不满足买入条件")
    sell_t = (f"跌破 **{levels['stop_loss']:.2f}** 止损位则离场"
              if advice["direction"] == "down" else "跌破支撑观察承接")
    plan = (f"观望持仓(浮盈 {pnl:+.1%} 或中性)。触发买入:{buy_t};触发卖出:{sell_t}。"
            f"模型主导方向:{pred['direction_cn']},建议:{advice['action_cn']}。")
    return plan, "wait"


if __name__ == "__main__":
    import json
    res = diagnose()
    print(json.dumps(res, ensure_ascii=False, indent=2))
