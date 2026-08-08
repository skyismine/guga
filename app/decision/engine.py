"""决策执行引擎:四层决策漏斗。

把散落的量化数据(市场情绪/概念资金/涨停/预测概率/技术位)收敛为
「可直接执行的明确操作方案」,解决"信息太多不知道买什么"的问题。

第一层  大盘开仓许可评级(A/B/C/D + 总仓位上限)
第二层  主线概念自动遴选(一票否决 + 准入 + 分级)
第三层  标的精准匹配(激进/稳健/工具 三档,每档首选+备选)
第四层  执行参数计算(ATR止损、分批建仓、目标价、仓位)

原则:纯增量、只读复用现有接口;所有阈值可配置;输出中性合规。
"""
import datetime as dt

from app import config
from app.support import settings as _st
from app.support import mainline as _ml
from app.support.portfolio import _one
from app.features.market_features import market_snapshot, fear_greed_label

# ---------------------------------------------------------------- 工具
def _cfg():
    return _st.load().get("decision", {})


def _today() -> str:
    return dt.date.today().strftime("%Y-%m-%d")


def _sector_stats(name: str) -> dict | None:
    """近3日累计涨幅 / 20日涨幅 / 20日波动(概念指数)。无指数数据返回 None。"""
    try:
        from app.features.concept_features import _get_concept_close
        close = _get_concept_close(name)
        if close is not None and len(close) >= 4:
            out = {"gain3": float(close.iloc[-1] / close.iloc[-4] - 1)}
            if len(close) >= 21:
                out["ret20"] = float(close.iloc[-1] / close.iloc[-21] - 1)
                out["vol20"] = float(close.pct_change().tail(20).std())
            return out
    except Exception:  # noqa: BLE001
        pass
    return None


# ---------------------------------------------------------------- 第一层 大盘开仓许可评级
def _market_score(fg, adv_ratio, zt, amount_yi, w) -> float:
    s = 0.0
    if fg is not None:
        s += max(0.0, min(float(fg), 100.0)) / 100.0 * w.get("mood", 40)
    if adv_ratio is not None:
        s += min(max(float(adv_ratio), 0.0), 2.0) / 2.0 * w.get("breadth", 25)
    if zt is not None:
        s += min(max(int(zt), 0), 80) / 80.0 * w.get("zt", 20)
    if amount_yi is not None:
        base = _cfg().get("min_amount_yi", 10000.0)
        s += min(max(float(amount_yi), 0.0), base) / base * w.get("amount", 15)
    else:
        s += w.get("amount", 15) * 0.6  # 成交额缺失按中性 60% 计
    return round(min(s, 100.0), 1)


def _grade(score, zt, adv_ratio, rules) -> str:
    if zt is None:
        zt = 0
    if score >= rules["score_full"] and zt >= rules["zt_full"] and adv_ratio >= rules["adv_ratio_full"]:
        return "A"
    if score >= rules["score_ok"] and zt >= rules["zt_ok"] and adv_ratio >= rules["adv_ratio_ok"]:
        return "B"
    if score >= rules["score_hold"] or zt >= rules["zt_hold"]:
        return "C"
    return "D"


def market_permit() -> dict:
    """第一层:市场可操作度评级 + 总仓位上限。"""
    dcfg = _cfg()
    rules = dcfg["market"]
    snap = market_snapshot()
    mkt = snap.get("market") or {}
    act = snap.get("activity") or {}
    fg = mkt.get("market_fear_greed")
    adv, dec, zt = act.get("advance"), act.get("decline"), act.get("limit_up")
    adv_ratio = None
    if adv is not None and dec:
        adv_ratio = adv / dec if dec else None
    amount_yi = None
    try:
        from app.review.data import collect_market_daily
        rows = collect_market_daily(10)
        if rows:
            amount_yi = rows[-1].get("amount_yi")
    except Exception:  # noqa: BLE001
        pass

    score = _market_score(fg, adv_ratio, zt, amount_yi, rules.get("weights", {}))
    grade = _grade(score, zt, adv_ratio, rules)
    cap = rules["cap"].get(grade, 0.1)
    checks = {
        "大盘打分": {"value": score, "ok": score >= rules["score_full"], "ok_min": score >= rules["score_ok"]},
        "涨停家数": {"value": zt, "ok": zt is not None and zt >= rules["zt_full"],
                     "ok_min": zt is not None and zt >= rules["zt_ok"]},
        "涨跌家数比": {"value": round(adv_ratio, 2) if adv_ratio else None,
                       "ok": adv_ratio is not None and adv_ratio >= rules["adv_ratio_full"],
                       "ok_min": adv_ratio is not None and adv_ratio >= rules["adv_ratio_ok"]},
    }
    reasons = []
    reasons.append(f"恐贪指数 {fg:.0f} 分({fear_greed_label(fg)}),贡献评分 {fg / 100 * rules['weights']['mood']:.0f}/{rules['weights']['mood']}")
    reasons.append(f"涨停 {zt} 家 / 上涨 {adv} 家 vs 下跌 {dec} 家,家数比 {adv_ratio:.2f}")
    if amount_yi:
        reasons.append(f"两市成交额 {amount_yi:,.0f} 亿")
    reasons.append(f"大盘综合评分 {score},达 {grade} 级标准(总仓位上限 {cap:.0%})")
    return {
        "grade": grade,
        "grade_label": {"A": "A级·积极配置", "B": "B级·谨慎配置",
                        "C": "C级·持有兑现", "D": "D级·观望为主"}[grade],
        "cap": cap,
        "score": score, "fear_greed": fg, "fear_greed_label": fear_greed_label(fg),
        "limit_up": zt, "advance": adv, "decline": dec, "adv_ratio": adv_ratio,
        "amount_yi": amount_yi, "checks": checks, "reasons": reasons,
        "date": str(snap.get("market_date") or _today()),
    }


# ---------------------------------------------------------------- 第二层 主线概念自动遴选
def _veto(name, r, dcfg, stats, zt_available=True) -> tuple:
    """返回 (是否淘汰, 理由列表)。zt_available=False 表示涨停池数据缺失,跳过该否决项。"""
    veto = dcfg.get("veto", {})
    reasons = []
    if r["net_yi"] < 0:
        reasons.append(f"当日主力净流出 {r['net_yi']:.1f} 亿(一票否决)")
    if stats and stats.get("gain3") is not None and stats["gain3"] >= veto.get("max_gain_3d", 0.15):
        reasons.append(f"近3日累计涨幅 {stats['gain3'] * 100:+.1f}%,已超 {veto.get('max_gain_3d', 0.15) * 100:.0f}% 过热上限(一票否决)")
    if zt_available and r["zt_count"] < veto.get("min_zt_in_sector", 2):
        reasons.append(f"板块内涨停 {r['zt_count']} 家,不足 {veto.get('min_zt_in_sector', 2)} 家(一票否决)")
    for kw in veto.get("bad_news_kw", []):
        if kw in (name + (r.get("leader") or "")):
            reasons.append(f"名称/领涨股命中利空关键词「{kw}」(一票否决)")
    return bool(reasons), reasons


def _pass_reasons(r, stats) -> list:
    out = []
    if r["net_yi"] >= 0:
        out.append(f"主力净流入 {r['net_yi']:.1f} 亿")
    if r["pct_chg"] >= 0:
        out.append(f"板块涨 {r['pct_chg']:+.2f}%")
    if r["zt_count"]:
        out.append(f"涨停 {r['zt_count']} 家")
    if r.get("news_hits"):
        out.append(f"消息催化命中 {r['news_hits']} 次")
    if stats:
        if stats.get("ret20") is not None:
            out.append(f"近20日涨幅 {stats['ret20'] * 100:+.1f}%")
        if stats.get("vol20"):
            out.append(f"20日波动率 {stats['vol20'] * 100:.1f}%")
    out.append(f"综合评分 {r['score']} 分,达标")
    return out


def mainline_select() -> dict:
    """第二层:一票否决 + 准入 + 分级(核心主攻/防御备选/观察池)。"""
    dcfg = _cfg()
    mline = dcfg.get("mainline", {})
    rows = _ml.sector_scores(use_cache=True)
    zt_pool = _ml._zt_pool()
    zt_available = len(zt_pool) > 0  # 涨停池为空视为数据缺失,跳过涨停家数否决项
    passed, rejected, low = [], [], []
    for r in rows:
        stats = _sector_stats(r["industry"])
        banned, why = _veto(r["industry"], r, dcfg, stats, zt_available=zt_available)
        if banned:
            rejected.append({"name": r["industry"], "score": r["score"],
                             "pct_chg": r["pct_chg"], "net_yi": r["net_yi"],
                             "level": "rejected", "reasons": why})
            continue
        item = {"name": r["industry"], "score": r["score"], "pct_chg": r["pct_chg"],
                "net_yi": r["net_yi"], "zt_count": r["zt_count"], "leader": r.get("leader", ""),
                "news_hits": r.get("news_hits", 0), "stats": stats or {}}
        if r["score"] >= mline.get("pass_score", 60.0):
            item["reasons"] = _pass_reasons(r, stats)
            passed.append(item)
        else:
            item["level"] = "watch"
            item["reasons"] = [f"综合评分 {r['score']} 分,低于准入线 {mline.get('pass_score', 60.0)} 分,仅跟踪"]
            low.append(item)

    passed.sort(key=lambda x: -x["score"])
    core = passed[0] if passed else None
    if core:
        core["level"] = "core"

    defensive = None
    if core and len(passed) > 1:
        def _defense_key(it):
            low_pos = 0.0 if not it["stats"].get("ret20") else -it["stats"]["ret20"]
            low_vol = 0.0 if not it["stats"].get("vol20") else it["stats"]["vol20"]
            return it["score"] * 0.5 + low_pos * 40 - low_vol * 100
        candidates = [p for p in passed if p["name"] != core["name"]]
        if candidates:
            defensive = max(candidates, key=_defense_key)
    if defensive:
        defensive["level"] = "defensive"
        defensive["reasons"].append("低位低波动防御属性(备选方向)")

    core_name = core["name"] if core else None
    def_name = defensive["name"] if defensive else None
    watch = [p for p in passed if p["name"] != core_name and p["name"] != def_name][: mline.get("watch_n", 3)]
    for w in watch:
        w["level"] = "watch"
        w.setdefault("reasons", _pass_reasons(w, w.get("stats") or {}))
    watch += low[: max(0, mline.get("watch_n", 3) - len(watch))]

    return {"core": core, "defensive": defensive, "watch": watch,
            "rejected": rejected[: mline.get("watch_n", 3)],
            "pass_score": mline.get("pass_score", 60.0)}


# ---------------------------------------------------------------- 第三层 标的精准匹配
_TRIGGER_TPL = {
    "aggressive": "回踩支撑位 {support} 企稳(缩量)关注,或放量突破压力位 {resistance} 启动信号",
    "steady": "回踩 {entry_low}~{support} 区间分批低吸关注",
    "etf": "板块异动期折价/平价时关注,回调至 {support} 附近分批观察",
}


def _predict_one(code, predictor, quotes):
    try:
        _, pred, adv = _one(code, predictor, quotes, None, _st.load())
        lv = adv.get("levels") or {}
        return {
            "p_up": round(pred["p_up"], 4), "p_flat": round(pred["p_flat"], 4),
            "p_down": round(pred["p_down"], 4), "direction": pred["direction_cn"],
            "action": adv["action_cn"], "action_key": adv["action"],
            "levels": lv,
            "atr14": (adv.get("technical") or {}).get("atr14"),
            "reasons": adv.get("reasons", [])[:3],
        }
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}"}


def match_level_targets(sector_name: str) -> dict:
    """第三层:对某主线输出 激进/稳健/工具 三档,每档首选+备选。"""
    spot = _ml._a_spot_map()
    stocks = _ml._match_stocks(sector_name, spot)
    result = {
        "sector": sector_name,
        "aggressive": {"label": "激进型·情绪龙头", "mood": ["A"], "items": []},
        "steady": {"label": "稳健型·中军龙头", "mood": ["A", "B"], "items": []},
        "etf": {"label": "工具型·对应ETF", "mood": ["A", "B", "C"], "items": []},
    }

    def _base(c):
        return {"code": c["code"], "name": c["name"], "price": round(c["price"], 2),
                "pct_chg": c["pct_chg"], "amount_yi": round(c["amount"] / 1e8, 2)
                if c.get("amount") else None,
                "float_mv": c.get("float_mv")}

    emo = sorted(stocks, key=lambda s: -s["pct_chg"])[:2]
    mid = sorted(stocks, key=lambda s: -(s.get("amount") or 0))[:2]
    quotes = _ml.get_spot_quotes([s["code"] for s in emo + mid]) if (emo or mid) else {}
    predictor = _ml.Predictor()

    for role, cands in (("aggressive", emo), ("steady", mid)):
        for rank, c in enumerate(cands[:2], 1):
            item = _base(c)
            item["rank"] = rank
            item["role"] = {"aggressive": "情绪龙头", "steady": "中军龙头"}[role]
            item.update(_predict_one(c["code"], predictor, quotes))
            lv = item.get("levels") or {}
            item["trigger"] = _TRIGGER_TPL[role].format(
                support=lv.get("support", "-"), resistance=lv.get("resistance", "-"),
                entry_low=lv.get("entry_low", "-"))
            result[role]["items"].append(item)

    etfs = _ml._etf_map()
    matched = []
    kws = _ml._ETF_ALIAS.get(_ml._concept_kw(sector_name)) or [_ml._concept_kw(sector_name)]
    min_wan = _st.load().get("etf_min_amount", 5000.0)
    for en, e in etfs.items():
        if any(kw and kw.lower() in en.lower() for kw in kws):
            if e["amount_wan"] >= min_wan:
                matched.append({**e, "name": en})
    matched.sort(key=lambda x: -x["amount_wan"])
    for rank, e in enumerate(matched[:2], 1):
        item = {"rank": rank, "role": "ETF", "code": e["code"], "name": e["name"],
                "price": round(e["price"], 3), "amount_wan": round(e["amount_wan"], 0),
                "trigger": "板块强势期间低吸对应 ETF,注意流动性"}
        result["etf"]["items"].append(item)

    for role in ("aggressive", "steady", "etf"):
        if not result[role]["items"]:
            result[role]["items"] = [{"rank": 1, "error": "暂无可匹配标的(数据源受限)"}]
    return result


# ---------------------------------------------------------------- 第四层 执行参数计算
def execution_plan(target: dict, total_asset: float, taste: str,
                   market_cap: float = None, single_cap: float = None) -> dict:
    """第四层:单标的精确执行参数(ATR止损 / 分批建仓 / 目标价 / 仓位)。

    market_cap:市场评级总仓位上限;single_cap:单只标的上限。
    建议仓位取「风险倒推仓位」与风控上限的较小者。
    """
    dcfg = _cfg()
    risk_rate = dcfg.get("risk", {}).get(taste, 0.015)
    batch = dcfg.get("batch", {"first": 0.60, "second": 0.40})
    price = float(target.get("price") or 0)
    if price <= 0:
        return {"ok": False, "reason": "无有效现价"}
    lv = target.get("levels") or {}
    atr = target.get("atr14")
    stop = float(lv.get("stop_loss") or 0) or (price - (1.5 * atr if atr else price * 0.04))
    support = float(lv.get("support") or 0) or price * 0.95
    resistance = float(lv.get("resistance") or 0) or price * 1.08
    target1 = float(lv.get("target") or 0) or resistance
    target2 = round(target1 * 1.10, 2)

    risk_money = total_asset * risk_rate
    loss_per_share = max(price - stop, price * 0.01)
    pos_value = risk_money / (loss_per_share / price)
    single_cap = single_cap if single_cap is not None else _st.load().get("risk", {}).get("single_pct", 0.10)
    max_mv = total_asset * min(market_cap or 1.0, single_cap)
    pos_value = min(pos_value, max_mv)
    shares = int(pos_value / price // 100) * 100
    shares = max(shares, 100)
    first = int(shares * batch.get("first", 0.60) // 100) * 100
    second = shares - first

    first_price = min(price, float(lv.get("entry_high") or price))
    return {
        "ok": True,
        "taste": taste,
        "risk_rate": risk_rate,
        "risk_money": round(risk_money, 2),
        "price": round(price, 2),
        "stop": round(stop, 2),
        "support": round(support, 2),
        "resistance": round(resistance, 2),
        "target1": round(target1, 2),
        "target2": target2,
        "position_value": round(pos_value, 2),
        "shares": shares,
        "position_pct": round(pos_value / total_asset, 4) if total_asset else 0,
        "batch": {
            "first": {"ratio": batch.get("first", 0.60), "shares": first,
                      "price": round(first_price, 2),
                      "note": "首批(60%)回踩/开盘区间分批关注"},
            "second": {"ratio": batch.get("second", 0.40), "shares": second,
                       "price": round(resistance, 2),
                       "note": f"二批(40%)放量站稳 {resistance:.2f} 后加仓关注"},
        },
        "note": f"单笔风险 {risk_money:,.0f} 元(总资金 {risk_rate:.1%}),"
                f"止损 {stop:.2f}(亏 {loss_per_share:.2f}/股),最大仓位 {shares} 股 / {pos_value:,.0f} 元",
    }


# ---------------------------------------------------------------- 聚合
def decision_brief(total_asset: float = None, taste: str = None) -> dict:
    """四层聚合,输出完整决策包。"""
    total_asset = total_asset or float(getattr(config, "DECISION_TOTAL_ASSET", 1000000) or 1000000)
    taste = taste or getattr(config, "DECISION_RISK_TASTE", "balanced")

    p1 = market_permit()
    p2 = mainline_select()
    core = p2.get("core")
    defensive = p2.get("defensive")

    layers = {"layer1": p1, "layer2": p2}
    targets = {}
    plans = {}
    if core:
        t = match_level_targets(core["name"])
        targets[core["name"]] = t
        for role, seg in (("aggressive", t["aggressive"]), ("steady", t["steady"]), ("etf", t["etf"])):
            item = seg["items"][0]
            if item.get("error"):
                plans.setdefault(core["name"], {})[role] = {"ok": False, "reason": item["error"]}
            else:
                plans.setdefault(core["name"], {})[role] = execution_plan(
                    item, total_asset, taste, market_cap=p1["cap"],
                    single_cap=_st.load().get("risk", {}).get("single_pct", 0.10))
    if defensive:
        t = match_level_targets(defensive["name"])
        targets[defensive["name"]] = t

    # 极简结论
    core_stock = ""
    if core and targets.get(core["name"], {}).get("steady", {}).get("items"):
        s = targets[core["name"]]["steady"]["items"][0]
        if not s.get("error"):
            core_stock = f"{s['name']}({s['code']})"
    else:
        for role in ("aggressive",):
            if core and targets.get(core["name"], {}).get(role, {}).get("items"):
                s = targets[core["name"]][role]["items"][0]
                if not s.get("error"):
                    core_stock = f"{s['name']}({s['code']})"
    line = f"{p1['grade_label']},总仓位上限 {p1['cap']:.0%}"
    if core:
        line += f",首选方向「{core['name']}」"
    if core_stock:
        line += f",首选标的 {core_stock}"
    line += f",单笔风险按「{taste}」偏好控制在总资金 1%-2%"
    if p1["grade"] in ("C", "D"):
        line += ",市场偏弱,以观望/持有兑现为主,不建议新开仓"
    return {
        "date": p1["date"],
        "taste": taste,
        "total_asset": total_asset,
        "conclusion": {
            "line": line,
            "grade": p1["grade"], "grade_label": p1["grade_label"], "cap": p1["cap"],
            "core_sector": core and core["name"], "core_stock": core_stock,
            "risk_tip": _risk_tip(p1["grade"]),
        },
        "layers": {"layer1": p1, "layer2": p2},
        "targets": targets,
        "plans": plans,
    }


def _risk_tip(grade: str) -> str:
    return {
        "A": "市场活跃但涨停过热时防冲高回落,严格按止损位执行",
        "B": "市场中性,仓位留有余地,优选主线方向并控制单笔风险",
        "C": "市场转弱,持有兑现为主,新开仓仅限观察级小仓试探",
        "D": "市场冰点,观望为主,严禁追涨与重仓博反弹",
    }.get(grade, "")


if __name__ == "__main__":
    import json
    b = decision_brief()
    print(json.dumps(b, ensure_ascii=False, indent=2)[:2000])
