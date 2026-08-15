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


# ------------------------------------------------------------------ 配置读取
def _cfg() -> dict:
    """读取 target_match 配置组(settings 默认 + json 覆盖)。"""
    return _st.load().get("target_match", {}) or {}


def _switch(name: str) -> bool:
    """读取单个开关(默认关闭)。"""
    return bool(_cfg().get(name, False))


def any_enabled() -> bool:
    """是否有任一优化开关开启。全关时走 100% 原逻辑直通。"""
    c = _cfg()
    return any(c.get(k, False) for k in (
        "enable_target_stabilizer", "enable_tradable_filter", "enable_advanced_rank",
        "enable_excess_return_adjust", "enable_sector_boost_stable",
        "enable_fallback_match"))


# ------------------------------------------------------------------ 内存状态
# P0.1 标的驻留防抖状态:{(sector, role): {code: {cycle, cool_until, removed_at}}}
_STATE_LOCK = threading.Lock()
_TARGET_STATE = {}


def _reset_state(sector: str, role: str) -> None:
    with _STATE_LOCK:
        _TARGET_STATE.pop((sector, role), None)


# ------------------------------------------------------------------ 可交易性过滤(P0.2)
def _is_zt_limit(code: str) -> bool:
    """涨跌停幅度:创业板/科创板 20%,其余 10%(ST 已在候选池剔除)。"""
    return code.startswith(("30", "68"))


def _list_days(code: str) -> int | None:
    """上市交易天数(用历史行数近似)。数据不可用返回 None(不参与过滤)。"""
    try:
        from app.data import fetcher as _f
        df = _f._load_cache(code)
        if df is None or df.empty:
            return None
        return int(len(df))
    except Exception:  # noqa: BLE001
        return None


def _avg_amount20(code: str) -> float | None:
    """20日日均成交额(元),基于本地日线缓存。不可用返回 None。"""
    try:
        from app.data import fetcher as _f
        df = _f._load_cache(code)
        if df is None or df.empty or "amount" not in df.columns:
            return None
        amt = df["amount"].tail(20).astype(float)
        if amt.empty:
            return None
        return float(amt.mean())
    except Exception:  # noqa: BLE001
        return None


def _tradable_filter(stocks: list, role: str, cfg: dict) -> list:
    """P0.2 可交易性基础过滤:命中任一规则直接剔除(不进入选股排名)。

    - 个股通用:一字涨停/一字跌停、盘中临停、停牌(实时快照 high==low 判定);
    - 次新股:上市天数 < min_list_days;
    - 流动性:20日日均成交额低于档位阈值。
    任一维度数据不可用时跳过该维度(不误杀)。
    """
    tf = cfg.get("tradable_filter", {}) or {}
    min_days = int(tf.get("min_list_days", 60) or 0)
    agg_min = float(tf.get("aggressive_min_avg_amount", 30_000_000) or 0)
    std_min = float(tf.get("steady_min_avg_amount", 100_000_000) or 0)
    min_amt = agg_min if role == "aggressive" else std_min

    out = []
    for s in stocks:
        code = str(s.get("code") or "").zfill(6)
        # 一字板/临停/停牌:盘中快照 high==low 视为一字锁死(无成交机会)
        try:
            from app.support import mainline as _ml
            q = _ml.get_spot_quotes([code]).get(code)
            if q:
                hi, lo = float(q.get("high") or 0), float(q.get("low") or 0)
                if hi and lo and abs(hi - lo) < 1e-9:
                    continue
        except Exception:  # noqa: BLE001
            pass  # 快照不可用则跳过该维度
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
            stable = (d["cycle"] >= need and d["cool_until"] <= now)
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
        from app.data import fetcher as _f
        return _f._load_cache(code)
    except Exception:  # noqa: BLE001
        return None


def _agg_dims(stocks: list, spot: dict, zt_map: dict, corr_cache: dict,
              sector_name: str, cfg: dict) -> dict:
    """激进型·情绪龙头各维度原始值:{code: {dim: value}}。

    dims: ladder(连板强度,40%), pct_chg(10/20cm归一化涨幅,30%),
          correlation(个股与板块指数相关,20%), amount(当日成交额,10%)。
    """
    w = ((cfg.get("advanced_rank", {}) or {}).get("aggressive_weights", {}) or {})
    out = {}
    for c in stocks:
        code = str(c["code"])
        s = spot.get(code) or {}
        pct = s.get("pct_chg") or 0
        limit = 0.20 if _is_zt_limit(code) else 0.10
        boards = int(zt_map.get(code, 0) or 0)
        corr = corr_cache.get(code)
        out[code] = {
            "ladder": boards,
            "pct_chg": max(0.0, min(1.0, pct / limit)) if limit else 0.0,
            "correlation": corr if corr is not None else None,
            "amount": s.get("amount") or 0,
        }
    return out


def _std_dims(stocks: list, spot: dict, cfg: dict) -> dict:
    """稳健型·中军龙头各维度原始值:{code: {dim: value}}。

    dims: market_cap(流通市值,40%), avg_amount(20日日均成交额,30%),
          trend(MA20斜率趋势强度,20%), amount(当日成交额,10%)。
    """
    w = ((cfg.get("advanced_rank", {}) or {}).get("steady_weights", {}) or {})
    out = {}
    for c in stocks:
        code = str(c["code"])
        s = spot.get(code) or {}
        df = _cached_hist(code)
        trend = 0.5
        if df is not None and len(df) >= 25 and "close" in df.columns:
            ma20 = df["close"].astype(float).rolling(20).mean()
            m = ma20.dropna()
            if len(m) >= 5 and m.iloc[-6]:
                slope = m.iloc[-1] / m.iloc[-6] - 1
                trend = max(0.0, min(1.0, 0.5 + slope * 20))
        out[code] = {
            "market_cap": s.get("float_mv") or s.get("total_mv") or 0,
            "avg_amount": _avg_amount20(code) or 0,
            "trend": trend,
            "amount": s.get("amount") or 0,
        }
    return out


def _rank_advanced(stocks: list, role: str, spot: dict, sector_name: str,
                   cfg: dict) -> list:
    """P1 高级排名:各维度用候选池 min-max 归一后按权重加权,降序取前2。"""
    w = ((cfg.get("advanced_rank", {}) or {}).get(
        "aggressive_weights" if role == "aggressive" else "steady_weights", {}) or {})
    if not stocks:
        return []
    corr_cache = {}
    zt_map = {}
    if role == "aggressive":
        try:
            from app.support import mainline as _ml
            for z in _ml._zt_pool():
                zt_map[str(z.get("code"))] = int(z.get("boards", 1) or 1)
        except Exception:  # noqa: BLE001
            pass
        # 个股与板块指数相关性(独立行情庄股相关性低 -> 降分)
        try:
            from app.features.concept_features import _get_concept_close
            idx = _get_concept_close(sector_name)
            if idx is not None and len(idx) > 5:
                import pandas as pd
                ir = pd.Series(idx).astype(float).pct_change().dropna()
                for c in stocks:
                    code = str(c["code"])
                    df = _cached_hist(code)
                    if df is None or len(df) < 10 or "close" not in df.columns:
                        continue
                    sr = df["close"].astype(float).pct_change().dropna()
                    al = pd.concat([ir, sr], axis=1, join="inner").dropna()
                    if len(al) >= 5:
                        corr_cache[code] = float(al.iloc[:, 0].corr(al.iloc[:, 1]))
        except Exception:  # noqa: BLE001
            pass
    dims = _agg_dims(stocks, spot, zt_map, corr_cache, sector_name, cfg) if \
        role == "aggressive" else _std_dims(stocks, spot, cfg)

    # 每维度 min-max 归一(全等或缺失 -> 0.5 中性)
    norm = {}
    for k in w:
        vals = [dims[c][k] for c in dims if dims[c].get(k) is not None]
        lo, hi = (min(vals), max(vals)) if vals else (0.0, 0.0)
        norm[k] = {}
        for c in dims:
            v = dims[c].get(k)
            if v is None:
                norm[k][c] = 0.5
            elif hi == lo:
                norm[k][c] = 0.5
            else:
                norm[k][c] = (v - lo) / (hi - lo)

    scored = []
    for c in stocks:
        code = str(c["code"])
        comp = sum(wt * norm[k].get(code, 0.5) for k, wt in w.items())
        c["rank_score"] = round(comp, 4)
        c["_adv_dims"] = {k: round(v, 4) for k, v in dims[code].items()
                          if isinstance(v, (int, float))}
        scored.append(c)
    scored.sort(key=lambda x: -x["rank_score"])
    return scored[:2]


# ------------------------------------------------------------------ P2 修正与降级
def _excess_adjust(item: dict, sector_name: str, cfg: dict) -> None:
    """P2.1 个股相对板块超额收益修正:仅调整动作优先级,不改 GBM 概率。

    近3日/近5日个股跑赢板块 → 动作档位+1;持续跑输 → 档位-1(在 adjust_signal 之后叠加)。
    """
    from app.decision.engine import _shift_signal
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
    except Exception:  # noqa: BLE001
        pass


def _boost_level(sector_level: str, sector_status: str, cfg: dict) -> str:
    """P2.2 板块溢价联动防抖:仅正式 core/defensive 触发上修,候选/观察回退 watch。"""
    if cfg.get("enable_sector_boost_stable") and sector_status in ("candidate", "watch", "rejected"):
        return "watch"
    return sector_level


# ------------------------------------------------------------------ 主入口
def match_targets_v2(sector_name: str, sector_level: str = "watch",
                     sector_status: str = "core") -> dict:
    """第三层优化版入口,输出分层结果 {raw_targets, stable_targets}。

    sector_status: 来自主线稳定器的层级(core/defensive/watch/candidate/rejected),
    用于 P2.2 溢价联动与高级选股的正式/候选区分。
    全关时 stable_targets 结构与原 match_level_targets 完全一致(逐字段拷贝)。
    """
    from app.decision import engine as _en
    raw = _en.match_level_targets(sector_name, sector_level)

    if not any_enabled():
        # 100% 兼容原逻辑:stable 直接透传 raw(逐份深拷贝,防上层误改 raw)
        import copy
        stable = {
            "aggressive": copy.deepcopy(raw["aggressive"]["items"]),
            "steady": copy.deepcopy(raw["steady"]["items"]),
            "etf": copy.deepcopy(raw["etf"]["items"]),
            "candidate": [],
            "fallback": [],
            "error": "",
        }
        return {"sector": sector_name, "raw_targets": raw, "stable_targets": stable}

    cfg = _cfg()
    spot = {}
    try:
        from app.support import mainline as _ml
        spot = _ml._a_spot_map()
    except Exception:  # noqa: BLE001
        pass
    stocks = []
    try:
        from app.support import mainline as _ml
        stocks = _ml._match_stocks(sector_name, spot)
    except Exception:  # noqa: BLE001
        pass

    # P0.2 前置过滤
    if cfg.get("enable_tradable_filter") and stocks:
        stocks = _tradable_filter(stocks, "aggressive", cfg)
        # 中军流动性阈值不同,用同一批再按中军阈值过滤
        if cfg.get("tradable_filter", {}).get("steady_min_avg_amount"):
            stocks_std = _tradable_filter(list(stocks), "steady", cfg)
        else:
            stocks_std = list(stocks)
    else:
        stocks_std = list(stocks)

    # P1 分档选股
    agg_cands, std_cands = _select(spot, stocks, stocks_std, sector_name, cfg)

    # 预测 + 信号修正(核心函数原样复用)
    quotes = {}
    try:
        from app.support import mainline as _ml
        codes = [x["code"] for x in agg_cands + std_cands]
        if codes:
            quotes = _ml.get_spot_quotes(codes)
    except Exception:  # noqa: BLE001
        pass
    predictor = None
    try:
        from app.support import mainline as _ml
        predictor = _ml.Predictor()
    except Exception:  # noqa: BLE001
        pass
    market = None
    try:
        from app.features.market_features import market_snapshot
        market = market_snapshot()
    except Exception:  # noqa: BLE001
        pass

    stable = {"aggressive": [], "steady": [], "etf": [], "candidate": [],
              "fallback": [], "error": ""}

    # 正式/候选 分离:P0.1 防抖开启时按驻留判定;关闭时前2直接为正式
    def _build(role, cands):
        if cfg.get("enable_target_stabilizer"):
            formal = _stabilize_rank(sector_name, role, cands, cfg)
            cands = [c for c in cands if not c.get("is_stable")]
            stable["candidate"].extend(cands)
            return formal
        for i, c in enumerate(cands[:2], 1):
            c["is_stable"] = True
            c["continue_rank_cycle"] = 1
            c["match_source"] = "normal"
        return cands[:2]

    agg_formal = _build("aggressive", agg_cands)
    std_formal = _build("steady", std_cands)

    # 渲染正式标的(复用原 _predict_one/_adjust_signal,仅做后置修正)
    for role, formal in (("aggressive", agg_formal), ("steady", std_formal)):
        for it in formal:
            item = _render_item(it, role, sector_name, sector_level, sector_status,
                                quotes, predictor, market, cfg)
            stable[role].append(item)

    # 候选观察标的也给出基础信息(仅展示,不参与执行计划)
    for role, cands in (("aggressive", agg_cands), ("steady", std_cands)):
        for it in cands:
            if it.get("is_stable"):
                continue
            item = _render_item(it, role, sector_name, sector_level, sector_status,
                                quotes, predictor, market, cfg, candidate=True)
            stable["candidate"].append(item)

    # 工具型 ETF:多维度校验 + 排序
    stable["etf"] = _match_etf_v2(sector_name, quotes, predictor, market,
                                  sector_level, sector_status, cfg)

    # P2.3 降级兜底
    if cfg.get("enable_fallback_match"):
        stable = _fallback(stable, sector_name, sector_level, sector_status,
                           spot, quotes, predictor, market, cfg)

    # error 占位保留(与原始逻辑一致)
    for role in ("aggressive", "steady", "etf"):
        if not stable[role]:
            stable[role] = [{"rank": 1, "error": "暂无可匹配标的(数据源受限)",
                             "is_stable": False, "match_source": "error"}]
    return {"sector": sector_name, "raw_targets": raw, "stable_targets": stable}


def _select(spot, stocks, stocks_std, sector_name: str, cfg: dict):
    """分档选股:开启高级排名用综合得分,关闭沿用原单一指标排名。"""
    from app.support import mainline as _ml
    if cfg.get("enable_advanced_rank"):
        agg = _rank_advanced(list(stocks), "aggressive", spot, sector_name, cfg)
        std = _rank_advanced(list(stocks_std), "steady", spot, sector_name, cfg)
    else:
        agg = sorted(stocks, key=lambda s: -(s.get("pct_chg") or 0))[:2]
        std = sorted(stocks_std, key=lambda s: -(s.get("amount") or 0))[:2]
    return agg, std


def _render_item(it: dict, role: str, sector_name: str, sector_level: str,
                 sector_status: str, quotes, predictor, market, cfg: dict,
                 candidate: bool = False) -> dict:
    """渲染单标的:原 _predict_one + _adjust_signal,叠加 P2.1/P2.2 修正。"""
    from app.decision import engine as _en
    c = it
    item = {"code": c["code"], "name": c["name"], "price": round(c["price"], 2),
            "pct_chg": c["pct_chg"], "amount_yi": round(c["amount"] / 1e8, 2)
            if c.get("amount") else None, "float_mv": c.get("float_mv"),
            "rank": 1, "role": {"aggressive": "情绪龙头", "steady": "中军龙头"}[role],
            "is_stable": bool(it.get("is_stable")),
            "continue_rank_cycle": int(it.get("continue_rank_cycle", 1)),
            "rank_score": round(it.get("rank_score", 0), 4) if it.get("rank_score") else None,
            "match_source": it.get("match_source", "candidate" if candidate else "normal")}
    pred = _en._predict_one(c["code"], predictor, quotes, market)
    if pred.get("error"):
        item["error"] = pred["error"]
        return item
    item.update(pred)
    _en._adjust_signal(item, _boost_level(sector_level, sector_status, cfg))
    if cfg.get("enable_excess_return_adjust"):
        _excess_adjust(item, sector_name, cfg)
    lv = item.get("levels") or {}
    tpl = _en._TRIGGER_TPL.get(role, _en._TRIGGER_TPL["steady"])
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
            item["trigger"] = _en._TRIGGER_TPL["etf"].format(
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
        except Exception:  # noqa: BLE001
            pass
    return stable