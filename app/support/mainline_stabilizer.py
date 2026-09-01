# -*- coding: utf-8 -*-
"""盘中主线防抖稳定器(外层独立模块,第四轮改造)。

背景问题
--------
盘中高频轮询时,原始流水线 `engine.mainline_select()` 直接基于**实时抓取**的单日
资金流打分(资金面权重最高 40 分、按排名线性计分),盘中微小净流入率抖动就会造成
名次互换、分数跳变,导致「核心主线」在几分钟内反复横跳,决策面板无所适从。

设计原则(与任务的硬性约束一致)
------------------------------
1. **禁止修改** `mainline.sector_scores` 内部打分公式、`engine.mainline_select`
   的准入过滤与一票否决逻辑 —— 原始流水线完整保留,继续输出 `raw` 原始结果;
2. 本模块作为**独立外层防抖器**,只对原始结果做「时间维度」的稳定化,不改动任何
   单次快照的内部计算;实盘对外输出从本模块取 `stable`,原始流水线结果仅作调试/回测;
3. 全部参数可配置(`settings.decision.mainline`),`enable_stabilizer=False` 时
   直接透传原始结果,天然兼容历史回测;
4. 保留原有 `reject_reason` 日志,每条稳定结果额外附加防抖日志字段。

防抖机制(Feature 清单)
----------------------
- `intraday_smooth_window`: 单日资金流滚动时间窗口均值(分钟),0=关闭;**5日资金表
  完全不参与平滑**,保证中长期资金趋势不被打平;
- `rank_delta_thresh`: 排名打分阻尼(在 `sector_scores` 内实现,此处仅透传参数);
- `STABILIZE_CYCLE`: 连续 N 个快照周期确认(驻留晋升/冷却出池/同池替换/防倒挂升格);
- `COOL_DOWN_MINUTE`: 移出正式池后的冷却时长,冷却中只能进入 `candidate`;
- `PASS_HYSTERESIS_UP / DOWN`: 进入正式池 / 移出正式池的滞回门槛,防临界反复进出;
- veto 防抖:瞬时否决(当日净流出、涨停家数不足)连续 N 周期命中才真淘汰;中长期否决
  (近3日涨幅≥15% 过热、利空关键词)瞬时直接生效;
- 同池替换:第二名瞬时分数超过当前 leader 不立刻替换,连续 N 周期领先才换人;
- 防倒挂驻留:观察池评分高于防御备选时,需连续 N 周期确认才升格。

Trade-off(注释说明)
-------------------
- 防抖会给「核心主线」确认带来确认延迟:默认后台**每5分钟轮询**时,`STABILIZE_CYCLE=3`
  约等于 3 分钟确认 + 平滑窗口约 5 分钟(资金流均值),合计约 5-10 分钟;冷却 20 分钟。
  牛市主升段方向明确,影响很小;C/D 级震荡市可显著减少假信号导致的无效调仓。
- 若网页端无人访问且未开启后台轮询,周期只在访问/刷新时推进(TTL 兜底)。
- 若希望更激进地跟盘,可调低 STABILIZE_CYCLE / PASS_HYSTERESIS_UP / poll_interval_sec。

输出结构
--------
    output = {
        "raw":       engine.mainline_select() 原始未防抖完整结果(调试/回测用),
        "stable":    {
            "core":     核心主攻(进攻池 leader,需驻留),
            "defensive":防御备选(防御池 leader,需驻留),
            "watch":    观察池(其余正式池成员 + 未达晋升线板块),
            "rejected": 淘汰(准入剔除 + 中长期否决 + 瞬时否决确认),
            "candidate":异动候选(达标但驻留确认中/冷却中/挑战者/瞬时否决暂缓),
            "pass_score": 准入线,
        },
        "stabilizer_enabled": bool,
        "stats":     {"date", "raw_core", "stable_core", "raw_switches", "stable_switches"},
    }

运行模式
--------
- 后台轮询(推荐):`start_polling()` 启动守护线程,按 `decision.mainline.poll_interval_sec`
  秒(默认 60)推进一个周期;平滑窗口与 N 周期确认因此按真实分钟节奏推进,独立于网页访问。
- 访问即算周期:网页/API 每次调用 `get_output()` 返回最近一次轮询结果(≤max_age 秒),
  缓存过期才同步重算;`enable_stabilizer=False` 时直接透传原始结果(兼容历史回测)。
"""

import threading
import time

from app.support import mainline as _ml
from app.decision import engine as _en
from app.review.data import collect_sector_flow as _cf
from app.review.data import collect_sector_flow_5d as _cf5d

# ------------------------------------------------------------------ 模块级状态
# 板块状态机:name -> state。跨快照周期驻留,进程内保存。
#   valid_cycles  连续达标周期数(驻留晋升/保级计数)
#   veto_hits     瞬时否决连续命中周期数(防抖确认)
#   lead_cycles   池内第 1 名的连续周期数(同池替换防抖)
#   inv_cycles    观察池高于防御备选的连续周期数(防倒挂防抖)
#   in_passed     是否在正式池(passed)
#   cool_until    冷却截止时间戳(epoch)
_SECTOR_STATE = {}
# 单日资金流平滑缓冲:[(ts, flows_list), ...],按时间窗口裁剪
_SMOOTH_BUF = []
# 上一周期稳定输出(用于 leader 驻留比较)与当日切换统计
_LAST_OUT = {"stable_core": None, "stable_def": None}
_SWITCH = {
    "date": None, "raw_core": None, "raw_def": None,
    "stable_core": None, "stable_def": None,
    "raw_switches": 0, "stable_switches": 0,
}
# 后台轮询与并发访问共享状态的互斥锁 + 最近一次稳定输出(供 get_output 快速读取)
_LOCK = threading.RLock()
_COMPUTING = threading.Event()   # 单飞标志: 后台轮询计算中, 并发访问直接复用最近输出(不重复抓取)
_LAST_OUTPUT = {"t": 0.0, "data": None}
_poll_thread = None
_poll_stop = threading.Event()


def _mainline_cfg() -> dict:
    from app.support import settings as _st
    return (_st.load().get("decision", {}).get("mainline", {}) or {})


def _today() -> str:
    import datetime
    return datetime.date.today().isoformat()


class _cycle_flow_cache:
    """单次 stabilize() 周期内共享「单日/5日概念资金流」抓取。

    原流水线(mainline_select→sector_scores)与稳定器(_smoothed_flows/_cf5d)各自会抓一次
    同一份资金流(collect_sector_flow / collect_sector_flow_5d 均无缓存、每次实时请求)。
    本上下文将二者统一到同一份样本:raw 与 stable 看到同一时点快照,且每个周期只请求一次,
    交易时段定时轮询时接口负担直接减半。
    其余数据(涨停池/新闻/成分股/板块统计)本身已按日缓存,二次调用命中缓存,无需处理。
    """
    def __enter__(self):
        global _cf, _cf5d
        from app.review import data as _data
        self._orig = (_cf, _cf5d, _data.collect_sector_flow, _data.collect_sector_flow_5d)
        store = {}

        def _memo(fn, key):
            def _w(*a, **kw):
                if key not in store:
                    store[key] = fn(*a, **kw)
                return store[key]
            return _w

        f1, f5 = _memo(_cf, "1d"), _memo(_cf5d, "5d")
        _cf, _cf5d = f1, f5
        # sector_scores 内部是函数级 from app.review.data import ..., 会实时读取模块属性
        _data.collect_sector_flow, _data.collect_sector_flow_5d = f1, f5
        return self

    def __exit__(self, *exc):
        global _cf, _cf5d
        from app.review import data as _data
        _cf, _cf5d = self._orig[0], self._orig[1]
        _data.collect_sector_flow, _data.collect_sector_flow_5d = self._orig[2], self._orig[3]


# ------------------------------------------------------------------ 平滑
def _smoothed_flows() -> list:
    """单日资金流滚动窗口均值。窗口=0 时关闭平滑直接返回实时值;5日表不参与。"""
    cfg = _mainline_cfg()
    window = int(cfg.get("intraday_smooth_window", 25) or 0)
    fresh = _cf()
    if not fresh:
        return fresh
    if window <= 0:
        return fresh
    now = time.time()
    _SMOOTH_BUF.append((now, fresh))
    cutoff = now - window * 60
    _SMOOTH_BUF[:] = [(t, f) for t, f in _SMOOTH_BUF if t >= cutoff]
    # 快照样本过稀(如低频调用)时,保留最近两条做轻平滑,避免单点脉冲直接翻牌
    if len(_SMOOTH_BUF) == 1:
        _SMOOTH_BUF.insert(0, (now - 1.0, fresh))
    names = set()
    for _, fl in _SMOOTH_BUF:
        for f in fl:
            names.add(f["industry"])
    out = []
    for name in names:
        rows = [f for _, fl in _SMOOTH_BUF for f in fl if f["industry"] == name]
        if not rows:
            continue
        agg = {"industry": name,
               "leader": rows[-1].get("leader", ""),
               "leader_pct": rows[-1].get("leader_pct", 0.0)}
        for k in ("net_yi", "inflow_yi", "outflow_yi", "pct_chg", "num"):
            vals = [r.get(k, 0) or 0 for r in rows]
            agg[k] = round(sum(vals) / len(vals), 4)
        out.append(agg)
    return out


# ------------------------------------------------------------------ 状态机工具
def _new_state() -> dict:
    return {"valid_cycles": 0, "veto_hits": 0, "lead_cycles": 0,
            "inv_cycles": 0, "in_passed": False, "cool_until": 0.0,
            # 第五轮:梯队变差确认(连续N周期)状态
            "ladder_score": None, "ladder_bad": 0, "ladder_hold": False}


def _cooling(st, now) -> bool:
    return st.get("cool_until", 0.0) > now


def _cool_remain(st, now) -> int:
    return int(max(0, st.get("cool_until", 0.0) - now) // 60)


def _mark_removed(st, now, cool_min: int) -> None:
    st["cool_until"] = now + cool_min * 60
    st["in_passed"] = False
    st["valid_cycles"] = 0
    st["veto_hits"] = 0
    st["ladder_score"] = None
    st["ladder_bad"] = 0
    st["ladder_hold"] = False


def _log_fields(st, now, trigger: str) -> dict:
    """防抖日志字段:is_stable_result / continue_valid_cycle / cool_down_remain / hysteresis_trigger"""
    return {"is_stable_result": True,
            "continue_valid_cycle": st.get("valid_cycles", 0),
            "cool_down_remain": _cool_remain(st, now),
            "hysteresis_trigger": trigger}


def _stamp(item, st, now, trigger: str) -> None:
    item["is_stable_result"] = True
    item["continue_valid_cycle"] = st.get("valid_cycles", 0)
    item["cool_down_remain"] = _cool_remain(st, now)
    item["hysteresis_trigger"] = trigger


# ---------------------------------------------------------------- 第五轮:梯队变差确认
def _ladder_hold(r, st, cfg, dcfg) -> dict:
    """盘中炸板导致梯队变差不立即生效:连续 N 个快照周期确认后才按新梯队计分。

    只对已在正式池(in_passed)的板块生效(驻留校验);确认期内按持有梯队回补分数,
    梯队恢复则清零计数。返回(可能被替换的)行。
    """
    if r.get("ladder_score") is None or not st.get("in_passed"):
        return r
    lcfg = (cfg.get("extend_factor") or {}).get("ladder") or {}
    N = int(lcfg.get("drop_confirm", cfg.get("STABILIZE_CYCLE", 3)))
    drop_delta = float(lcfg.get("drop_delta", 0.25))
    if N <= 0 or drop_delta <= 0:
        return r
    new = float(r.get("ladder_score") or 0.0)
    held = st.get("ladder_score")
    if held is None:
        st["ladder_score"] = new
        return r
    drop = float(held) - new
    if drop >= drop_delta:
        st["ladder_bad"] = st.get("ladder_bad", 0) + 1
        if st["ladder_bad"] >= N:
            st["ladder_bad"] = 0
            st["ladder_score"] = new
            st["ladder_hold"] = False
            return r
        # 确认期内:score 回补 ladder 降幅(避免瞬时炸板直接掉分出池)
        from app.support import settings as _st
        w_trend = float((_st.load().get("score_weights") or {}).get("trend", 30))
        ladder_w = float(lcfg.get("ladder_w", 0.2))
        r = dict(r)
        r["score"] = round(float(r["score"]) + w_trend * ladder_w * drop, 2)
        r["ladder_score"] = float(held)
        r["_ladder_note"] = (f"梯队变差确认中(炸板未连续 {N} 个周期确认),"
                             f"按持有梯队 {held:.2f} 计分")
        st["ladder_hold"] = True
        return r
    st["ladder_bad"] = 0
    st["ladder_score"] = new
    st["ladder_hold"] = False
    return r


# ------------------------------------------------------------------ 条目构造
def _mk_item(r, stats, level, reasons, pool=None, extra=None) -> dict:
    it = {"name": r["industry"], "score": r["score"], "pct_chg": r["pct_chg"],
          "net_yi": r["net_yi"], "zt_count": r["zt_count"], "leader": r.get("leader", ""),
          "news_hits": r.get("news_hits", 0), "stats": stats or {},
          "rate_1d": r.get("rate_1d"), "fund_rank_1d": r.get("fund_rank_1d"),
          "fund_status": r.get("fund_status"), "rate_5d": r.get("rate_5d"),
          "fund_rank_5d": r.get("fund_rank_5d"),
          "breakdown": r.get("breakdown") or {}, "level": level,
          "reasons": list(reasons)}
    if r.get("ladder_score") is not None:      # 第五轮:梯队标签(复盘展示)
        it["ladder_score"] = r["ladder_score"]
        it["ladder_tag"] = r.get("ladder_tag")
        it["size_bias"] = r.get("size_bias", 0)
    note = r.get("_ladder_note")               # 梯队变差确认中的计分说明
    if note:
        it["reasons"] = it["reasons"] + [note]
    if pool:
        it["pool"] = pool
    if extra:
        it.update(extra)
    return it


def _mk_rejected(r, stats, reasons) -> dict:
    it = {"name": r["industry"], "score": r.get("score", 0),
          "pct_chg": r.get("pct_chg", 0), "net_yi": r.get("net_yi", 0),
          "zt_count": r.get("zt_count", 0), "leader": r.get("leader", ""),
          "stats": stats or {}, "level": "rejected", "reasons": list(reasons),
          "breakdown": r.get("breakdown") or {}}
    if r.get("ladder_score") is not None:
        it["ladder_tag"] = r.get("ladder_tag")
        it["size_bias"] = r.get("size_bias", 0)
    return it


# ------------------------------------------------------------------ 核心稳定逻辑
def _pick_leader(pool_rows, prev_name, label, N, now, margin: float = 0.0):
    """池内 leader 选任(同池替换防抖)。

    返回 (leader_item 或 None, challenger_item 或 None)。
    - 无前任(初始建仓)或前任已出池:池内 resident 第 1 名直接接任
      (它已通过驻留 valid_cycles>=N 的确认,无需再等);
    - 前任仍在池且达标:保持,除非新人连续 lead_cycles>=N 周期领先且
      分数超过前任至少 margin 分(3.3 阶段化超越幅度,退潮更高/主升更低)才替换;
    - 新人驻留不足或未满领先周期 → 返回 None 及该挑战者(进入 candidate)。
    """
    if not pool_rows:
        return None, None
    top = pool_rows[0]
    st = _SECTOR_STATE[top["name"]]
    st["lead_cycles"] = st.get("lead_cycles", 0) + 1
    for other in pool_rows[1:]:
        _SECTOR_STATE[other["name"]]["lead_cycles"] = 0
    if prev_name:
        prev = next((p for p in pool_rows if p["name"] == prev_name), None)
        if prev is not None:
            if prev["name"] == top["name"]:
                return prev, None
            # 换人防抖:新人须连续 N 周期领先且分数超前任 margin 分才替换
            if st["lead_cycles"] >= N and top["score"] > prev["score"] + margin:
                return top, prev
            return prev, top
    # 无前任或前任已出池:resident 第 1 名直接接任(驻留已提供 N 周期确认)
    return top, None


def _confidence(st: dict, score: float, up: float, down: float, N: int, ccfg: dict) -> float:
    """稳定器置信度(0-1, 3.3): 驻留周期进度 + 相对保级线的安全边际。

    置信度 = base + cycle_w×min(1, valid_cycles/N) + margin_w×min(1, (score-down)/(up-down))。
    """
    cycles = st.get("valid_cycles", 0)
    c = float(ccfg.get("base", 0.3))
    c += float(ccfg.get("cycle_w", 0.4)) * min(1.0, cycles / max(1, N))
    span = max(up - down, 0.1)
    c += float(ccfg.get("margin_w", 0.3)) * min(1.0, max(0.0, (score - down) / span))
    return round(max(0.0, min(1.0, c)), 2)


def _build_stable(cfg: dict) -> dict:
    dcfg = _en._cfg()
    N = int(cfg.get("STABILIZE_CYCLE", 3))
    cool_min = int(cfg.get("COOL_DOWN_MINUTE", 20))
    up = float(cfg.get("PASS_HYSTERESIS_UP", 62.0))
    down = float(cfg.get("PASS_HYSTERESIS_DOWN", 58.0))
    pass_score = float(cfg.get("pass_score", 60.0))
    watch_n = int(cfg.get("watch_n", 3))
    # 阶段联动: 退潮加长驻留周期/提高准入, 主升适度放宽灵敏度; 3.3 超越幅度阶段化
    margin = 5.0
    try:
        from app.decision.engine import phase_cfg
        _p = phase_cfg()
        N = max(1, int(N * _p.get("stab_cycle_adj", 1.0)))
        pass_score = float(_p["admission"])
        margin = float(_p.get("lead_margin", 5.0))
    except Exception:  # noqa: BLE001
        pass
    now = time.time()
    # 第五轮:全局风格偏转标签(复盘展示)+ 3.4 风格分数微调输入
    ext_on = bool(cfg.get("enable_extend_factor"))
    style = _ml.market_style_bias() if ext_on else None
    style_tag = (style or {}).get("tag", "") if ext_on else ""

    # 平滑单日资金 + 5日表原样(稳定器不参与 5 日平滑)
    flows = _smoothed_flows()
    flows_5d = _cf5d()
    rows = _ml.sector_scores(
        use_cache=True, flows=flows, flows_5d=flows_5d,
        rank_delta_thresh=cfg.get("rank_delta_thresh"),
        weaken_news_on_no_5d_money=cfg.get("weaken_news_on_no_5d_money", True))
    if not rows:
        # 6.1 主线数据未更新: 回退上一交易日主线并标注(降级不空池)
        try:
            from app.review import archive
            _prev = archive.prev_day(_today())
            _pst = (_prev or {}).get("stable") or {}
            _core_n = _pst.get("core")
            _def_n = _pst.get("defensive")
            _core = {"name": _core_n} if _core_n else None
            _defen = {"name": _def_n} if _def_n else None
            for _it in (_core, _defen):
                if _it:
                    _it.update(score=0, pct_chg=0, net_yi=0, zt_count=0, leader="",
                               reasons=["主线未更新,复用上一交易日主线"], is_stable_result=True,
                               degraded=True, degrade_reason="主线未更新(数据源异常)")
            if _core or _defen:
                return {"core": _core, "defensive": _defen, "watch": [], "rejected": [],
                        "candidate": [], "pass_score": pass_score, "degraded": True}
        except Exception:  # noqa: BLE001
            pass
        return {"core": None, "defensive": None, "watch": [], "rejected": [],
                "candidate": [], "pass_score": pass_score, "degraded": True}

    zt_pool = _ml._zt_pool()
    zt_available = len(zt_pool) > 0
    names = [r["industry"] for r in rows if r.get("level") != "rejected"]
    stats_map = _en._sector_stats_many(names) if names else {}

    passed, rejected, candidate, low = [], [], [], []
    for r in rows:
        name = r["industry"]
        st = _SECTOR_STATE.setdefault(name, _new_state())
        pool = _en._sector_pool(name)
        if r.get("level") == "rejected":
            st["valid_cycles"] = 0
            rejected.append(_mk_rejected(r, stats_map.get(name), [r.get("reject_reason", "")]))
            continue
        stats = stats_map.get(name) or {}
        # 3.1 真否决(利空关键词/数据严重缺失)瞬时生效
        banned, hard = _en._veto(name, r, dcfg, stats, zt_available=zt_available)
        if banned:
            _mark_removed(st, now, cool_min)
            rejected.append(_mk_rejected(r, stats, hard))
            continue
        # 3.1 分级扣分(软性): 净流出/涨停不足/过热 → 扣分,不否决(替代瞬时否决防抖)
        penalty, pen_reasons = _en._veto_penalty(r, dcfg, stats, zt_available=zt_available)
        # 3.4 风格偏转分数微调(与当前大小盘风格对齐/背离)
        s_adj = _en._style_score_adj(style, r.get("size_bias", 0)) if ext_on else 0.0
        # 3.2 准入线动态调整(板块历史分位+波动率)
        adj = _en._sector_admission_adj(name, r["score"])
        # 第五轮:梯队变差确认(盘中炸板不立即生效,连续 N 周期才反映到分数)
        r = _ladder_hold(r, st, cfg, dcfg)
        score = round(r["score"] - penalty + s_adj, 2)
        up_eff = round(up + adj, 1)
        down_eff = round(down + adj, 1)
        _extra = {}
        if penalty:
            _extra["veto_penalty"] = penalty
        if s_adj:
            _extra["style_adj"] = s_adj
        if adj:
            _extra["admission_adj"] = adj
        if st.get("in_passed"):
            # 已在正式池:滞回保级(保级线随板块动态准入调整)
            if score < down_eff:
                _mark_removed(st, now, cool_min)
                st["in_passed"] = False
                st["valid_cycles"] = 0
                it = _mk_item(r, stats, "candidate",
                              [f"分数 {score:.2f} 低于保级线 {down_eff:.0f}"
                               + (f"(板块动态{adj:+.1f})" if adj else "")
                               + f",移出正式池,冷却 {cool_min} 分钟"] + pen_reasons,
                              pool=pool, extra=_log_fields(st, now, "hysteresis_down"))
                it["score"] = score
                it.update(_extra)
                candidate.append(it)
                continue
            st["valid_cycles"] = st.get("valid_cycles", 0) + 1
            it = _mk_item(r, stats, "passed", _en._pass_reasons(r, stats) + pen_reasons, pool=pool)
            it["score"] = score
            it.update(_extra)
            passed.append(it)
        else:
            # 新板块:进入正式池需过晋升线(动态) + 驻留
            if score >= up_eff:
                st["valid_cycles"] = st.get("valid_cycles", 0) + 1
                if st["valid_cycles"] >= N:
                    if _cooling(st, now):
                        it = _mk_item(r, stats, "candidate",
                                      [f"冷却中,剩余 {_cool_remain(st, now)} 分钟,仅作候选"],
                                      pool=pool, extra=_log_fields(st, now, "cool_down"))
                        it["score"] = score
                        it.update(_extra)
                        candidate.append(it)
                    else:
                        st["in_passed"] = True
                        # 第五轮:入池即初始化梯队跟踪基准(后续周期梯队变差需连续N周期确认)
                        if r.get("ladder_score") is not None:
                            st["ladder_score"] = float(r["ladder_score"])
                        it = _mk_item(r, stats, "passed", _en._pass_reasons(r, stats) + pen_reasons, pool=pool)
                        it["score"] = score
                        it.update(_extra)
                        passed.append(it)
                else:
                    it = _mk_item(r, stats, "candidate",
                                  [f"驻留确认中({st['valid_cycles']}/{N} 个快照周期)"],
                                  pool=pool, extra=_log_fields(st, now, "residency"))
                    it["score"] = score
                    it.update(_extra)
                    candidate.append(it)
            else:
                st["valid_cycles"] = 0
                if score >= down_eff:
                    it = _mk_item(r, stats, "watch",
                                  [f"综合评分 {score:.2f} 分,低于晋升线 {up_eff:.0f}"
                                   + (f"(板块动态{adj:+.1f})" if adj else "") + ",仅跟踪"],
                                  pool=pool)
                    it["score"] = score
                    it.update(_extra)
                    low.append(it)

    # ---- 池内分级(禁止跨池对比) + 同池替换防抖(3.3 阶段化超越幅度)
    aggressive = sorted([p for p in passed if p.get("pool") == "aggressive"],
                        key=lambda x: -x["score"])
    defensive_pool = sorted([p for p in passed if p.get("pool") == "defensive"],
                            key=lambda x: -x["score"])
    core, core_chal = _pick_leader(aggressive, _LAST_OUT.get("stable_core"), "core", N, now, margin)
    defen, def_chal = _pick_leader(defensive_pool, _LAST_OUT.get("stable_def"), "defensive", N, now, margin)
    if core:
        core["level"] = "core"
        core["reasons"] = (core.get("reasons") or []) + ["进攻属性池第 1 名(核心主攻)"]
    if defen:
        defen["level"] = "defensive"
        defen["reasons"] = (defen.get("reasons") or []) + ["防御属性池第 1 名(备选方向)"]

    # ---- 观察池:正式池内其余成员 + 未达晋升线板块
    chal_names = {ch["name"] for ch in (core_chal, def_chal) if ch}
    core_name = core["name"] if core else None
    def_name = defen["name"] if defen else None
    watch_candidates = [p for p in passed
                        if p["name"] not in (core_name, def_name) and p["name"] not in chal_names]
    watch_candidates.sort(key=lambda x: -x["score"])

    # ---- 防倒挂:观察池评分高于防御备选,连续 N 周期确认才升格
    mcheck = dcfg.get("mainline_check", {})
    if mcheck.get("enforce", True) and defen and watch_candidates:
        for w in watch_candidates:
            st = _SECTOR_STATE[w["name"]]
            if w["score"] > defen["score"]:
                st["inv_cycles"] = st.get("inv_cycles", 0) + 1
            else:
                st["inv_cycles"] = 0
            if st.get("inv_cycles", 0) >= N and not _cooling(st, now):
                w["level"] = "defensive"
                w["reasons"] = [r for r in w.get("reasons", [])
                                if "属性" not in r and "备选" not in r]
                w["reasons"].append(f"观察池连续 {N} 个周期评分高于防御备选,升格防御备选")
                defen = w
                def_name = defen["name"]
                watch_candidates = [p for p in passed
                                    if p["name"] not in (core_name, def_name) and p["name"] not in chal_names]
                watch_candidates.sort(key=lambda x: -x["score"])
                break

    watch = watch_candidates[:watch_n]
    for w in watch:
        w["level"] = "watch"
    watch += low[:max(0, watch_n - len(watch))]

    # ---- 3.3 稳定器置信度(0-1): 驻留周期进度 + 相对保级线安全边际
    ccfg = cfg.get("confidence", {})
    for it in ([core] if core else []) + ([defen] if defen else []) + watch:
        _stt = _SECTOR_STATE.get(it["name"], _new_state())
        _adj = it.get("admission_adj", 0.0)
        it["confidence"] = _confidence(_stt, it["score"], up + _adj, down + _adj, N, ccfg)

    # ---- 挑战者(同池替换未遂)并入 candidate
    for ch in (core_chal, def_chal):
        if ch:
            ch["level"] = "candidate"
            ch["reasons"] = (ch.get("reasons") or []) + [f"同池替换需连续 {N} 个周期领先,当前为挑战者"]
            candidate.append(ch)

    # ---- 性价比维度 + 防抖日志字段
    vcfg = dcfg.get("value", {})
    if vcfg.get("enabled", True):
        shown = [x for x in ([core] if core else []) + ([defen] if defen else []) + watch if x]
        for p in shown:
            st = p.get("stats") or {}
            p["pos_rating"] = _en._pos_rating(st, vcfg)
            p["profit_ratio"] = _en._profit_ratio(st, st.get("price"))
            p["rr_label"] = _en._rr_label(p["profit_ratio"])
            p["priority"] = _en._priority(p.get("level", "watch"), p["profit_ratio"])
            p["reasons"] = p.get("reasons") or []
            if vcfg.get("note", True) and p.get("level") != "rejected":
                p["reasons"] = [_en._value_notes(p, vcfg)] + p["reasons"]
    for p in ([core] if core else []) + ([defen] if defen else []) + watch:
        _stamp(p, _SECTOR_STATE.get(p["name"], _new_state()), now,
               "resident" if p["level"] in ("core", "defensive") else "watch")

    _LAST_OUT["stable_core"] = core["name"] if core else None
    _LAST_OUT["stable_def"] = defen["name"] if defen else None

    if style_tag:      # 第五轮:风格标签透传到全部稳定条目(复盘展示)
        for it in ([core] if core else []) + ([defen] if defen else []) \
                  + watch + rejected + candidate:
            it["market_style_tag"] = style_tag

    return {"core": core, "defensive": defen, "watch": watch,
            "rejected": rejected[:max(watch_n, 5)],
            "candidate": candidate[:12],
            "pass_score": pass_score}


# ------------------------------------------------------------------ 切换统计
def _update_switches(raw: dict, stable: dict) -> None:
    day = _today()
    if day != _SWITCH["date"]:
        _SWITCH.update(date=day, raw_core=None, raw_def=None,
                       stable_core=None, stable_def=None,
                       raw_switches=0, stable_switches=0)
    r_core = raw.get("core") and raw["core"]["name"]
    r_def = raw.get("defensive") and raw["defensive"]["name"]
    s_core = stable.get("core") and stable["core"]["name"]
    s_def = stable.get("defensive") and stable["defensive"]["name"]
    if _SWITCH["raw_core"] is not None and r_core != _SWITCH["raw_core"]:
        _SWITCH["raw_switches"] += 1
    if _SWITCH["raw_def"] is not None and r_def != _SWITCH["raw_def"]:
        _SWITCH["raw_switches"] += 1
    if _SWITCH["stable_core"] is not None and s_core != _SWITCH["stable_core"]:
        _SWITCH["stable_switches"] += 1
    if _SWITCH["stable_def"] is not None and s_def != _SWITCH["stable_def"]:
        _SWITCH["stable_switches"] += 1
    _SWITCH.update(raw_core=r_core, raw_def=r_def, stable_core=s_core, stable_def=s_def)


def _sw_stats() -> dict:
    return {"date": _SWITCH["date"],
            "raw_core": _SWITCH["raw_core"], "stable_core": _SWITCH["stable_core"],
            "raw_switches": _SWITCH["raw_switches"], "stable_switches": _SWITCH["stable_switches"]}


# ------------------------------------------------------------------ 对外入口
def stabilize() -> dict:
    """稳定器对外入口(线程安全, 推进一个快照周期)。

    - `enable_stabilizer=False`:直接透传原始流水线结果(兼容历史回测);
    - 单飞: 后台轮询计算中, 并发 Web 访问直接复用最近输出(不重复抓取/不阻塞等待);
    - 否则:原始结果进 `raw`,稳定结果进 `stable`;结果写入 `_LAST_OUTPUT`。
    """
    with _LOCK:
        if _COMPUTING.is_set() and _LAST_OUTPUT["data"] is not None:
            return _LAST_OUTPUT["data"]        # 另一线程正在计算, 复用最近输出(单飞)
        _COMPUTING.set()
        try:
            cfg = _mainline_cfg()
            if not cfg.get("enable_stabilizer", True):
                raw = _en.mainline_select()
                out = {"raw": raw, "stable": raw, "stabilizer_enabled": False,
                       "stats": _sw_stats()}
            else:
                with _cycle_flow_cache():
                    raw = _en.mainline_select()
                    stable = _build_stable(cfg)
                _update_switches(raw, stable)
                out = {"raw": raw, "stable": stable, "stabilizer_enabled": True,
                       "stats": _sw_stats()}
            _LAST_OUTPUT["t"] = time.time()
            _LAST_OUTPUT["data"] = out
            return out
        finally:
            _COMPUTING.clear()


def get_output(max_age: float = 30.0) -> dict:
    """返回最近一次稳定输出。

    优先复用后台定时轮询的最近结果,避免每次页面访问都触发完整数据抓取;
    非交易时段(收盘/周末)直接复用最近输出(数据为收盘快照,不再重复抓取)。
    """
    with _LOCK:
        if _LAST_OUTPUT["data"] is not None:
            fresh = time.time() - _LAST_OUTPUT["t"] <= max_age
            if fresh or not _is_trading_time():
                return _LAST_OUTPUT["data"]
    return stabilize()


# ------------------------------------------------------------------ 后台定时轮询
def _is_trading_time(now=None) -> bool:
    """是否处于交易时段(工作日 9:30-11:30 / 13:00-15:00)。非交易时段不推进周期,
    避免对同一份收盘数据反复抓取(状态机本就无变化)。"""
    import datetime as _dt
    now = now or _dt.datetime.now()
    if now.weekday() >= 5:            # 周六日
        return False
    hm = now.hour * 60 + now.minute
    return (9 * 60 + 30) <= hm <= (11 * 60 + 30) or (13 * 60) <= hm <= (15 * 60)


def start_polling(interval_sec: int = None) -> threading.Thread:
    """启动后台守护线程,按 poll_interval_sec 秒推进一个稳定器周期(幂等)。

    仅在交易时段轮询(默认),空闲时线程挂起,不占用接口。返回线程对象。
    """
    global _poll_thread
    if _poll_thread is not None and _poll_thread.is_alive():
        return _poll_thread
    cfg = _mainline_cfg()
    if interval_sec is None:
        interval_sec = int(cfg.get("poll_interval_sec", 300) or 0)
    if interval_sec <= 0:
        return None
    hours_only = cfg.get("poll_trading_hours_only", True)
    _poll_stop.clear()

    def _loop():
        while not _poll_stop.is_set():
            try:
                if (not hours_only) or _is_trading_time():
                    stabilize()
            except Exception as e:  # noqa: BLE001
                # 单次轮询失败不中断线程,保留上次输出,下个周期重试
                print(f"[稳定器] 轮询失败(保留上次输出): {e}")
            _poll_stop.wait(interval_sec)

    _poll_thread = threading.Thread(target=_loop, name="mainline-stabilizer-poll",
                                    daemon=True)
    _poll_thread.start()
    return _poll_thread


def stop_polling() -> None:
    """停止后台轮询线程(测试/进程退出时调用)。"""
    _poll_stop.set()


def reset() -> None:
    """清空进程内状态(测试/回测/开关切换时调用)。"""
    with _LOCK:
        _SECTOR_STATE.clear()
        _SMOOTH_BUF.clear()
        _LAST_OUT.clear()
        _LAST_OUTPUT.update(t=0.0, data=None)
        _SWITCH.update(date=None, raw_core=None, raw_def=None,
                       stable_core=None, stable_def=None,
                       raw_switches=0, stable_switches=0)


if __name__ == "__main__":
    import json
    out = stabilize()
    print(json.dumps(out, ensure_ascii=False, indent=2)[:3000])