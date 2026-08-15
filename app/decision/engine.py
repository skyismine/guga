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


_sector_stats_cache = {}  # name -> (date, result)


def _sector_stats(name: str) -> dict | None:
    """近3日累计涨幅 / 20日涨幅 / 20日波动(概念指数)。无指数数据返回 None。
    进程内按日缓存,避免同一天对同一板块重复抓取/重复加载概念指数。"""
    today = _today()
    hit = _sector_stats_cache.get(name)
    if hit and hit[0] == today:
        return hit[1]
    result = _sector_stats_uncached(name)
    _sector_stats_cache[name] = (today, result)
    return result


def _sector_stats_uncached(name: str) -> dict | None:
    try:
        from app.features.concept_features import _get_concept_close
        close = _get_concept_close(name)
        if close is not None and len(close) >= 4:
            out = {"gain3": float(close.iloc[-1] / close.iloc[-4] - 1)}
            out = {"gain3": float(close.iloc[-1] / close.iloc[-4] - 1)}
            if len(close) >= 21:
                out["ret20"] = float(close.iloc[-1] / close.iloc[-21] - 1)
                out["vol20"] = float(close.pct_change().tail(20).std())
                # 近20日压力/支撑(短期性价比维度)
                win = close.tail(20)
                out["price"] = float(close.iloc[-1])
                out["res20"] = float(win.max())
                out["sup20"] = float(win.min())
                out["dd20"] = float(close.iloc[-1] / win.max() - 1)  # 相对20日高点回撤(负值)
            return out
    except Exception:  # noqa: BLE001
        pass
    return None


def _sector_stats_many(names: list, max_workers: int = 8) -> dict:
    """并发预取多个板块统计(概念指数本地缓存命中时极快,冷缓存网络抓取时并发加速)。"""
    from concurrent.futures import ThreadPoolExecutor
    result = {}
    if not names:
        return result
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        for name, stats in zip(names, ex.map(_sector_stats, names)):
            result[name] = stats
    return result


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


def _sector_pool(name: str) -> str:
    """板块属性池判定:进攻(aggressive)/ 防御(defensive),关键词命中优先,默认按配置。"""
    pool = _cfg().get("pool", {})
    for kw in pool.get("aggressive_kw", []):
        if kw and kw.lower() in name.lower():
            return "aggressive"
    for kw in pool.get("defensive_kw", []):
        if kw and kw.lower() in name.lower():
            return "defensive"
    return pool.get("default", "aggressive")


def _style_order(items: list, style: dict, thresh: float) -> list:
    """同池板块按风格偏转微调排序(第五轮扩展因子)。

    仅当:风格明确(style_bias≠0)、同池相邻板块分数差<=thresh 时,
    将与本轮风格(大盘/小盘)对齐的板块提前;分数差距大时保持原始 score 排序。
    不修改任何板块 score,仅调整池内先后顺序(影响 leader 选任)。
    """
    if not items or not style or thresh <= 0:
        return items
    bias = style.get("bias")
    if bias not in (-1, 1):
        return items
    out = list(items)
    changed = True
    while changed:
        changed = False
        for i in range(len(out) - 1):
            a, b = out[i], out[i + 1]
            if abs((a["score"] or 0) - (b["score"] or 0)) > thresh:
                continue
            sa = a.get("size_bias", 0)
            sb = b.get("size_bias", 0)
            va = 1 if sa == bias else 0
            vb = 1 if sb == bias else 0
            if vb > va:
                out[i], out[i + 1] = out[i + 1], out[i]
                changed = True
    return out


def _pos_rating(stats: dict, vcfg: dict) -> str:
    """位置评级:低位启动 / 中位运行 / 短期高位(基于近3日涨幅与20日回撤)。"""
    gain3 = stats.get("gain3")
    dd20 = stats.get("dd20")
    if gain3 is None or dd20 is None:
        return "位置未知"
    p = vcfg.get("pos", {})
    if gain3 < p.get("low_gain3", 0.05) and dd20 < -p.get("low_dd20", 0.10):
        return "低位启动"
    if gain3 >= p.get("mid_gain3", 0.10) or dd20 > -p.get("mid_dd20", 0.05):
        return "短期高位"
    return "中位运行"


def _profit_ratio(stats: dict, price: float) -> float | None:
    """短期盈亏比 = (第一压力位 - 现价) / (现价 - 第一支撑位)。"""
    res = stats.get("res20")
    sup = stats.get("sup20")
    if res is None or sup is None or price is None or price <= sup:
        return None
    denom = price - sup
    if denom <= 0:
        return None
    return (res - price) / denom


def _rr_label(rr) -> str:
    if rr is None:
        return "无数据"
    if rr > 1.5:
        return "高性价比"
    if rr >= 1.0:
        return "中等性价比"
    return "追高风险"


def _priority(level: str, rr) -> str:
    """操作优先级:高=核心+盈亏比>1.5;中=核心+盈亏比1~1.5 或 防御+盈亏比>1.5;低=盈亏比<1 或观察池。"""
    high = 1.5
    if rr is None:
        return "低" if level == "watch" else "中"
    if level == "core" and rr > 1.5:
        return "高"
    if (level == "core" and rr >= 1.0) or (level in ("defensive",) and rr > 1.5):
        return "中"
    return "低"


def _value_notes(it: dict, vcfg: dict) -> str:
    """入选理由定性结论:核心结论 + 数据支撑。"""
    name = it.get("name", "")
    lv = it.get("level", "")
    st = it.get("stats") or {}
    rr = it.get("profit_ratio")
    pos = it.get("pos_rating")
    if not vcfg.get("note", True):
        return ""
    # 资金面:当日净流入率 + 排名
    rate = it.get("rate_1d")
    rank = it.get("fund_rank_1d")
    fund_txt = f"净流入率 {rate * 100:.1f}% 全市场第 {rank} 名" if rate is not None and rank else "资金持续净流入"
    zt = f"{it.get('zt_count') or 0} 家涨停形成板块效应" if (it.get("zt_count") or 0) > 0 else "板块个股活跃"
    if pos == "低位启动":
        lead = "资金技术双共振,低位启动持续性强"
    elif pos == "短期高位":
        lead = "短线已进入高位,追高风险大,仅作观察"
    elif pos == "中位运行":
        lead = "资金技术共振,中位蓄势待突破"
    else:
        lead = "资金面与板块效应共振"
    return f"{lead}——{fund_txt},{zt}"


def mainline_select() -> dict:
    """第二层:一票否决 + 准入 + 属性池分级(核心主攻/防御备选/观察池)。

    先划分板块属性池(进攻/防御),再池内排名,禁止跨池对比:
    - 核心主攻:进攻属性池中综合得分第 1 名(满足准入);
    - 防御备选:防御属性池中综合得分第 1 名(满足准入);
    - 观察池:其余得分>=准入线的板块,按综合得分降序;
    - 强制校验:观察池任一板块得分不得高于防御备选,否则调整标签并标注。
    """
    dcfg = _cfg()
    mline = dcfg.get("mainline", {})
    rows = _ml.sector_scores(use_cache=True)
    zt_pool = _ml._zt_pool()
    zt_available = len(zt_pool) > 0  # 涨停池为空视为数据缺失,跳过涨停家数否决项
    # ---- 第五轮:扩展因子(风格偏转排序 + 标签字段) ----
    ext_cfg = _ml._extend_cfg()
    style = _ml.market_style_bias() if ext_cfg else None
    style_tag = (style or {}).get("tag", "") if ext_cfg else ""
    style_thresh = float((ext_cfg.get("style") or {}).get("sort_bias_thresh", 3.0)) if ext_cfg else 0.0
    passed, rejected, low = [], [], []
    names = [r["industry"] for r in rows if r.get("level") != "rejected"]
    stats_map = {r["industry"]: _sector_stats(r["industry"]) for r in rows} if len(rows) <= 30 else _sector_stats_many(names)
    for r in rows:
        if r.get("level") == "rejected":
            rejected.append({"name": r["industry"], "score": r.get("score", 0),
                             "pct_chg": r["pct_chg"], "net_yi": r["net_yi"],
                             "stats": stats_map.get(r["industry"]) or {},
                             "level": "rejected", "reasons": [r["reject_reason"]],
                             "breakdown": r.get("breakdown") or {}})
            continue
        stats = stats_map.get(r["industry"])
        banned, why = _veto(r["industry"], r, dcfg, stats, zt_available=zt_available)
        if banned:
            rejected.append({"name": r["industry"], "score": r["score"],
                             "pct_chg": r["pct_chg"], "net_yi": r["net_yi"],
                             "zt_count": r["zt_count"], "leader": r.get("leader", ""),
                             "stats": stats or {},
                             "level": "rejected", "reasons": why,
                             "breakdown": r.get("breakdown") or {}})
            continue
        item = {"name": r["industry"], "score": r["score"], "pct_chg": r["pct_chg"],
                "net_yi": r["net_yi"], "zt_count": r["zt_count"], "leader": r.get("leader", ""),
                "news_hits": r.get("news_hits", 0), "stats": stats or {},
                "rate_1d": r.get("rate_1d"), "fund_rank_1d": r.get("fund_rank_1d"),
                "fund_status": r.get("fund_status"), "rate_5d": r.get("rate_5d"),
                "fund_rank_5d": r.get("fund_rank_5d"),
                "breakdown": r.get("breakdown") or {}}
        if ext_cfg:
            # 第五轮:梯队/市值风格标签字段(仅复盘展示,不参与打分)
            item["ladder_score"] = r.get("ladder_score")
            item["ladder_tag"] = r.get("ladder_tag")
            item["size_bias"] = r.get("size_bias", 0)
            item["market_style_tag"] = style_tag
        if r["score"] >= mline.get("pass_score", 60.0):
            item["reasons"] = _pass_reasons(r, stats)
            item["pool"] = _sector_pool(r["industry"])
            passed.append(item)
        else:
            item["level"] = "watch"
            item["reasons"] = [f"综合评分 {r['score']} 分,低于准入线 {mline.get('pass_score', 60.0)} 分,仅跟踪"]
            low.append(item)

    # ---- 属性池内分级(禁止跨池对比);第五轮:风格偏转仅调整同池相邻且分差小的排序
    aggressive = sorted([p for p in passed if p.get("pool") == "aggressive"], key=lambda x: -x["score"])
    defensive_pool = sorted([p for p in passed if p.get("pool") == "defensive"], key=lambda x: -x["score"])
    if ext_cfg and style:
        aggressive = _style_order(aggressive, style, style_thresh)
        defensive_pool = _style_order(defensive_pool, style, style_thresh)
    core = aggressive[0] if aggressive else None
    if core:
        core["level"] = "core"
    defensive = defensive_pool[0] if defensive_pool else None
    if defensive:
        defensive["level"] = "defensive"
        defensive["reasons"].append("防御属性池第 1 名(备选方向)")

    # ---- 观察池:其余得分达标板块,降序
    core_name = core["name"] if core else None
    def_name = defensive["name"] if defensive else None
    watch_candidates = [p for p in passed if p["name"] != core_name and p["name"] != def_name]
    watch_candidates.sort(key=lambda x: -x["score"])

    # ---- 强制校验:观察池得分不得高于防御备选(防倒挂)
    mcheck = dcfg.get("mainline_check", {})
    if mcheck.get("enforce", True) and defensive and watch_candidates:
        if watch_candidates[0]["score"] > defensive["score"]:
            top_watch = watch_candidates[0]
            top_watch["level"] = "defensive"
            top_watch["reasons"] = [r for r in top_watch.get("reasons", [])
                                    if "属性" not in r and "备选" not in r]
            top_watch["reasons"].append("属性不匹配,原评分高于防御备选,归入防御备选")
            defensive = top_watch
            def_name = defensive["name"]
            watch_candidates = [p for p in passed if p["name"] != core_name and p["name"] != def_name]
            watch_candidates.sort(key=lambda x: -x["score"])

    watch = watch_candidates[: mline.get("watch_n", 3)]
    for w in watch:
        w["level"] = "watch"
        w.setdefault("reasons", _pass_reasons(w, w.get("stats") or {}))
    watch += low[: max(0, mline.get("watch_n", 3) - len(watch))]

    # ---- 性价比维度(位置评级/盈亏比/操作优先级/定性结论)
    vcfg = dcfg.get("value", {})
    if vcfg.get("enabled", True):
        shown = [x for x in ([core] if core else []) + ([defensive] if defensive else []) + watch if x]
        for p in shown:
            st = p.get("stats") or {}
            p["pos_rating"] = _pos_rating(st, vcfg)
            p["profit_ratio"] = _profit_ratio(st, st.get("price"))
            p["rr_label"] = _rr_label(p["profit_ratio"])
            p["priority"] = _priority(p.get("level", "watch"), p["profit_ratio"])
            p["reasons"] = p.get("reasons") or []
            # 变现为"定性结论 + 数据支撑"格式(取代纯复述数据):首行为结论总结
            if vcfg.get("note", True) and p.get("level") != "rejected":
                p["reasons"] = [_value_notes(p, vcfg)] + p["reasons"]

    out = {"core": core, "defensive": defensive, "watch": watch,
           "rejected": rejected[: mline.get("watch_n", 3)],
           "pass_score": mline.get("pass_score", 60.0)}
    if ext_cfg:
        # 第五轮:全局风格偏转信息(复盘展示,不参与打分)
        out["market_style"] = style or _ml.market_style_bias()
    return out


# ---------------------------------------------------------------- 第三层 标的精准匹配
# 统一 5 档操作信号口径(全系统统一命名),从 advisor 动作映射并叠加修正
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


def _shift_signal(sig: str, delta: int) -> str:
    """信号档位移(正=上修/更积极, 负=下修/更保守),越界封顶。"""
    idx = _SIGNAL_RANK.get(sig, 0) + delta
    return _SIGNAL_LEVELS[max(0, min(len(_SIGNAL_LEVELS) - 1, idx))]


def _adjust_signal(item: dict, sector_level: str) -> dict:
    """叠加板块等级修正 + 位置修正,统一为 5 档信号并输出修正说明。

    item 需含 action_key / ret3d;修改 item["signal"]/item["action"]/item["adj_notes"]。
    """
    dcfg = _cfg()
    scfg = dcfg.get("signal", {})
    if not scfg.get("enabled", True) or item.get("error"):
        return item
    base = _ACTION_TO_SIGNAL.get(item.get("action_key"), "观望")
    sig = base
    notes = []

    # 1) 板块等级修正:核心主攻上修 1 档
    boost = scfg.get("sector_boost", {}).get(sector_level, 0)
    if boost:
        sig = _shift_signal(sig, boost)
        notes.append(f"核心主线溢价上修 {boost} 档")

    # 2) 位置修正:低位启动上修 / 短期高位下修
    low_pos = scfg.get("low_pos_ret3d", 0.05)
    high_pos = scfg.get("high_pos_ret3d", 0.15)
    ret3d = item.get("ret3d")
    if ret3d is not None:
        if ret3d < low_pos and ret3d > 0:
            sig = _shift_signal(sig, 1)
            notes.append(f"低位启动(近3日 {ret3d:+.1%}),信号上修关注")
        elif ret3d >= high_pos:
            sig = _shift_signal(sig, -1)
            notes.append(f"短期高位(近3日 {ret3d:+.1%}),信号下修防回落")

    item["signal"] = sig
    item["action"] = sig
    item["adj_notes"] = notes if scfg.get("note", True) else []
    return item


_TRIGGER_TPL = {
    "aggressive": "回踩支撑位 {support} 企稳(缩量)关注,或放量突破压力位 {resistance} 启动信号",
    "steady": "回踩 {entry_low}~{support} 区间分批低吸关注",
    "etf": "板块异动期折价/平价时关注,回调至 {support} 附近分批观察",
}


def _predict_one(code, predictor, quotes, market):
    try:
        df, pred, adv = _one(code, predictor, quotes, market, _st.load())
        lv = adv.get("levels") or {}
        close = df["close"]
        ret3d = float(close.iloc[-1] / close.iloc[-4] - 1) if len(close) >= 4 else None
        ma5 = float(close.rolling(5).mean().iloc[-1]) if len(close) >= 5 else None
        ma10 = float(close.rolling(10).mean().iloc[-1]) if len(close) >= 10 else None
        return {
            "p_up": round(pred["p_up"], 4), "p_flat": round(pred["p_flat"], 4),
            "p_down": round(pred["p_down"], 4), "direction": pred["direction_cn"],
            "action": adv["action_cn"], "action_key": adv["action"],
            "levels": lv,
            "atr14": (adv.get("technical") or {}).get("atr14"),
            "ret3d": round(ret3d, 4) if ret3d is not None else None,
            "ma5": round(ma5, 2) if ma5 is not None else None,
            "ma10": round(ma10, 2) if ma10 is not None else None,
            "reasons": adv.get("reasons", [])[:3],
        }
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}"}


def match_level_targets(sector_name: str, sector_level: str = "watch") -> dict:
    """第三层:对某主线输出 激进/稳健/工具 三档,每档首选+备选。

    sector_level 决定信号修正:core 板块标的信号整体上修(叠加板块溢价)。
    """
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
    try:
        market = market_snapshot()
    except Exception:  # noqa: BLE001
        market = None

    for role, cands in (("aggressive", emo), ("steady", mid)):
        for rank, c in enumerate(cands[:2], 1):
            item = _base(c)
            item["rank"] = rank
            item["role"] = {"aggressive": "情绪龙头", "steady": "中军龙头"}[role]
            item.update(_predict_one(c["code"], predictor, quotes, market))
            if item.get("error"):
                result[role]["items"].append(item)
                continue
            _adjust_signal(item, sector_level)
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
                "price": round(e["price"], 3), "amount_wan": round(e["amount_wan"], 0)}
        item.update(_predict_one(e["code"], predictor, quotes, market))
        if item.get("error"):
            result["etf"]["items"].append(item)
            continue
        _adjust_signal(item, sector_level)
        lv = item.get("levels") or {}
        if lv:
            item["trigger"] = _TRIGGER_TPL["etf"].format(
                support=lv.get("support", "-"), resistance=lv.get("resistance", "-"))
        else:
            item["trigger"] = "板块强势期间低吸对应 ETF,注意流动性"
        result["etf"]["items"].append(item)

    for role in ("aggressive", "steady", "etf"):
        if not result[role]["items"]:
            result[role]["items"] = [{"rank": 1, "error": "暂无可匹配标的(数据源受限)"}]
    return result


# ---------------------------------------------------------------- 第四层 执行参数计算
def _trigger_status(code: str, support: float, resistance: float, mode: str,
                    tcfg: dict) -> dict:
    """量化触发条件判断:未触发 / 触发中 / 已触发(盘中按分钟K线判定)。

    - 缩量企稳(回踩模式 pullback):最新价落在支撑位±band 区间内,且最近连续
      bars 根5分钟K线成交额均低于当日分钟成交额均值*vol_ratio -> 触发中;
    - 有效突破(breakout):价格站稳压力位上方超过 above_minutes 分钟,且该档
      5分钟成交额 >= 前30分钟均量*vol_mult -> 触发中。
    数据不可用返回 unknown(不阻塞页面)。
    """
    if not tcfg.get("enabled", True):
        return {"status": "未触发", "label": "trigger-off", "note": "未启用量化触发"}
    if mode not in ("breakout", "pullback"):
        return {"status": "未触发", "label": "trigger-off", "note": "无量化模式对应"}
    if (mode == "pullback" and not support) or (mode == "breakout" and not resistance):
        return {"status": "未触发", "label": "trigger-off", "note": "无有效支撑/压力位"}
    try:
        import pandas as pd
        from app.data.fetcher import get_intraday_bars
        bars = get_intraday_bars(code, period=tcfg.get("minute_period", "5"), limit=120)
        if bars is None or len(bars) < 5:
            return {"status": "未知", "label": "trigger-unknown", "note": "盘中数据未就绪"}
        if "close" not in bars.columns:
            return {"status": "未知", "label": "trigger-unknown", "note": "分钟数据列缺失"}
        amt = pd.to_numeric(bars["amount"] if "amount" in bars.columns else bars["volume"],
                            errors="coerce").fillna(0.0)
        closes = pd.to_numeric(bars["close"], errors="coerce")
        if amt.iloc[-1] is None or pd.isna(closes.iloc[-1]):
            return {"status": "未知", "label": "trigger-unknown", "note": "分钟数据不完整"}
        day_avg = float(amt.mean()) if len(amt) else 0.0
        shrink = tcfg.get("shrink", {})
        band = float(shrink.get("band", 0.01))
        need = int(shrink.get("bars", 3))
        vol_r = float(shrink.get("vol_ratio", 0.80))
        brk = tcfg.get("breakout", {})
        above_min = int(brk.get("above_minutes", 5))
        mult = float(brk.get("vol_mult", 2.0))

        if mode == "pullback":
            in_band = (support * (1 - band) <= closes) & (closes <= support * (1 + band))
            low_vol = (amt < day_avg * vol_r) if day_avg else pd.Series([False] * len(amt), index=amt.index)
            seq = 0
            for i in range(len(amt) - 1, -1, -1):
                seq = seq + 1 if bool(in_band.iloc[i] and low_vol.iloc[i]) else 0
                if seq >= need:
                    return {"status": "触发中", "label": "trigger-on",
                            "note": f"缩量企稳:支撑±{band:.0%}内连续{need}根低量"}
            return {"status": "未触发", "label": "trigger-off",
                    "note": f"未满足缩量企稳(需支撑±{band:.0%}内连续{need}根低量)"}
        else:
            above = (closes > resistance).tolist()
            seq = 0
            for i in range(len(above) - 1, -1, -1):
                seq = seq + 1 if above[i] else 0
                if seq * 5 >= above_min:
                    base = amt.iloc[max(0, i - 6): i]  # 前30分钟约6根5分钟K线
                    base_avg = float(base.mean()) if len(base) else 0.0
                    if base_avg > 0 and float(amt.iloc[i]) >= base_avg * mult:
                        return {"status": "触发中", "label": "trigger-on",
                                "note": f"放量突破:站稳{above_min}分钟,量达前30分钟均量{mult:.1f}倍"}
            return {"status": "未触发", "label": "trigger-off",
                    "note": f"未满足有效突破(需站稳{above_min}分钟且放量{mult:.1f}倍)"}
    except Exception as e:  # noqa: BLE001
        return {"status": "未知", "label": "trigger-unknown", "note": f"触发判定异常:{e}"}


def _matrix_cap(grade: str, asset_type: str, dcfg: dict) -> float | None:
    """动态仓位矩阵:市场评级 x 标的类型 -> 单标的总仓位上限(占总资金)。未启用返回 None。"""
    pm = dcfg.get("position_matrix", {})
    if not pm.get("enabled", True):
        return None
    grade = grade or "B"
    row = pm.get("cap", {}).get(grade) or {}
    if not row:
        return None
    val = row.get(asset_type)
    if val is None:
        val = row.get("mid")
    if val is None:
        return None
    return float(val)


def execution_plan(target: dict, total_asset: float, taste: str,
                   market_cap: float = None, single_cap: float = None,
                   grade: str = None, asset_type: str = None,
                   sector_used_pct: float = 0.0, sector_cap_pct: float = None) -> dict:
    """第四层:单标的精确执行参数(ATR止损 / 分批建仓 / 目标价 / 仓位)。

    market_cap:市场评级总仓位上限;single_cap:单只标的上限(旧模式)。
    grade:市场评级(A/B/C/D);asset_type:标的类型(mood|mid|etf|def_etf),动态仓位矩阵。
    sector_used_pct:本板块已用仓位占比;sector_cap_pct:单板块总仓位上限(超出则压缩+预警)。

    建议仓位取「风险倒推仓位」与「风控上限(总仓位/单票/矩阵/板块)」的较小者。

    全链路自洽约束:
    - 第一目标价 = 现价 x (1 + 0.5 x ATR),且至少高于现价 3%;
    - 第二目标价 = 近 20 日平台压力位(VectorBT levels.resistance);
    - 分批与触发强绑定:回踩低吸(逐级降低) / 突破跟进(逐级抬高),二选一;
    - 回踩区间限定 5日线~10日线,跨度不超过 8%;
    - 股数 = 风险金额 / (现价-止损价),金额/股数/最大亏损/占比四者自洽。
    """
    dcfg = _cfg()
    risk_rate = dcfg.get("risk", {}).get(taste, 0.015)
    batch = dcfg.get("batch", {"first": 0.60, "second": 0.40})
    plancfg = dcfg.get("plan", {})
    price = float(target.get("price") or 0)
    if price <= 0:
        return {"ok": False, "reason": "无有效现价"}
    lv = target.get("levels") or {}
    atr = target.get("atr14")
    atr = float(atr) if atr else None
    ma5 = target.get("ma5")
    ma10 = target.get("ma10")

    # ---- 止损:VectorBT 止损位优先,缺失回退 ATR 或现价比例
    stop = float(lv.get("stop_loss") or 0) or (price - (1.5 * atr if atr else price * 0.04))
    # 现价跌破止损视为止损无效(止损应低于买入价),修正为 ATR/比例回退
    if stop >= price:
        stop = price - (1.5 * atr if atr else price * 0.04)

    # ---- 目标价
    resistance = float(lv.get("resistance") or 0) or price * 1.08
    target1 = price * (1 + (0.5 * (atr / price if atr else 0.04)))
    target1 = max(target1, price * (1 + plancfg.get("target1_min_gain", 0.03)))  # 至少+3%
    target1 = max(target1, price * 1.001)  # 确保高于现价
    target2 = resistance  # 近20日平台压力位
    # 目标价强制递增:第二目标不得低于第一目标
    if target2 < target1:
        target2 = target1

    # ---- 分批模式(与触发条件强绑定)
    mode = plancfg.get("mode", "auto")
    if mode == "auto":
        sig = target.get("signal")
        ret3d = target.get("ret3d") or 0
        mode = "breakout" if (sig in ("突破跟进", "关注低吸") and ret3d >= 0.05) else "pullback"
    if mode == "breakout":
        # 突破跟进:首批=压力位突破价,二批=突破后回踩确认价(略低于突破价)
        first_price = resistance
        second_price = first_price * (1 - 0.01)
        deep_support = float(lv.get("support") or 0) or price * 0.92
        mode_note = "突破跟进:首批放量突破压力位,二批回踩确认"
        trigger = f"放量突破压力位 {first_price:.2f} 确认,回踩 {second_price:.2f} 不破再关注"
        first_note = "首批:突破压力位(60%)"
        second_note = "二批:回踩确认(40%)"
        avg_cost = first_price * batch.get("first", 0.60) + second_price * batch.get("second", 0.40)
        if stop >= avg_cost:
            stop = avg_cost * 0.98  # 止损必须低于加权买入成本
    else:
        # 回踩低吸:支撑上沿(5日线附近) -> 支撑下沿(10日线附近)
        hi = (ma5 or price * 0.97)
        lo = (ma10 or price * 0.94)
        # 回踩区间跨度限制(不超过 8%),超限则压缩
        span = (hi - lo) / price
        if span > plancfg.get("pullback_span_max", 0.08):
            lo = hi - plancfg.get("pullback_span_max", 0.08) * price
        lo = min(lo, hi)
        first_price = hi          # 第一批=支撑上沿(5日线附近)
        second_price = lo         # 第二批=支撑下沿(10日线附近)
        # 回踩区间整体位于现价下方(现价过高时保留,勿越现价)
        first_price = min(first_price, price)
        second_price = min(second_price, first_price)
        # 极端加仓位(中期强支撑,不混入短线回踩区间)
        deep_support = float(lv.get("support") or 0) or price * 0.92
        # 止损 = min(原止损, 二批下方缓冲),必须低于加权买入成本
        avg_cost = first_price * batch.get("first", 0.60) + second_price * batch.get("second", 0.40)
        stop = min(stop, second_price * 0.97)
        if stop >= avg_cost:
            stop = second_price * 0.97
        mode_note = "回踩低吸:首批5日线附近(支撑上沿),二批10日线附近(支撑下沿)"
        trigger = f"回踩支撑区间 {second_price:.2f}~{first_price:.2f} 缩量企稳"
        first_note = "首批:回踩支撑上沿(60%)"
        second_note = "二批:回踩支撑下沿(40%)"
    # 逐级方向强制校验:回踩二批低于一批,突破二批低于一批但整体在压力位上方
    if first_price <= 0 or second_price <= 0:
        first_price, second_price = price, price * 0.97

    # ---- 仓位股数(风险公式,三者自洽;以加权买入成本为基准)
    avg_cost = first_price * batch.get("first", 0.60) + second_price * batch.get("second", 0.40)
    risk_money = total_asset * risk_rate
    loss_per_share = max(avg_cost - stop, price * 0.01)  # 每股最大亏损(基于加权成本)
    risk_shares = risk_money / loss_per_share          # 风险公式推股数

    # ---- 单标的上限:动态仓位矩阵优先(升级2),否则回退固定 single_pct
    pm = dcfg.get("position_matrix", {})
    use_matrix = (pm.get("enabled", True) and asset_type is not None and single_cap is None)
    matrix_cap = _matrix_cap(grade, asset_type, dcfg) if use_matrix else None
    matrix_note = ""
    if matrix_cap is not None:
        single_cap = matrix_cap            # 矩阵覆盖率旧参数
        matrix_note = f"(仓位矩阵 {grade}级上限 {matrix_cap:.0%})"
        if matrix_cap <= 0:
            return {"ok": False,
                    "name": target.get("name"), "code": target.get("code"),
                    "reason": f"{grade} 级市场下「{asset_type}」类型禁止新开仓(仓位矩阵 0%)"}
    else:
        single_cap = single_cap if single_cap is not None else _st.load().get("risk", {}).get("single_pct", 0.10)
    max_mv = total_asset * min(market_cap or 1.0, single_cap)

    # ---- 单板块总仓位上限(超出则压缩 + 预警)
    pm_block = pm if use_matrix else {}
    if pm_block and sector_cap_pct is None:
        sector_cap_pct = pm.get("sector_cap", {}).get(grade or "B", 0.20)
    block_note = ""
    if pm_block and sector_cap_pct and pm.get("enforce", True):
        remaining = max(0.0, sector_cap_pct - (sector_used_pct or 0.0))
        max_blk = total_asset * remaining
        max_mv = min(max_mv, max_blk)
        block_note = f";板块已用 {sector_used_pct or 0:.1%},上限 {sector_cap_pct:.0%},本票最多 {remaining:.1%}"

    pos_value = min(risk_shares * avg_cost, max_mv)    # 同时受全部风控上限约束
    shares = int(pos_value / avg_cost // 100) * 100
    shares = max(shares, 100)
    pos_value = shares * avg_cost

    # 强制校验:风险公式推算仓位 vs 实际仓位,偏差<=5%
    implied = (risk_shares * avg_cost) if risk_shares else pos_value
    if implied > 0:
        dev = abs(pos_value - min(implied, max_mv)) / min(implied, max_mv)
        if dev > plancfg.get("position_check_tol", 0.05):
            pos_value = min(implied, max_mv)
            shares = int(pos_value / avg_cost // 100) * 100
            shares = max(shares, 100)
            pos_value = shares * avg_cost
    first = int(shares * batch.get("first", 0.60) // 100) * 100
    second = shares - first
    max_loss = shares * loss_per_share

    # ---- 触发条件量化(升级4):盘中按分钟K线判定当前触发状态
    tcfg = dcfg.get("trigger", {})
    trigger_state = {"status": "未触发", "label": "trigger-off", "note": ""}
    if tcfg.get("enabled", True) and target.get("code"):
        st = _trigger_status(str(target.get("code")), float(lv.get("support") or 0) or None,
                             float(resistance or 0) or None, mode, tcfg)
        trigger_state = st if st else trigger_state

    return {
        "ok": True,
        "taste": taste,
        "name": target.get("name"),
        "code": target.get("code"),
        "trigger": trigger,
        "trigger_status": trigger_state,
        "mode": mode,
        "mode_note": mode_note,
        "asset_type": asset_type,
        "matrix_cap": matrix_cap,
        "risk_rate": risk_rate,
        "risk_money": round(risk_money, 2),
        "price": round(price, 2),
        "stop": round(stop, 2),
        "support": round(float(lv.get("support") or 0), 2) or round(price * 0.95, 2),
        "resistance": round(resistance, 2),
        "target1": round(target1, 2),
        "target2": round(target2, 2),
        "position_value": round(pos_value, 2),
        "shares": shares,
        "position_pct": round(pos_value / total_asset, 4) if total_asset else 0,
        "max_loss": round(max_loss, 2),
        "batch": {
            "first": {"ratio": batch.get("first", 0.60), "shares": first,
                      "price": round(first_price, 2), "note": first_note},
            "second": {"ratio": batch.get("second", 0.40), "shares": second,
                       "price": round(second_price, 2), "note": second_note},
            "deep_support": round(deep_support, 2),
        },
        "note": (f"单笔风险 {risk_money:,.0f} 元(总资金 {risk_rate:.1%}),预计最大亏损 {max_loss:,.0f} 元"
                 f"({max_loss / total_asset:.2%} 占总资金),止损 {stop:.2f}(亏 {loss_per_share:.2f}/股),"
                 f"建议仓位 {shares} 股 / {pos_value:,.0f} 元({pos_value / total_asset:.1%})。{mode_note}"
                 f"{matrix_note}{block_note}"),
    }


# ---------------------------------------------------------------- 聚合
def decision_brief(total_asset: float = None, taste: str = None) -> dict:
    """四层聚合,输出完整决策包。默认参数取自 settings.decision(可配置)。"""
    dcfg = _cfg()
    total_asset = total_asset or float(dcfg.get("total_asset", 1000000) or 1000000)
    taste = taste or dcfg.get("taste", "balanced")

    p1 = market_permit()
    # 第四轮改造:主线输出统一经外层防抖稳定器取「stable」稳定结果;
    # 原始流水线结果进 layer2_raw 仅作调试/回测,不再直接驱动今日决策。
    # 惰性导入避免 engine->mainline_stabilizer->engine 的循环依赖。
    # get_output() 优先复用后台定时轮询的最近结果,页面访问不重复抓取数据。
    from app.support import mainline_stabilizer as _stab
    mout = _stab.get_output()
    p2 = mout["stable"]
    core = p2.get("core")
    defensive = p2.get("defensive")

    layers = {"layer1": p1, "layer2": p2,
              "layer2_raw": mout.get("raw"),
              "stabilizer_stats": mout.get("stats")}
    targets = {}
    plans = {}
    if core:
        t = match_level_targets(core["name"], sector_level="core")
        targets[core["name"]] = t
        role_slots = (("steady", "mid"), ("aggressive", "mood"), ("etf", "etf"))
        blk_used = 0.0
        for role, atype in role_slots:
            seg = t[role]
            if not seg:
                continue
            item = seg["items"][0]
            if item.get("error"):
                plans.setdefault(core["name"], {})[role] = {"ok": False, "reason": item["error"]}
                continue
            p = execution_plan(
                item, total_asset, taste, market_cap=p1["cap"],
                grade=p1["grade"], asset_type=atype, sector_used_pct=blk_used)
            plans.setdefault(core["name"], {})[role] = p
            if p.get("ok"):
                blk_used = min(1.0, blk_used + (p.get("position_pct") or 0.0))
    if defensive:
        t = match_level_targets(defensive["name"], sector_level="defensive")
        targets[defensive["name"]] = t
        # 防御备选 ETF:单独一档(防御备选ETF),计入防御板块总仓位
        seg = t.get("etf")
        if seg:
            item = seg["items"][0]
            if item.get("error"):
                plans.setdefault(defensive["name"], {})["etf"] = {"ok": False, "reason": item["error"]}
            else:
                p = execution_plan(
                    item, total_asset, taste, market_cap=p1["cap"],
                    single_cap=_st.load().get("risk", {}).get("single_pct", 0.10),
                    grade=p1["grade"], asset_type="def_etf",
                    sector_used_pct=0.0)
                plans.setdefault(defensive["name"], {})["etf"] = p

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
        "layers": layers,
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
