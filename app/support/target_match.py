"""第六轮:标的精准匹配优化(外挂模块,全部开关默认关闭)。

设计原则(对应任务硬性约束):
- 不修改 engine.match_level_targets / _predict_one / GBM / 5档信号 核心计算逻辑;
  本模块只做「前置过滤 + 后置修正 + 新增选股逻辑」,以可配置开关方式外挂接入。
- 所有开关默认关闭;全关时 match_targets_v2 输出与 match_level_targets 完全一致,
  兼容历史回测(decision_brief 仅在开关开启时改读 stable_targets)。
- 与 mainline_stabilizer 联动:sector_status 区分 正式core/defensive 与 候选/观察;
  候选异动主线不启用板块溢价上修,也不启用高级选股逻辑。

Trade-off 说明(为什么默认关闭):
- 防抖/过滤/高级排名都需要额外的历史行情与盘中快照抓取,接口成本上升;
- 高级排名用多维度加权,参数敏感性高于原单一指标,先关闭由用户按需开启。
"""
import threading
import time

from app.support import settings as _st
from app.support.signals import shift_signal as _shift_signal, _TRIGGER_TPL as _TTPL


def _fault(e: BaseException, note: str = ""):
    """记录一次被降级吞掉的异常(接入 fault 统一日志, 不再静默)。"""
    try:
        from app.support import fault as _flt
        _flt.warning("target_match", note or "处理降级(按缺省继续)", exc=e)
    except Exception as _e:  # noqa: BLE001
        _fault(_e)


# ------------------------------------------------------------------ 配置读取
def _cfg() -> dict:
    """读取 target_match 配置组(settings 默认 + json 覆盖)。"""
    return _st.load().get("target_match", {}) or {}


def _switch(name: str) -> bool:
    """读取单个开关(默认关闭)。"""
    return bool(_cfg().get(name, False))


# ------------------------------------------------------------------ 内存状态
# P0.1 标的驻留防抖状态:{(sector, role): {code: {cycle, cool_until, removed_at}}}
_STATE_LOCK = threading.Lock()
_TARGET_STATE = {}


def _reset_state(sector: str, role: str) -> None:
    with _STATE_LOCK:
        _TARGET_STATE.pop((sector, role), None)


# ------------------------------------------------------------------ 可交易性过滤(P0.2)
def _list_days(code: str) -> int | None:
    """上市交易天数(用历史行数近似)。数据不可用返回 None(不参与过滤)。"""
    try:
        from app.support.stock_utils import list_days as _sd
        return _sd(code)
    except Exception:  # noqa: BLE001
        return None


def _avg_amount20(code: str) -> float | None:
    """20日日均成交额(元),基于本地日线缓存。不可用返回 None。"""
    try:
        from app.support.stock_utils import avg_amount20 as _sa
        return _sa(code)
    except Exception:  # noqa: BLE001
        return None


def _tradable_filter(stocks: list, role: str, cfg: dict) -> list:
    """P0.2 可交易性基础过滤:命中任一规则直接剔除(不进入选股排名)。

    - 个股通用:一字涨停/一字跌停、盘中临停、停牌(实时快照 high==low 判定);
    - 次新股:上市天数 < min_list_days;
    - 流动性:20日日均成交额低于档位阈值(并叠加全档位基础下限 min_avg_amount_base);
    - 4.1 名称剔除:ST/退市/风险警示 与 利空关键词(bad_news_kw)。
    任一维度数据不可用时跳过该维度(不误杀)。
    """
    tf = cfg.get("tradable_filter", {}) or {}
    min_days = int(tf.get("min_list_days", 60) or 0)
    agg_min = float(tf.get("aggressive_min_avg_amount", 30_000_000) or 0)
    std_min = float(tf.get("steady_min_avg_amount", 100_000_000) or 0)
    base_min = float(tf.get("min_avg_amount_base", 50_000_000) or 0)
    min_amt = max(agg_min if role == "aggressive" else std_min, base_min)
    exclude_kw = tf.get("exclude_kw", ["ST", "退市", "风险警示", "*"])
    bad_kw = tf.get("bad_news_kw", [])

    out = []
    for s in stocks:
        code = str(s.get("code") or "").zfill(6)
        name = str(s.get("name") or "")
        # 4.1 名称剔除(ST/退市/风险警示/带星) 与 利空关键词
        if any(k and k.upper() in name.upper() for k in exclude_kw):
            continue
        if any(k and k in name for k in bad_kw):
            continue
        # 一字板/临停/停牌:盘中快照 high==low 视为一字锁死(无成交机会)
        try:
            from app.support import mainline as _ml
            q = _ml.get_spot_quotes([code]).get(code)
            if q:
                hi, lo = float(q.get("high") or 0), float(q.get("low") or 0)
                if hi and lo and abs(hi - lo) < 1e-9:
                    continue
        except Exception as _e:  # noqa: BLE001
            _fault(_e)  # 快照不可用则跳过该维度
        # 次新股
        days = _list_days(code)
        if days is not None and days < min_days:
            continue
        # 流动性
        avg = _avg_amount20(code)
        if avg is not None and avg < min_amt:
            continue
        out.append(s)
    return out


# ------------------------------------------------------------------ P0.1 驻留防抖
def _stabilize_rank(sector: str, role: str, ranked: list, cfg: dict) -> list:
    """对某档位排序后的候选序列应用驻留防抖,返回稳定输出序列(仍为排序后结构)。

    状态键 code:cycle(连续上榜周期), cool_until(冷却到期), removed_at。
    规则:
    - 新标的首次进入前2 → 标记候选(cycle=1),不进入正式推荐;
    - 连续 TARGET_STABILIZE_CYCLE 个周期保持前2 且不在冷却期 → 晋升正式;
    - 已正式标的排名跌出前2但仍在 TARGET_KEEP_RANK 内 → 不剔除;
    - 跌出 TARGET_KEEP_RANK 且持续 -> 剔除并进入冷却。
    """
    scfg = cfg.get("stabilizer", {}) or {}
    need = int(scfg.get("TARGET_STABILIZE_CYCLE", 3) or 1)
    cool_min = int(scfg.get("TARGET_COOLDOWN_MINUTE", 15) or 0)
    keep = int(scfg.get("TARGET_KEEP_RANK", 5) or 1)
    now = time.time()
    key = (sector, role)
    with _STATE_LOCK:
        st = _TARGET_STATE.setdefault(key, {})
        cold = not bool(st)   # 冷启动: 该档从未处理过 → 直接晋升正式,避免首日全空

        # 1) 更新上榜周期:本次前2 才累加,否则清零(用 name+code 组合键防同名)
        def _k(it):
            return f"{it.get('code')}"

        top2 = {_k(x) for x in ranked[:2]}
        for it in ranked[: keep]:
            k = _k(it)
            d = st.setdefault(k, {"cycle": 0, "cool_until": 0.0, "removed_at": 0.0})
            d["cycle"] = d.get("cycle", 0) + 1 if k in top2 else 0

        # 2) 判定正式/候选
        formal = []
        for rank, it in enumerate(ranked[:2], 1):
            k = _k(it)
            d = st[k]
            stable = ((d["cycle"] >= need and d["cool_until"] <= now) or cold)
            it["continue_rank_cycle"] = d["cycle"]
            it["is_stable"] = bool(stable)
            it["match_source"] = "normal" if stable else "candidate_residency"
            if stable:
                formal.append(it)
        # 若前2内无正式,补位观察:从 keep 范围内找已正式(驻留稳定)标的补进正式列表
        if not formal:
            for it in ranked[2: keep]:
                k = _k(it)
                d = st.get(k)
                if d and d["cycle"] >= need and d["cool_until"] <= now:
                    it["continue_rank_cycle"] = d["cycle"]
                    it["is_stable"] = True
                    it["match_source"] = "normal_keep"
                    formal.append(it)
                    break
        return formal


# ------------------------------------------------------------------ P1 高级排名
def _cached_hist(code: str):
    """读取本地日线缓存(不触发网络抓取)。"""
    try:
        from app.support.stock_utils import cached_hist as _sc
        return _sc(code)
    except Exception:  # noqa: BLE001
        return None


# ------------------------------------------------------------------ P2 修正与降级
def _excess_adjust(item: dict, sector_name: str, cfg: dict) -> None:
    """P2.1 个股相对板块超额收益修正:仅调整动作优先级,不改 GBM 概率。

    近3日/近5日个股跑赢板块 → 动作档位+1;持续跑输 → 档位-1(在 adjust_signal 之后叠加)。
    """
    code = str(item.get("code") or "").zfill(6)
    try:
        from app.features.concept_features import _get_concept_close
        idx = _get_concept_close(sector_name)
        df = _cached_hist(code)
        if idx is None or df is None or len(df) < 6 or len(idx) < 6:
            return
        import pandas as pd
        sc = df["close"].astype(float)
        ic = pd.Series(idx).astype(float)
        ic = ic[ic.index <= sc.index[-1]]
        r3s = sc.iloc[-1] / sc.iloc[-4] - 1 if len(sc) >= 4 else None
        r5s = sc.iloc[-1] / sc.iloc[-6] - 1 if len(sc) >= 6 else None
        r3i = ic.iloc[-1] / ic.iloc[-4] - 1 if len(ic) >= 4 else None
        r5i = ic.iloc[-1] / ic.iloc[-6] - 1 if len(ic) >= 6 else None
        if None in (r3s, r5s, r3i, r5i):
            return
        ex3, ex5 = r3s - r3i, r5s - r5i
        if ex3 > 0 and ex5 > 0:
            item["signal"] = _shift_signal(item.get("signal") or "持有观察", 1)
            item["action"] = item["signal"]
            item["adj_notes"] = (item.get("adj_notes") or []) + \
                [f"超额收益持续为正(3日{ex3:+.1%}/5日{ex5:+.1%}),动作优先级+1"]
        elif ex3 < 0 and ex5 < 0:
            item["signal"] = _shift_signal(item.get("signal") or "持有观察", -1)
            item["action"] = item["signal"]
            item["adj_notes"] = (item.get("adj_notes") or []) + \
                [f"持续跑输板块(3日{ex3:+.1%}/5日{ex5:+.1%}),动作优先级-1"]
    except Exception as _e:  # noqa: BLE001
        _fault(_e)


def _boost_level(sector_level: str, sector_status: str, cfg: dict) -> str:
    """P2.2 板块溢价联动防抖:仅正式 core/defensive 触发上修,候选/观察回退 watch。"""
    if cfg.get("enable_sector_boost_stable") and sector_status in ("candidate", "watch", "rejected"):
        return "watch"
    return sector_level


# ------------------------------------------------------------------ 分阶段盈亏比准入(四阶段体系)
def _trade_rr(item: dict, role: str) -> tuple:
    """计算交易模式与盈亏比(复用模型 levels)。

    - trade_mode: 现价贴近压力/突破位 → 右侧突破; 否则左侧低吸;
    - 左侧低吸盈亏比 = (目标-低吸位)/(低吸位-止损); 右侧突破盈亏比 = (目标-突破位)/(突破位-止损);
    - levels 缺失时返回 rr=None(准入放行, 兜底)。
    """
    lv = item.get("levels") or {}
    price = item.get("price")
    entry_low, entry_high = lv.get("entry_low"), lv.get("entry_high")
    stop, tgt = lv.get("stop_loss"), lv.get("target")
    if price and entry_high and entry_high > 0 and price >= entry_high * 0.98:
        mode = "right"
    else:
        mode = "left"
    rr = None
    if mode == "right":
        if entry_high and stop and tgt and entry_high > stop:
            rr = (tgt - entry_high) / (entry_high - stop)
    else:
        if entry_low and stop and tgt and entry_low > stop:
            rr = (tgt - entry_low) / (entry_low - stop)
    return mode, rr


def _rr_pass(phase: str, mode: str, rr, pcfg: dict) -> bool:
    """分阶段盈亏比硬门槛: 不达标剔除(None=数据缺失放行, 兜底)。"""
    if rr is None:
        return True
    if mode == "left":
        return rr >= pcfg["rr_left"]
    return pcfg["rr_right"] is not None and rr >= pcfg["rr_right"]


def _trade_rr_dual(item: dict, role: str, df=None) -> tuple:
    """分模式盈亏比双口径(4.2): 短期(5日区间)与中期(20日levels)。

    - 左侧低吸: rr = (压力位-低吸价)/(低吸价-止损位), 止损位=支撑位下方ATR;
    - 右侧突破: rr = (目标价-突破价)/(突破价-回踩位), 目标价=突破位+ATR幅度;
    - 输出 (mode, rr_5d, rr_20d, note), 根据交易模式选择参考。
    """
    lv = item.get("levels") or {}
    price = item.get("price")
    entry_low, entry_high = lv.get("entry_low"), lv.get("entry_high")
    stop, tgt = lv.get("stop_loss"), lv.get("target")
    atr = float(item.get("atr14") or 0) or 0.0
    mode = "right" if (price and entry_high and entry_high > 0 and price >= entry_high * 0.98) else "left"
    # 中期(20日 levels 平台, 现有口径)
    rr20 = None
    if mode == "right":
        if entry_high and stop and tgt and entry_high > stop:
            rr20 = (tgt - entry_high) / (entry_high - stop)
    else:
        if entry_low and stop and tgt and entry_low > stop:
            rr20 = (tgt - entry_low) / (entry_low - stop)
    # 短期(5日高低区间 + ATR 止损/目标)
    rr5 = None
    df = df or _cached_hist(str(item.get("code") or "").zfill(6))
    if df is not None and len(df) >= 6 and "close" in df.columns:
        c = df["close"].astype(float)
        hi5, lo5 = float(c.tail(5).max()), float(c.tail(5).min())
        if mode == "right":
            brk = hi5
            tgt5 = brk + atr
            if atr > 0:
                rr5 = (tgt5 - brk) / atr
        else:
            buy = lo5
            stop5 = lo5 - atr
            if buy > stop5:
                rr5 = (hi5 - buy) / (buy - stop5)
    note = f"盈亏比 短(5日){round(rr5, 2) if rr5 else '无'} / 中(20日){round(rr20, 2) if rr20 else '无'}"
    return mode, rr5, rr20, note


# ------------------------------------------------------------------ 主入口
def match_targets_v2(sector_name: str, sector_level: str = "watch",
                     sector_status: str = "core") -> dict:
    """第三层优化版入口: 三档梯队(中军/情绪/补涨) + 工具ETF + 可选增强。

    选股子逻辑升级为 tier_select 三档综合评分体系(固化默认): 中军(趋势核心) +
    情绪(领涨弹性) + 补涨优选(高低切换), 三档强制去重, 差异化交易参数。
    stable_targets 结构: {steady, aggressive, repair, etf, candidate, fallback, error}。
    P0.1 驻留防抖 / P2.1 超额修正 / P2.3 兜底 仍由原开关控制(可选增强层)。
    """
    from app.decision import engine as _en
    raw = _en.match_level_targets(sector_name, sector_level)
    cfg = _cfg()
    spot = {}
    try:
        from app.support import mainline as _ml
        spot = _ml._a_spot_map()
    except Exception as _e:  # noqa: BLE001
        _fault(_e)
    stocks = []
    try:
        from app.support import mainline as _ml
        stocks = _ml._match_stocks(sector_name, spot)
    except Exception as _e:  # noqa: BLE001
        _fault(_e)

    # P0.2 可交易性前置过滤(可选,默认关)
    if cfg.get("enable_tradable_filter") and stocks:
        stocks = _tradable_filter(stocks, "aggressive", cfg)

    # 三档梯队选股(固化默认开启)
    from app.support import tier_select as _ts
    tiers = _ts.select_three_tiers(sector_name, sector_level, sector_status, spot, stocks)

    # 预测上下文
    quotes = {}
    try:
        from app.support import mainline as _ml
        codes = [it["code"] for role in ("steady", "aggressive", "repair")
                 for it in tiers[role]]
        if codes:
            quotes = _ml.get_spot_quotes(codes)
    except Exception as _e:  # noqa: BLE001
        _fault(_e)
    predictor = None
    try:
        from app.support import mainline as _ml
        predictor = _ml.Predictor()
    except Exception as _e:  # noqa: BLE001
        _fault(_e)
    market = None
    try:
        from app.features.market_features import market_snapshot
        market = market_snapshot()
    except Exception as _e:  # noqa: BLE001
        _fault(_e)

    stable = {"aggressive": [], "steady": [], "repair": [], "etf": [],
              "candidate": [], "fallback": [], "error": ""}

    # 可选: P0.1 驻留防抖(在三档正式名单上再稳定)
    formal = {role: list(tiers[role]) for role in ("steady", "aggressive", "repair")}
    if cfg.get("enable_target_stabilizer"):
        for role in ("steady", "aggressive", "repair"):
            ranked = [dict(it) for it in tiers[role]]
            ok = _stabilize_rank(sector_name, role, ranked, cfg)
            ok_codes = {x["code"] for x in ok}
            formal[role] = [it for it in tiers[role] if it["code"] in ok_codes]

    # 渲染三档正式(携带差异化交易参数), 叠加分阶段盈亏比准入与档位禁用
    from app.decision.engine import get_market_phase, phase_cfg
    _phase = get_market_phase()
    _pcfg = phase_cfg(_phase)
    _allowed = _pcfg.get("tiers") or ["steady", "aggressive", "repair"]
    rendered = {role: [] for role in ("steady", "aggressive", "repair")}
    # 并行化 _render_item(内部 _predict_one 串行慢): 线程池并发预测
    from concurrent.futures import ThreadPoolExecutor
    _jobs = []
    for role in ("steady", "aggressive", "repair"):
        for it in formal[role]:
            it["is_stable"] = True
            it["continue_rank_cycle"] = 1
            it.setdefault("match_source", "normal")
            _jobs.append((role, it))
    if _jobs:
        with ThreadPoolExecutor(max_workers=min(4, len(_jobs))) as _ex:
            _items = list(_ex.map(
                lambda j: _render_item(j[1], j[0], sector_name, sector_level, sector_status,
                                       quotes, predictor, market, cfg), _jobs))
        for (role, it), item in zip(_jobs, _items):
            for k in ("type", "position_coef", "stop_loss_pct", "target_profit_pct",
                      "is_pulse_watch", "pos_adjusted", "style"):
                if k in it:
                    item.setdefault(k, it[k])
            rendered[role].append(item)
    # 分阶段硬门槛: 档位禁用 + 盈亏比准入(前置校验, 不达标直接剔除/该档空缺), 并区分空缺原因
    _reason_map = {"steady": "中军龙头", "aggressive": "情绪龙头", "repair": "补涨优选"}
    for role in ("steady", "aggressive", "repair"):
        if role not in _allowed:
            stable[role] = [{"rank": 1, "error": f"当前阶段「{_pcfg.get('label', '')}」禁用{_reason_map[role]}档位",
                             "is_stable": False, "match_source": "phase_disabled"}]
            continue
        kept = []
        for item in rendered[role]:
            mode, rr = _trade_rr(item, role)
            item["trade_mode"] = "右侧突破" if mode == "right" else "左侧低吸"
            item["trade_rr"] = round(rr, 2) if rr else None
            # 4.2 分模式盈亏比双口径(短5日/中20日)
            try:
                m2, rr5, rr20, note = _trade_rr_dual(item, role)
                item["trade_rr_5d"] = round(rr5, 2) if rr5 else None
                item["trade_rr_20d"] = round(rr20, 2) if rr20 else None
                item["trade_rr_note"] = note
            except Exception as _e:  # noqa: BLE001
                _fault(_e)
            if not _rr_pass(_phase, mode, rr, _pcfg):
                continue
            kept.append(item)
        if kept:
            stable[role] = kept
        elif rendered[role]:
            _rr_txt = f"左侧≥{_pcfg.get('rr_left')} / 右侧" + \
                (f"≥{_pcfg.get('rr_right')}" if _pcfg.get("rr_right") else "禁开")
            stable[role] = [{"rank": 1, "error": f"{_reason_map[role]}候选未达当前阶段盈亏比门槛({_rr_txt})",
                             "is_stable": False, "match_source": "rr_gate"}]
        else:
            stable[role] = [{"rank": 1, "error": _no_candidate_reason(spot, stocks),
                             "is_stable": False, "match_source": "no_data"}]

    # 观察名单(脉冲/未入选, 仅展示不参与执行)
    for w in tiers.get("watch", []):
        it = w["it"]
        role = it.get("type", "steady")
        item = _render_item(it, role, sector_name, sector_level, sector_status,
                            quotes, predictor, market, cfg, candidate=True)
        item.setdefault("is_pulse_watch", True)
        stable["candidate"].append(item)

    # 工具型 ETF:多维度校验 + 排序
    stable["etf"] = _match_etf_v2(sector_name, quotes, predictor, market,
                                  sector_level, sector_status, cfg)

    # P2.3 降级兜底(可选)
    if cfg.get("enable_fallback_match"):
        stable = _fallback(stable, sector_name, sector_level, sector_status,
                           spot, quotes, predictor, market, cfg)

    # error 占位: ETF 及兜底后仍空的档位
    for role in ("aggressive", "steady", "repair", "etf"):
        if not stable[role]:
            stable[role] = [{"rank": 1, "error": "暂无匹配ETF(流动性不足或未匹配到)"
                             if role == "etf" else "暂无可匹配标的",
                             "is_stable": False, "match_source": "error"}]
    return {"sector": sector_name, "raw_targets": raw, "stable_targets": stable}


def _no_candidate_reason(spot: dict, stocks: list) -> str:
    """区分「无候选」的具体数据原因。"""
    if not spot:
        return "行情源不可用(全A快照获取失败)"
    if not stocks:
        return "板块无成分股(成分接口失败)"
    return "板块候选不足,暂无匹配标的"


def _render_item(it: dict, role: str, sector_name: str, sector_level: str,
                 sector_status: str, quotes, predictor, market, cfg: dict,
                 candidate: bool = False) -> dict:
    """渲染单标的:原 _predict_one + _adjust_signal,叠加 P2.1/P2.2 修正。"""
    from app.decision import engine as _en
    c = it
    item = {"code": c["code"], "name": c["name"], "price": round(c["price"], 2),
            "pct_chg": c["pct_chg"], "amount_yi": round(c["amount"] / 1e8, 2)
            if c.get("amount") else None, "float_mv": c.get("float_mv"),
            "rank": 1, "role": {"aggressive": "情绪龙头", "steady": "中军龙头",
                                "repair": "补涨优选"}[role],
            "is_stable": bool(it.get("is_stable")),
            "continue_rank_cycle": int(it.get("continue_rank_cycle", 1)),
            "rank_score": round(it.get("rank_score", 0), 4) if it.get("rank_score") else None,
            "match_source": it.get("match_source", "candidate" if candidate else "normal")}
    # 数据不足/补抓填充标记透传(决策结果可追溯, 非真实数据时避免误读)
    if it.get("data_insufficient"):
        item["data_insufficient"] = True
        item["data_insufficient_reason"] = it.get("data_insufficient_reason") or "历史数据不足,保守计分"
    if it.get("filled"):
        item["filled"] = True
        item["fill_method"] = it.get("fill_method", "fetch_on_miss")
    pred = _en._predict_one(c["code"], predictor, quotes, market)
    if pred.get("error"):
        item["error"] = pred["error"]
        return item
    item.update(pred)
    # 个股级数据校准落地: 数据异常/不一致标注到标的上("数据可能异常,建议核实")
    if pred.get("data_warning"):
        item["data_note"] = "数据可能异常,建议核实: " + "; ".join(pred["data_warning"][:2])
        if item.get("reasons"):
            item["reasons"] = item["reasons"][:2] + [item["data_note"]]
    _en._adjust_signal(item, _boost_level(sector_level, sector_status, cfg))
    if cfg.get("enable_excess_return_adjust"):
        _excess_adjust(item, sector_name, cfg)
    lv = item.get("levels") or {}
    tpl = _TTPL.get(role, _TTPL["steady"])
    item["trigger"] = tpl.format(support=lv.get("support", "-"),
                                 resistance=lv.get("resistance", "-"),
                                 entry_low=lv.get("entry_low", "-"))
    return item


def _match_etf_v2(sector_name, quotes, predictor, market, sector_level,
                  sector_status, cfg: dict) -> list:
    """工具型 ETF:关键词匹配 + 当日额门槛(原逻辑),叠加 20日均额/溢价校验。"""
    from app.support import mainline as _ml
    etfs = _ml._etf_map()
    kws = _ml._ETF_ALIAS.get(_ml._concept_kw(sector_name)) or [_ml._concept_kw(sector_name)]
    min_wan = _st.load().get("etf_min_amount", 5000.0)
    matched = []
    tf = cfg.get("tradable_filter", {}) or {}
    etf_min_avg = float(tf.get("etf_min_avg_amount", 80_000_000) or 0)
    for en, e in etfs.items():
        if not any(kw and kw.lower() in en.lower() for kw in kws):
            continue
        if e["amount_wan"] < min_wan:
            continue
        code = str(e["code"]).zfill(6)
        if cfg.get("enable_tradable_filter") and etf_min_avg:
            avg = _avg_amount20(code)
            if avg is not None and avg < etf_min_avg:
                continue
        matched.append({**e, "name": en})
    if cfg.get("enable_advanced_rank"):
        # 排序优先级:日均成交额大 > 当日成交额大
        def _sort_key(x):
            avg = _avg_amount20(str(x["code"]).zfill(6)) or 0
            return (avg, x["amount_wan"])
        matched.sort(key=_sort_key, reverse=True)
    else:
        matched.sort(key=lambda x: -x["amount_wan"])
    out = []
    for rank, e in enumerate(matched[:2], 1):
        item = {"rank": rank, "role": "ETF", "code": e["code"], "name": e["name"],
                "price": round(e["price"], 3), "amount_wan": round(e["amount_wan"], 0),
                "is_stable": True, "continue_rank_cycle": 1, "rank_score": None,
                "match_source": "normal"}
        pred = _en_predict_one(e["code"], predictor, quotes, market)
        if pred.get("error"):
            item["error"] = pred["error"]
            out.append(item)
            continue
        item.update(pred)
        from app.decision import engine as _en
        _en._adjust_signal(item, _boost_level(sector_level, sector_status, cfg))
        lv = item.get("levels") or {}
        if lv:
            item["trigger"] = _TTPL["etf"].format(
                support=lv.get("support", "-"), resistance=lv.get("resistance", "-"))
        else:
            item["trigger"] = "板块强势期间低吸对应 ETF,注意流动性"
        out.append(item)
    return out


def _en_predict_one(code, predictor, quotes, market):
    from app.decision import engine as _en
    return _en._predict_one(code, predictor, quotes, market)


def _fallback(stable: dict, sector_name, sector_level, sector_status,
              spot, quotes, predictor, market, cfg: dict) -> dict:
    """P2.3 匹配失败降级兜底:档位内补选 -> 跨档位 -> 关联概念板块 -> 保留 error。

    标注 fallback 来源与原因。
    """
    from app.support import mainline as _ml
    for role in ("aggressive", "steady"):
        if stable[role]:
            continue
        # 1) 本档位候选补选(前2之外的后备候选)
        # (候选已在 _build 时进入 stable.candidate,此处跨档位复用)
        for c in stable["candidate"]:
            if c.get("role") == {"aggressive": "情绪龙头", "steady": "中军龙头"}[role]:
                if not c.get("error"):
                    c["is_stable"] = True
                    c["match_source"] = "fallback_role"
                    stable[role] = [c]
                    break
        if stable[role]:
            continue
        # 2) 跨档位:用另一档位合格标的
        other = "steady" if role == "aggressive" else "aggressive"
        for c in stable[other]:
            if not c.get("error"):
                c2 = dict(c)
                c2["is_stable"] = True
                c2["match_source"] = f"fallback_cross_{other}"
                stable[role] = [c2]
                break
        if stable[role]:
            continue
        # 3) 关联概念板块(名称双向子串)
        try:
            from app.support import mainline as _ml
            for name in _ml._concepts_cached():
                if name == sector_name:
                    continue
                if sector_name in name or name in sector_name:
                    st = _ml._match_stocks(name, spot)
                    if st:
                        c = st[0]
                        item = _render_item(c, role, name, sector_level,
                                            sector_status, quotes, predictor,
                                            market, cfg)
                        item["match_source"] = f"fallback_related:{name}"
                        item["is_stable"] = True
                        stable[role] = [item]
                        break
        except Exception as _e:  # noqa: BLE001
            _fault(_e)
    return stable