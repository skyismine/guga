"""板块标的三档梯队全量优化: 中军龙头(steady) + 情绪龙头(aggressive) + 补涨优选(repair)。

相对原有「情绪=涨幅排序 / 中军=成交额排序」的高重合、单指标脉冲问题,本模块:

1. 候选池分层独立筛选:
   - 稳健池(中军): 板块内流通市值分位前40%(≥60分位) + 20日日均额≥1亿, 聚焦行业核心;
   - 激进池(情绪): 市值分位后60%(<60分位) + 近20日波动率≥板块中位 + 20日日均额≥300万, 聚焦弹性小票。
2. 三档强制去重: 优先级 中军 > 情绪 > 补涨; 每档从「前档用剩」的候选池选拔, 绝不同股重复;
   成分股不足5只时允许部分重合并标注「标的池不足,档位重合」。
3. 数量弹性: ≥15只→中2/情2/补1; 8-14只→1/1/1; 5-7只→1/1/0; <5只→仅中军1。
4. 综合评分(降序): 中军(市值分位40/20日均额30/MA20趋势20/当日额10);
   情绪(连板40/涨幅归一25/量价20/板块联动15); 补涨(滞涨40/承接30/弹性20/趋势10)。
5. 持续性校验(1日滞回): 连续2个交易日进入排名前列才正式入选, 单日脉冲入观察(is_pulse_watch)。
6. 差异化交易参数(随标的输出) + 位置修正(近3日涨幅超阈值下修) + 主线层级联动。
"""
import bisect
import threading
import time

from app.support import settings as _st


# ------------------------------------------------------------------ 固化交易参数
_TIER_PARAMS = {
    "steady":     {"type": "steady",     "position_coef": 1.5, "stop_loss_pct": -0.05,
                   "target_profit_pct": 0.08, "style": "波段操作/分歧低吸"},
    "aggressive": {"type": "aggressive", "position_coef": 0.5, "stop_loss_pct": -0.08,
                   "target_profit_pct": 0.15, "style": "短线交易/快进快出"},
    "repair":     {"type": "repair",     "position_coef": 0.6, "stop_loss_pct": -0.06,
                   "target_profit_pct": 0.10, "style": "短线套利/高低切博弈"},
}
# 近3日累计涨幅下修阈值: 超过即下修评级(高位防回落)
_POS_CUTOFF = {"steady": 0.18, "aggressive": 0.12, "repair": 0.10}

# 1日滞回状态: {(sector, role): {code: 首次上榜日期}}
_PERSIST = {}
_PERSIST_LOCK = threading.Lock()


# ------------------------------------------------------------------ 基础工具(复用现有)
def _hist(code):
    try:
        from app.support.target_match import _cached_hist
        return _cached_hist(str(code).zfill(6))
    except Exception:  # noqa: BLE001
        return None


def _avg_amount20(code):
    try:
        from app.support.target_match import _avg_amount20
        return _avg_amount20(str(code).zfill(6))
    except Exception:  # noqa: BLE001
        return None


def _ret_n(df, n):
    if df is None or len(df) < n + 1 or "close" not in df.columns:
        return None
    c = df["close"].astype(float)
    return float(c.iloc[-1] / c.iloc[-(n + 1)] - 1)


def _vol20(df):
    if df is None or len(df) < 22 or "close" not in df.columns:
        return None
    r = df["close"].astype(float).pct_change().tail(20)
    v = r.std()
    return float(v) if v == v else None


def _ma_last(df, n):
    if df is None or len(df) < n or "close" not in df.columns:
        return None
    return float(df["close"].astype(float).tail(n).mean())


def _pos20(df):
    """近20日价格分位(0~1): 现价在20日高低区间的位置。"""
    if df is None or len(df) < 20 or "close" not in df.columns:
        return None
    c = df["close"].astype(float).tail(20)
    lo, hi = float(c.min()), float(c.max())
    if hi <= lo:
        return 0.5
    return float((c.iloc[-1] - lo) / (hi - lo))


def _vol_ratio(df):
    """当日量能比: 最新成交量 / 前19日均量。"""
    if df is None or len(df) < 20 or "volume" not in df.columns:
        return None
    v = df["volume"].astype(float)
    base = float(v.iloc[:-1].tail(19).mean())
    if base <= 0:
        return None
    return float(v.iloc[-1] / base)


def _sector_ret5(sector_name):
    """板块指数近5日收益(概念指数)。不可用返回 None。"""
    try:
        from app.features.concept_features import _get_concept_close
        idx = _get_concept_close(sector_name)
        if idx is None or len(idx) < 6:
            return None
        import pandas as pd
        c = pd.Series(idx).astype(float)
        return float(c.iloc[-1] / c.iloc[-6] - 1)
    except Exception:  # noqa: BLE001
        return None


def _corr_sector(sector_name, code):
    """个股与板块指数日收益相关性(板块联动性)。"""
    try:
        from app.features.concept_features import _get_concept_close
        idx = _get_concept_close(sector_name)
        df = _hist(code)
        if idx is None or df is None or len(df) < 10:
            return None
        import pandas as pd
        ir = pd.Series(idx).astype(float).pct_change().dropna()
        sr = df["close"].astype(float).pct_change().dropna()
        al = pd.concat([ir, sr], axis=1, join="inner").dropna()
        if len(al) < 5:
            return None
        c = al.iloc[:, 0].corr(al.iloc[:, 1])
        return float(c) if c == c else None
    except Exception:  # noqa: BLE001
        return None


# ------------------------------------------------------------------ 候选池分层
def _mv_pct(fmv, sorted_fmv):
    if not fmv or fmv <= 0 or not sorted_fmv:
        return 0.5
    return bisect.bisect_right(sorted_fmv, fmv) / len(sorted_fmv)


def _split_pools(stocks, spot) -> tuple:
    """按市值/波动率/流动性拆分为 稳健池 与 激进池(独立分层)。

    设计取舍: 全A快照在东财被拦时走新浪兜底,缺少 float_mv/total_mv,
    市值分位降级用「当日成交额」作规模代理(与中军成交属性同向),并在备注标注。
    """
    rated = []
    for s in stocks:
        code = str(s.get("code") or "").zfill(6)
        sp = spot.get(code) or {}
        fmv = (sp.get("float_mv") or s.get("float_mv")
               or sp.get("total_mv") or (sp.get("amount") or 0))   # 市值缺失→当日额代理
        df = _hist(code)
        rated.append({**s, "code": code, "_fmv": float(fmv or 0),
                      "_avg": _avg_amount20(code), "_vol": _vol20(df),
                      "_pos20": _pos20(df), "_vr": _vol_ratio(df),
                      "_ret3": _ret_n(df, 3), "_ret5": _ret_n(df, 5)})
    if not rated:
        return [], []
    fmv_sorted = sorted(r["_fmv"] for r in rated if r["_fmv"] > 0)
    vols = [r["_vol"] for r in rated if r["_vol"] is not None]
    med_vol = sorted(vols)[len(vols) // 2] if vols else None
    steady, aggressive = [], []
    for r in rated:
        pct = _mv_pct(r["_fmv"], fmv_sorted)
        avg = r["_avg"]
        r["_mv_pct"] = pct
        # 流动性/波动率数据缺失时降级放行(全档位容错,不因冷缓存空池)
        liq_ok_steady = (avg is None) or (avg >= 1e8)              # 稳健池: 日均额≥1亿
        liq_ok_agg = (avg is None) or (avg >= 3e6)                 # 激进池: 日均额≥300万
        vol_ok = (r["_vol"] is None) or (med_vol is None) or (r["_vol"] >= med_vol)
        if pct >= 0.6 and liq_ok_steady:
            steady.append(r)
        if pct < 0.6 and liq_ok_agg and vol_ok:
            aggressive.append(r)
    return steady, aggressive


# ------------------------------------------------------------------ 综合评分
def _norm_list(vals):
    """min-max 归一为列表(与输入顺序对齐),全等/空返回 0.5 中性。"""
    vals = list(vals)
    if not vals:
        return []
    lo, hi = min(vals), max(vals)
    if hi <= lo:
        return [0.5] * len(vals)
    return [(v - lo) / (hi - lo) for v in vals]


def _score_steady(pool):
    """中军综合评分: 市值分位40 / 20日均额30 / MA20趋势20 / 当日额10。"""
    nm_cap = _norm_list([r["_mv_pct"] for r in pool])
    nm_avg = _norm_list([(r["_avg"] or 0) for r in pool])
    nm_amt = _norm_list([spot_val(r) for r in pool])
    trend = {}
    for r in pool:
        df = _hist(r["code"])
        t = 0.5
        if df is not None and len(df) >= 25:
            ma = df["close"].astype(float).rolling(20).mean().dropna()
            if len(ma) >= 6 and ma.iloc[-6]:
                slope = ma.iloc[-1] / ma.iloc[-6] - 1
                t = max(0.0, min(1.0, 0.5 + slope * 20))
        trend[r["code"]] = t
    nm_trend = _norm_list([trend[r["code"]] for r in pool])
    out = []
    for i, r in enumerate(pool):
        score = (0.4 * nm_cap[i] + 0.3 * nm_avg[i] + 0.2 * nm_trend[i] + 0.1 * nm_amt[i])
        out.append({"it": r, "score": round(score, 4), "dims": {
            "市值分位": round(r["_mv_pct"], 2), "趋势": round(trend[r["code"]], 2)}})
    return out


def spot_val(r):
    """当日成交额(元)。"""
    return r.get("amount") or 0


def _score_aggressive(pool, sector_name):
    """情绪综合评分: 连板40 / 涨幅归一25 / 量价20 / 板块联动15。"""
    zt_map = {}
    try:
        from app.support import mainline as _ml
        for z in _ml._zt_pool():
            zt_map[str(z.get("code"))] = int(z.get("boards", 1) or 1)
    except Exception:  # noqa: BLE001
        pass
    corr = {}
    for r in pool:
        c = _corr_sector(sector_name, r["code"])
        if c is not None:
            corr[r["code"]] = c
    # 连板归一(按池内最高板, 无板=0)
    boards = [zt_map.get(r["code"], 0) for r in pool]
    nm_board = _norm_list(boards)
    # 涨幅归一: pct/涨跌幅限制(10cm=0.10, 20cm=0.20)
    def _limit(code):
        return 0.20 if code.startswith(("30", "68")) else 0.10
    pctv = []
    for r in pool:
        p = float(r.get("pct_chg") or 0) / _limit(r["code"])
        pctv.append(max(0.0, min(1.0, p)))
    nm_pct = _norm_list(pctv)
    # 量价: 换手率(spot.turnover) + 量能比
    vr_map = []
    for r in pool:
        v = r.get("_vr")
        if v is None:
            v = 1.0
        vr_map.append(max(0.0, min(1.0, v / 3.0)))   # 3倍量封顶
    nm_vr = _norm_list(vr_map)
    out = []
    for i, r in enumerate(pool):
        c = r["code"]
        corr_v = corr.get(c)
        corr_n = max(0.0, min(1.0, (corr_v + 1) / 2)) if corr_v is not None else 0.5
        score = (0.4 * nm_board[i] + 0.25 * nm_pct[i]
                 + 0.2 * nm_vr[i] + 0.15 * corr_n)
        out.append({"it": r, "score": round(score, 4), "dims": {
            "连板": boards[i], "涨幅": round(pctv[i], 2), "量比": round(vr_map[i], 2)}})
    return out


def _score_repair(pool, sector_name):
    """补涨优选综合评分: 滞涨40 / 承接30 / 弹性20 / 趋势10。"""
    idx5 = _sector_ret5(sector_name)
    out = []
    for r in pool:
        df = _hist(r["code"])
        ret5 = r.get("_ret5")
        # 滞涨程度: 近5日超额收益为负 + 位置低(0.3~0.6 最优)
        excess = (ret5 - idx5) if (ret5 is not None and idx5 is not None) else None
        pos = r.get("_pos20")
        lag = 0.0
        if excess is not None and excess < 0:
            lag += 0.5
        if pos is not None and 0.3 <= pos <= 0.6:
            lag += 0.5
        elif pos is not None and pos < 0.3:
            lag += 0.2
        # 承接信号: 缩量回调至均线 + 当日温和放量
        hold = 0.0
        ma10 = _ma_last(df, 10)
        ma20 = _ma_last(df, 20)
        px = float(df["close"].iloc[-1]) if df is not None and len(df) else None
        vr = r.get("_vr")
        if px is not None and ma20:
            near_ma = abs(px - ma20) / ma20 <= 0.03
            if near_ma:
                hold += 0.5
        if px is not None and ma10 and abs(px - ma10) / ma10 <= 0.03:
            hold += 0.3
        if vr is not None and 1.0 < vr < 2.5:      # 温和放量(非脉冲)
            hold += 0.2
        # 弹性: 市值中等(板块内) + 波动率适中
        flex = 0.0
        if 0.3 <= r.get("_mv_pct", 0.5) <= 0.7:
            flex += 0.6
        v = r.get("_vol")
        if v is not None and 0.01 < v < 0.05:
            flex += 0.4
        # 趋势: MA20 向上
        trend = 0.0
        if df is not None and len(df) >= 21:
            c = df["close"].astype(float)
            if float(c.iloc[-1]) >= float(c.tail(20).mean()):
                trend = 1.0
        score = 0.4 * lag + 0.3 * hold + 0.2 * flex + 0.1 * trend
        out.append({"it": r, "score": round(score, 4), "dims": {
            "滞涨": round(lag, 2), "承接": round(hold, 2), "弹性": round(flex, 2)}})
    return out


# ------------------------------------------------------------------ 数量弹性
def _quota(cons_n: int) -> dict:
    if cons_n >= 15:
        return {"steady": 2, "aggressive": 2, "repair": 1}
    if cons_n >= 8:
        return {"steady": 1, "aggressive": 1, "repair": 1}
    if cons_n >= 5:
        return {"steady": 1, "aggressive": 1, "repair": 0}
    return {"steady": 1, "aggressive": 0, "repair": 0}


# ------------------------------------------------------------------ 1日滞回持续性
def _persistence(sector: str, role: str, ranked: list, n: int) -> tuple:
    """1日滞回: 连续2个交易日进入排名前列才正式; 单日脉冲进观察。

    跨日记忆 {code: 最近上榜日期}(不每日清空,否则滞回恒失效):
    - 某 code 在之前任一交易日上过榜 → 今日再上榜视为「连续2日」→ 正式;
    - 今日首日上榜 → 脉冲 → 观察(is_pulse_watch=True);
    - 冷启动(该档首次处理)放宽为直接正式,避免空档。
    返回 (formal, watch, cold)。
    """
    today = time.strftime("%Y-%m-%d")
    key = (sector, role)
    with _PERSIST_LOCK:
        st = _PERSIST.setdefault(key, {})
        cold = not bool(st)
        formal, watch = [], []
        for i, cand in enumerate(ranked[: max(n, 1)], 1):
            cand["it"]["type"] = role
            code = cand["it"]["code"]
            if st.get(code) or cold:
                # 之前任一交易日也在排名前列 → 连续上榜,正式入选
                cand["it"]["is_pulse_watch"] = False
                formal.append(cand)
            else:
                # 今日首日上榜 → 单日脉冲,进观察
                cand["it"]["is_pulse_watch"] = True
                watch.append(cand)
            st[code] = today
        # 兜底: 全部脉冲时保留榜首为正式,避免空档
        if not formal and ranked:
            top = ranked[0]
            top["it"]["is_pulse_watch"] = False
            formal = [top]
            watch = [c for c in ranked[1:] if c["it"].get("is_pulse_watch")]
    return formal[:n], watch[:3], cold


# ------------------------------------------------------------------ 主线层级联动
def _tier_quota_by_level(sector_level: str, cons_n: int) -> dict:
    """按主线层级联动适配。核心/防御三档配齐; 发酵=情绪+补涨(符合则), 中军不强制; 观察=仅观察。"""
    base = _quota(cons_n)
    if sector_level in ("watch", "candidate", "rejected"):
        return {"steady": 0, "aggressive": 0, "repair": 0}
    if sector_level == "branch":
        return {"steady": 0, "aggressive": max(1, base["aggressive"]), "repair": base["repair"]}
    return base


# ------------------------------------------------------------------ 4.1 候选池规模(每角色初始候选数)
def _cap_candidates(pool: list, role: str, n: int) -> list:
    """按角色快速预筛, 候选池保留 top-n(默认5)再进入综合评分, 降低噪声与接口成本。"""
    if n <= 0 or len(pool) <= n:
        return pool
    if role == "steady":
        key = lambda r: -(r.get("amount") or 0)
    elif role == "aggressive":
        key = lambda r: -((float(r.get("pct_chg") or 0) * 3 + float(r.get("amount") or 0) / 1e7))
    else:
        key = lambda r: -(r.get("amount") or 0)
    return sorted(pool, key=key)[:n]


# ------------------------------------------------------------------ 位置修正
def _position_adjust(it: dict, role: str) -> None:
    """近3日累计涨幅超阈值 → 下修评级与仓位系数, 高位防回落优先。"""
    ret3 = it.get("_ret3")
    cutoff = _POS_CUTOFF[role]
    if ret3 is not None and ret3 >= cutoff:
        params = _TIER_PARAMS[role]
        it["position_coef"] = round(params["position_coef"] * 0.5, 2)
        it["pos_adjusted"] = f"近3日累计涨幅 {ret3 * 100:.1f}% ≥ {cutoff * 100:.0f}%,高位下修,防回落优先"
    if role == "repair":
        # 启动后涨幅超10% 即转情绪龙头评级标准(高位收紧)
        ret1 = it.get("_ret1")
        if ret1 is not None and ret1 >= 0.10:
            it["pos_adjusted"] = (it.get("pos_adjusted") or "") + "; 启动后单日涨幅超10%,转情绪龙头标准收紧风控"


# ------------------------------------------------------------------ 主入口
def select_three_tiers(sector_name: str, sector_level: str, sector_status: str,
                       spot: dict, stocks: list) -> dict:
    """三档梯队选股主入口,返回 {steady, aggressive, repair, watch, degraded, quota}。

    每档为候选 dict(含 code/name/pct_chg/amount/float_mv + rank_score + 交易参数 + is_pulse_watch)。
    无符合标的时对应档位为空(不硬凑), 供上层渲染。
    """
    steady_pool, aggressive_pool = _split_pools(stocks, spot)
    # 4.1 候选池规模: 每角色初始候选 n 只(默认5)再进入综合评分
    try:
        _pool_n = int((_st.load().get("target_match") or {}).get("candidate_pool_size", 5) or 5)
    except Exception:  # noqa: BLE001
        _pool_n = 5
    steady_pool = _cap_candidates(steady_pool, "steady", _pool_n)
    aggressive_pool = _cap_candidates(aggressive_pool, "aggressive", _pool_n)
    cons_n = len(stocks)
    quota = _tier_quota_by_level(sector_status if sector_status in ("core", "defensive", "branch")
                                 else sector_level, cons_n)
    if not stocks or cons_n < 5:
        quota = {"steady": 1, "aggressive": 0, "repair": 0}
        # 成分不足5只: 允许降级重合
        pool = sorted(stocks, key=lambda s: -(s.get("amount") or 0))
        if pool:
            steady = [pool[0]]
        else:
            steady = []
        return {"steady": steady, "aggressive": [], "repair": [],
                "watch": [], "degraded": True, "quota": quota}

    result = {"steady": [], "aggressive": [], "repair": [], "watch": [], "degraded": False,
              "quota": quota}

    # 1) 中军(优先确定, 从稳健池)
    used = set()
    if quota["steady"] and steady_pool:
        scored = sorted(_score_steady(steady_pool), key=lambda x: -x["score"])
        formal, watch, cold = _persistence(sector_name, "steady", scored, quota["steady"])
        for c in formal:
            it = c["it"]
            it["rank_score"] = c["score"]
            it.update(_TIER_PARAMS["steady"])
            _position_adjust(it, "steady")
            used.add(it["code"])
            result["steady"].append(it)
        result["watch"].extend(w for w in watch)
    if not result["steady"]:
        result["degraded"] = True

    # 2) 情绪(从激进池, 剔除已用)
    if quota["aggressive"] and aggressive_pool:
        rest = [r for r in aggressive_pool if r["code"] not in used]
        scored = sorted(_score_aggressive(rest, sector_name), key=lambda x: -x["score"])
        formal, watch, _ = _persistence(sector_name, "aggressive", scored, quota["aggressive"])
        for c in formal:
            it = c["it"]
            it["rank_score"] = c["score"]
            it.update(_TIER_PARAMS["aggressive"])
            _position_adjust(it, "aggressive")
            used.add(it["code"])
            result["aggressive"].append(it)
        result["watch"].extend(w for w in watch if w["it"]["code"] not in {x["it"]["code"] for x in result["watch"]})

    # 3) 补涨优选(从前两档之外的全体剩余候选)
    if quota["repair"]:
        rest = [r for r in stocks if r["code"] not in used]
        if len(rest) >= 3:
            scored = sorted(_score_repair(rest, sector_name), key=lambda x: -x["score"])
            best = scored[0]
            if best["score"] >= 0.4:                 # 综合得分阈值, 不硬凑
                it = best["it"]
                it["rank_score"] = best["score"]
                it.update(_TIER_PARAMS["repair"])
                _position_adjust(it, "repair")
                result["repair"].append(it)

    return result
