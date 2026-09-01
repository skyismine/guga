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
import json
import os
import time

from app import config
from app.support import settings as _st
from app.support import mainline as _ml
from app.support.portfolio import _one
from app.data import dal
from app.features.market_features import market_snapshot, fear_greed_label

# ---------------------------------------------------------------- 工具
def _cfg():
    return _st.load().get("decision", {})


def _today() -> str:
    return dt.date.today().strftime("%Y-%m-%d")


_sector_stats_cache = {}  # name -> (date, result)

# 盘中量能采样存档:{date: {"HH:MM": 两市累计成交额(亿)}} —— 「相同时段」量能比的数据基础
_VOL_ARCH_FILE = os.path.join(config.DATA_DIR, "intraday_amount.json")
_VR_SMOOTH = []      # 大盘量能比平滑窗口 [(ts, vr)](EWMA 加权,近期权重更高)
_SEC_VR_SMOOTH = {}  # 板块量能比5分钟滚动窗口 {name: [(ts, vr)]}
_SEC_ADJ_LAST = {}   # 板块量能修正滞回记忆 {name: (vr_used, delta)}
_VR_TREND = {"dir": None, "count": 0}      # 大盘量能趋势(连续放量/缩量)

# 2.1 环境自适应权重: 近5组权重均值平滑(5日均值口径)
_WEIGHT_HIST = []    # [{mood,breadth,zt,vp,trend}...]
_FG_ARCH_FILE = os.path.join(config.DATA_DIR, "fg_history.json")    # {date: 恐贪} 情绪波动判定

# 2.4 评级滞回: 升级/降级阈值偏置、切换确认、单日限幅、历史存档
_GRADE_ARCH_FILE = os.path.join(config.DATA_DIR, "grade_history.json")  # {date: grade}
_GRADE_UP_BIAS, _GRADE_DOWN_BIAS = 5.0, 5.0   # 升级需更严(score+5)/降级需更松(score-5)
_GRADE_CONFIRM, _GRADE_MAX_STEP = 2, 1        # 切换确认次数(盘中5分钟窗口) / 单日最多变1级
_GRADE_ORDER = {"A": 3, "B": 2, "C": 1, "D": 0}
_GRADE_PENDING = {"grade": None, "count": 0, "ts": 0.0}


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
        from app.features.concept_features import _get_concept_daily
        df = _get_concept_daily(name)
        close = df["close"]
        if close is not None and len(close) >= 4:
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
                # 板块量能比:当日成交量 / 近5日均量(>1 放量,<1 缩量)
                if "volume" in df.columns and len(df["volume"]) >= 6:
                    v = df["volume"].astype(float)
                    if float(v.tail(5).mean()) > 0:
                        out["volume_ratio"] = float(v.iloc[-1] / v.tail(5).mean())
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


def _sector_volume_adj(name: str, vr_raw: float, pct_chg, gain3, grade: str):
    """板块量能修正 delta(固化规则),返回 (delta, 平滑后量能比)。

    - 涨跌方向绑定(修复放量下跌加分漏洞):上涨放量正修(0~+2)/缩量小负修(0~-1);
      下跌放量大幅负修(0~-3,出货信号)/缩量小正修(0~+0.6,抛压衰竭);
    - 滞回:平滑后量能比波动 <0.05 沿用上次 delta,过滤盘中微小毛刺;
    - 过热保护:近3日累计涨幅 >=15% 仅保留负向修正,防高位出货板块被推升排名;
    - 大盘环境联动:A/B/C-D 级修正幅度分别 100%/80%/50%(弱市降低量能噪声干扰);
    - 输入量能比先做5分钟滚动窗口均值平滑,原始瞬时值不参与打分。
    """
    _VR_LO, _VR_HI, _MAX = 0.5, 2.0, 2.0   # 量能比截断区间与最大修正幅度(分)
    _UP_WEAK, _DN_HEAVY, _DN_MILD = 0.5, 1.5, 0.3  # 方向系数:涨缩量/跌放量/跌缩量
    _HYST, _HOT, _SMOOTH_SEC = 0.05, 0.15, 300     # 滞回阈值/过热涨幅线/平滑窗口
    _SCALE = {"A": 1.0, "B": 0.8, "C": 0.5, "D": 0.5}
    now = time.time()
    buf = [x for x in _SEC_VR_SMOOTH.get(name, []) if now - x[0] <= _SMOOTH_SEC]
    buf.append((now, float(vr_raw)))
    _SEC_VR_SMOOTH[name] = buf[-64:]
    vr = sum(v for _, v in buf) / len(buf)
    vr_c = max(_VR_LO, min(vr, _VR_HI))
    up = (pct_chg or 0.0) > 0
    if up:
        # 上涨:放量正向修正,缩量小幅负向修正(无量上涨不可持续)
        base = (_MAX * (vr_c - 1.0) / (_VR_HI - 1.0) if vr_c >= 1.0
                else -_UP_WEAK * _MAX * (1.0 - vr_c) / (1.0 - _VR_LO))
    else:
        # 下跌:放量大幅负向修正(放量出货),缩量小幅正向修正(抛压衰竭)
        base = (-_DN_HEAVY * _MAX * (vr_c - 1.0) / (_VR_HI - 1.0) if vr_c >= 1.0
                else _DN_MILD * _MAX * (1.0 - vr_c) / (1.0 - _VR_LO))
    if (gain3 or 0.0) >= _HOT:
        base = min(base, 0.0)  # 过热保护:只许扣分不许加分
    delta = base * _SCALE.get(grade or "B", 0.8)
    last = _SEC_ADJ_LAST.get(name)
    if last and abs(vr - last[0]) < _HYST:
        delta = last[1]        # 滞回:波动太小沿用上次修正
    else:
        _SEC_ADJ_LAST[name] = (vr, delta)
    return round(delta, 2), round(vr, 3)


# ---------------------------------------------------------------- 第一阶段 市场阶段全局判定
# 四阶段仓位与盈亏比体系: 评级+情绪+量能+连板 → 全局唯一 market_phase,全系统复用。
# 字段含义:
#   cap         分阶段账户总仓位上限(全系统仓位天花板,替代固定上限)
#   single_cap  单只个股/行业ETF 上限
#   add_cap     单次新增开仓/加仓上限(<=0 禁止新增)
#   admission   主线准入线(评分门槛)
#   core_max/branch_max  核心/发酵主线数量硬约束
#   rr_left/rr_right     左侧低吸/右侧突破盈亏比门槛(None=禁止该模式)
#   tiers       允许的标的三档(空档即禁用)
#   stop_adj    止损幅度阶段化系数(×止损)
#   stab_cycle_adj  稳定器驻留周期系数
_PHASE_CFG = {
    "retreat": {"label": "退潮冰点/缩量磨底", "cap": 0.30, "single_cap": 0.01, "add_cap": 0.02,
                "admission": 65, "core_max": 2, "branch_max": 0,
                "rr_left": 2.0, "rr_right": None, "tiers": ["steady", "repair"],
                "stop_adj": 0.8, "stab_cycle_adj": 1.5, "lead_margin": 10.0, "keynote": "只减不加,极轻仓试错"},
    "startup": {"label": "启动确认期", "cap": 0.50, "single_cap": 0.02, "add_cap": 0.05,
                "admission": 60, "core_max": 3, "branch_max": 2,
                "rr_left": 2.0, "rr_right": 1.5, "tiers": ["steady", "aggressive", "repair"],
                "stop_adj": 1.0, "stab_cycle_adj": 1.0, "lead_margin": 6.0, "keynote": "回踩低吸+突破试加"},
    "main":    {"label": "主升发酵期", "cap": 0.70, "single_cap": 0.05, "add_cap": 0.10,
                "admission": 58, "core_max": 4, "branch_max": 3,
                "rr_left": 1.5, "rr_right": 1.2, "tiers": ["steady", "aggressive", "repair"],
                "stop_adj": 1.2, "stab_cycle_adj": 0.8, "lead_margin": 5.0, "keynote": "顺势加仓,持有为主"},
    "climax":  {"label": "高潮加速期", "cap": 0.50, "single_cap": 0.03, "add_cap": 0.0,
                "admission": 62, "core_max": 3, "branch_max": 0,
                "rr_left": 99.0, "rr_right": None, "tiers": ["steady"],
                "stop_adj": 0.9, "stab_cycle_adj": 1.2, "lead_margin": 8.0, "keynote": "分批兑现,逐步降仓"},
}


def _phase_from_permit(p: dict) -> str:
    """评级→阶段映射(复用 market_permit 已算指标, 不重复抓取)。"""
    grade = p.get("grade")
    fg = p.get("fear_greed")
    vol = p.get("vol_ratio")
    zt = p.get("limit_up")
    if grade in ("C", "D"):
        return "retreat"                       # 退潮冰点/缩量磨底
    if grade == "A":
        # 高潮加速: 情绪高温 或 (放量且涨停家数高 → 普涨/连板飙升)
        if (fg is not None and fg >= 70) or (vol is not None and vol >= 1.1 and zt is not None and zt >= 60):
            return "climax"
        return "main"                          # 主升发酵
    if grade == "B":
        return "startup"                       # 启动确认
    return "retreat"                           # 兜底: 未知评级按退潮防守


# 全局阶段缓存(60s): 避免各层重复判定造成口径分歧
_PHASE_CACHE = {"t": 0.0, "phase": None}


def get_market_phase(force: bool = False) -> str:
    """全局唯一市场阶段标识(60s 缓存, 供主线/标的/风控/复盘全链路复用)。"""
    now = time.time()
    if not force and _PHASE_CACHE["phase"] and now - _PHASE_CACHE["t"] < 60:
        return _PHASE_CACHE["phase"]
    try:
        p = market_permit()
        phase = _phase_from_permit(p)
    except Exception:  # noqa: BLE001
        phase = "main"                         # 兜底: 判定异常回退默认主升参数
    _PHASE_CACHE["t"] = now
    _PHASE_CACHE["phase"] = phase
    return phase


def phase_cfg(phase: str = None) -> dict:
    """当前阶段配置(含 phase 键); 阶段异常时回退 main。"""
    phase = phase or get_market_phase()
    cfg = _PHASE_CFG.get(phase) or _PHASE_CFG["main"]
    return {**cfg, "phase": phase}


# ---------------------------------------------------------------- 第一层 大盘开仓许可评级
def _in_trading_time(now: dt.datetime = None) -> bool:
    """交易时段(周一~周五 9:30-15:00, 含午休; 与 fetcher.is_trading_time 口径一致)。"""
    now = now or dt.datetime.now()
    if now.weekday() >= 5:
        return False
    m = now.hour * 60 + now.minute
    return 9 * 60 + 30 <= m <= 15 * 60


def _session_elapsed_fraction(now: dt.datetime) -> float:
    """当日交易进度(0~1): 上午 9:30-11:30 + 下午 13:00-15:00, 共240分钟。"""
    m = now.hour * 60 + now.minute
    if m <= 11 * 60 + 30:                      # 上午
        return max(0.0, (m - (9 * 60 + 30))) / 240.0
    return min(1.0, (120 + max(0, m - 13 * 60)) / 240.0)


def _market_vol_ratio(rows: list, amount_yi):
    """大盘量能比 = 截至当前时段累计成交额 / 近5日相同时段平均成交额。

    2.3 优化:
    - 存档写入加文件锁(dal.file_lock,跨平台),避免并发写冲突损坏存档;
    - 平滑改用 EWMA(alpha=0.5)替代简单移动平均,近期样本权重更高;
    - 增加量能趋势判定(连续放量/缩量),供前端与评分参考;
    - 冷启动: 相同时段存档不足5日时,用近5日全天均额按当日交易进度折算,
      而非直接对比全天均额(避免开盘初期被误判为大幅缩量)。
    """
    _ARCH_KEEP_DAYS = 7   # 存档保留天数(只需覆盖近5个交易日)
    _SMOOTH_SEC = 300     # 滚动平滑窗口=5分钟
    _EWMA_ALPHA = 0.5     # EWMA 权重(近期权重更高)
    if not amount_yi:
        return None, None
    now = dt.datetime.now()
    hm = now.strftime("%H:%M")
    # 交易时段含午休(9:30-15:00):午休期间累计成交额静止,仍属「盘中口径」
    in_session = _in_trading_time(now)
    try:
        with open(_VOL_ARCH_FILE, encoding="utf-8") as f:
            arch = json.load(f)
    except Exception:  # noqa: BLE001
        arch = {}
    arch = {d: v for d, v in arch.items() if d >= (dt.date.today() - dt.timedelta(days=_ARCH_KEEP_DAYS)).isoformat()}
    if in_session:
        arch.setdefault(_today(), {})[hm] = round(float(amount_yi), 1)
        dal.locked_write(_VOL_ARCH_FILE, json.dumps(arch, ensure_ascii=False))
    # 分母:过去5个交易日相同时段的累计成交额(取当日存档中<=当前时刻的最近一条)
    samples = []
    if in_session:
        for d in sorted(k for k in arch if k < _today())[-5:]:
            earlier = [float(v) for k, v in sorted(arch[d].items()) if k <= hm]
            if earlier:
                samples.append(earlier[-1])
    if not samples:
        # 冷启动: 相同时段存档不足 → 近5日全天均额按当日交易进度折算
        full = [float(r["amount_yi"]) for r in rows[:-1] if r.get("amount_yi")][-5:]
        if not full or sum(full) <= 0:
            return None, None
        frac = _session_elapsed_fraction(now)
        expected = (sum(full) / len(full)) * max(0.05, frac)
        vr_raw = float(amount_yi) / expected if expected > 0 else None
    else:
        if sum(samples) <= 0:
            return None, None
        vr_raw = float(amount_yi) / (sum(samples) / len(samples))
    if vr_raw is None:
        return None, None
    t = time.time()
    _VR_SMOOTH[:] = [(x, v) for x, v in _VR_SMOOTH if t - x <= _SMOOTH_SEC]
    _VR_SMOOTH.append((t, vr_raw))
    # EWMA 平滑(近期权重更高),替代简单移动平均
    ewma = None
    for _, v in sorted(_VR_SMOOTH):
        ewma = v if ewma is None else _EWMA_ALPHA * v + (1 - _EWMA_ALPHA) * ewma
    vr = ewma if ewma is not None else vr_raw
    # 量能趋势: 连续放量/缩量(供前端展示与复盘)
    d_ = "up" if vr >= 1.05 else ("down" if vr <= 0.95 else "flat")
    if _VR_TREND["dir"] == d_:
        _VR_TREND["count"] += 1
    else:
        _VR_TREND["dir"], _VR_TREND["count"] = d_, 1
    return round(vr, 3), round(vr_raw, 3)


def _volume_price_score(vr, pct_chg):
    """量价配合分(0~5分):涨放量满分/涨缩量线性扣/跌放量重罚40%/跌缩量70%。

    修复原「只看量不看价」漏洞——放量下跌是出货信号不能加分,缩量上涨
    缺乏增量资金需按比例扣分;量在价先,量价同向才给高分。
    """
    _FULL, _VR_FLOOR = 5.0, 0.5   # 满分与缩量线性扣分的下限(量能比0.5得半分)
    _DN_HEAVY, _DN_MILD = 0.4, 0.7  # 下跌时量能项得分系数:放量出货40%/缩量惜售70%
    if vr is None:
        return None
    if (pct_chg or 0.0) > 0:
        # 上涨:放量(vr>=1)拿满分,缩量按比例线性扣分
        return round(_FULL * max(_VR_FLOOR, min(float(vr), 1.0)), 2)
    return round(_FULL * (_DN_HEAVY if vr >= 1.0 else _DN_MILD), 2)


def _trend_score(closes, amounts=None):
    """趋势强度分(0~20), 0 基准完全由趋势强度驱动(2.2)。

    维度(合计权重): 20日涨跌幅(±6) + 均线排列强度(±5, MA5/10/20/60 对级)
      + 20日均线斜率(±3) + 量能配合(±2, 上涨放量确认/下跌放量确认) 
      + 持续性(±2, 连续站上/跌破20日均线) + 过度偏离惩罚(0~-3, 回归风险)。
    映射: signed∈[-20,20] → [0,20], 中性≈10(无固定基准)。
    """
    vals = [float(x) for x in (closes or []) if x]
    if len(vals) < 21:
        return 10.0, "数据不足,按中性计"
    ret20 = vals[-1] / vals[-21] - 1
    ma5 = sum(vals[-5:]) / 5.0
    ma10 = sum(vals[-10:]) / 10.0
    ma20 = sum(vals[-20:]) / 20.0
    ma60 = sum(vals[-60:]) / 60.0 if len(vals) >= 60 else ma20
    # 均线多头/空头排列强度(0~3 对)
    bull = (ma5 > ma10) + (ma10 > ma20) + (ma20 > ma60)
    bear = (ma5 < ma10) + (ma10 < ma20) + (ma20 < ma60)
    align = 5.0 * (bull - bear) / 3.0
    # 20日均线斜率(近5日 ma20 变化, 归一 ±3)
    slope_raw = (ma20 - sum(vals[-25:-20]) / 5.0) / ma20 if len(vals) >= 25 else 0.0
    slope = 3.0 * max(-1.0, min(1.0, slope_raw * 20))
    # 量能配合(用成交额近似,口径同 1.3: 只看成交额不看成交量)
    vp = 0.0
    amts = [float(x) for x in (amounts or []) if x]
    if len(amts) >= 6:
        base = sum(amts[-6:-1]) / 5.0
        if base > 0:
            amt_ratio = amts[-1] / base
            if amt_ratio >= 1.05:
                vp = 2.0 if ret20 > 0 else -2.0      # 放量确认当前趋势方向
            elif amt_ratio <= 0.9:
                vp = -1.0 if ret20 > 0 else 1.0      # 缩量: 涨缩量弱/跌缩量抛压衰竭
            else:
                vp = 0.5
    # 持续性: 连续站上/跌破20日均线天数
    above = [1 if v > ma20 else 0 for v in vals]
    streak = 0
    for v in reversed(above):
        if v == above[-1]:
            streak += 1
        else:
            break
    persist = 2.0 * min(1.0, streak / 10.0) * (1 if above[-1] else -1)
    # 过度偏离惩罚(远离20日均线→均值回归风险)
    dev = vals[-1] / ma20 - 1
    dev_pen = -3.0 * min(1.0, max(0.0, abs(dev) - 0.10) / 0.10) if abs(dev) > 0.10 else 0.0
    signed = (6.0 * max(-1.0, min(1.0, ret20 / 0.08)) + align + slope + vp + persist + dev_pen)
    score = round(max(0.0, min(20.0, (signed + 20) / 2)), 1)
    note = (f"20日{ret20:+.1%},{'多头' if above[-1] else '空头'}持续{streak}日,"
            f"斜率{slope:+.1f},偏离{dev:+.1%}")
    return score, note


def _fg_series(fg) -> list:
    """恐贪历史(近10个自然日), 每日存最新值; 用于情绪波动判定(2.1 情绪市)。"""
    try:
        with open(_FG_ARCH_FILE, encoding="utf-8") as f:
            arch = json.load(f)
    except Exception:  # noqa: BLE001
        arch = {}
    if fg is not None:
        arch[_today()] = round(float(fg), 1)
        dal.locked_write(_FG_ARCH_FILE, json.dumps(arch, ensure_ascii=False))
    return [float(v) for _, v in sorted(arch.items())][-10:]


def _adaptive_weights(fg_hist: list, closes: list) -> dict:
    """环境自适应评分权重(2.1)。

    - 趋势市(20日涨跌幅| |>5%): trend 30 / mood 20
    - 震荡市(20日涨跌幅| |<2%): breadth 35 / trend 10
    - 情绪市(恐贪波动率>20):   mood 35 / vp 0(显式清零)
    近5组权重均值平滑 + 单维度±50%限幅(显式清零维度除外) + 归一化总分100。
    """
    _BASE = {"mood": 30.0, "breadth": 25.0, "zt": 20.0, "vp": 5.0, "trend": 20.0}
    _SMOOTH_N, _LIMIT = 5, 0.5
    w = dict(_BASE)
    mode = "neutral"
    if closes and len(closes) >= 21:
        ret20 = float(closes[-1]) / float(closes[-21]) - 1
        if abs(ret20) > 0.05:
            mode = "trend"
        elif abs(ret20) < 0.02:
            mode = "range"
    mood_vol = 0.0
    if fg_hist and len(fg_hist) >= 5:
        m = sum(fg_hist) / len(fg_hist)
        mood_vol = (sum((v - m) ** 2 for v in fg_hist) / len(fg_hist)) ** 0.5
    if mode == "trend":
        w["trend"], w["mood"] = 30.0, 20.0
    elif mode == "range":
        w["breadth"], w["trend"] = 35.0, 10.0
    zero_keys = set()
    if mood_vol > 20:                       # 情绪市: 恐贪权重提升、量价归零(显式规则)
        w["mood"], w["vp"] = 35.0, 0.0
        zero_keys.add("vp")
    w["mode"] = mode
    # 5组权重均值平滑(当日重复调用稳定,避免单次环境抖动引发权重跳变)
    _WEIGHT_HIST.append(dict(w))
    if len(_WEIGHT_HIST) > _SMOOTH_N:
        _WEIGHT_HIST.pop(0)
    keys = ("mood", "breadth", "zt", "vp", "trend")
    if len(_WEIGHT_HIST) > 1:
        avg = {k: sum(h[k] for h in _WEIGHT_HIST) / len(_WEIGHT_HIST) for k in keys}
        w.update(avg)
    # 单维度 ±50% 限幅(显式清零的维度保持 0)
    for k in keys:
        if k in zero_keys:
            w[k] = 0.0
            continue
        w[k] = max(_BASE[k] * (1 - _LIMIT), min(_BASE[k] * (1 + _LIMIT), w[k]))
    tot = sum(w[k] for k in keys) or 100.0
    for k in keys:
        w[k] = round(w[k] / tot * 100, 1)
    w["mode"] = mode
    return w


def _market_score(fg, adv_ratio, zt, vp, trend, weights: dict = None) -> float:
    """大盘评分(总分固定100)=恐贪+宽度+涨停+量价配合+趋势强度。

    权重默认 30/25/20/5/20,由 _adaptive_weights 按市场环境动态调整(2.1);
    vp/trend 缺失时按中性值计入,不中断打分。
    """
    w = weights or {"mood": 30.0, "breadth": 25.0, "zt": 20.0, "vp": 5.0, "trend": 20.0}
    s = 0.0
    if fg is not None:
        s += max(0.0, min(float(fg), 100.0)) / 100.0 * w["mood"]
    if adv_ratio is not None:
        s += min(max(float(adv_ratio), 0.0), 2.0) / 2.0 * w["breadth"]
    if zt is not None:
        s += min(max(int(zt), 0), 80) / 80.0 * w["zt"]
    s += vp if vp is not None else w["vp"] * 0.6          # 量价分缺失按中性60%计
    s += trend if trend is not None else w["trend"] / 2.0  # 趋势分缺失按中性50%计
    return round(min(s, 100.0), 1)


def _grade_raw(score, zt, adv_ratio, rules, bias: float = 0.0) -> str:
    """评级原始判定(A/B/C/D)。bias>0 更严(升级用), bias<0 更松(降级用)。"""
    if zt is None:
        zt = 0
    ar = adv_ratio if adv_ratio is not None else -1.0     # 缺失按最差计(防 None 比较异常)
    if score >= rules["score_full"] + bias and zt >= rules["zt_full"] and ar >= rules["adv_ratio_full"]:
        return "A"
    if score >= rules["score_ok"] + bias and zt >= rules["zt_ok"] and ar >= rules["adv_ratio_ok"]:
        return "B"
    if score >= rules["score_hold"] or zt >= rules["zt_hold"]:
        return "C"
    return "D"


def _load_last_grade() -> tuple:
    """读取最近一次评级存档(优先当日,否则最近一天)。返回 (grade, arch)。"""
    try:
        with open(_GRADE_ARCH_FILE, encoding="utf-8") as f:
            arch = json.load(f)
    except Exception:  # noqa: BLE001
        return None, {}
    if _today() in arch:
        return arch[_today()], arch
    return (arch[sorted(arch.keys())[-1]] if arch else None), arch


def _grade(score, zt, adv_ratio, rules) -> tuple:
    """评级滞回(2.4): 抑制 A→B→A 式频繁切换。

    - 滞回: 升级用更严阈值(bias=+5), 降级用更松阈值(bias=-5);
    - 切换确认: 盘中需连续 _GRADE_CONFIRM 次(5分钟窗口)满足, 收盘后单次即定;
    - 单日最多变化1级(限幅);
    - 切换/维持输出明确原因, 供复盘; 当日评级持久化到 grade_history.json。
    """
    raw = _grade_raw(score, zt, adv_ratio, rules, bias=0.0)
    last, arch = _load_last_grade()
    change_reason = None
    today = _today()

    if last is None or last == raw:
        grade = raw
        _GRADE_PENDING.update({"grade": None, "count": 0, "ts": 0.0})
    else:
        order = _GRADE_ORDER
        lv, rv = order.get(last, 1), order.get(raw, 1)
        # 单日限幅: 向 raw 方向最多变化1级
        if abs(rv - lv) > _GRADE_MAX_STEP:
            direction = 1 if rv > lv else -1
            raw = [g for g, o in order.items() if o == lv + direction][0]
            rv = order[raw]
        if raw == last:
            grade = last
        else:
            # 滞回判定: 升级需更严(bias>0)/降级需更松(bias<0)
            if rv > lv:
                cand = _grade_raw(score, zt, adv_ratio, rules, bias=_GRADE_UP_BIAS)
                justified = order[cand] > lv
            else:
                cand = _grade_raw(score, zt, adv_ratio, rules, bias=-_GRADE_DOWN_BIAS)
                justified = order[cand] < lv
            if not justified:
                grade = last
                change_reason = (f"滞回: 未满足「{'升级' if rv > lv else '降级'}」确认阈值"
                                 f"(需更{'严' if rv > lv else '松'}边界),维持 {last}")
                _GRADE_PENDING.update({"grade": None, "count": 0, "ts": 0.0})
            elif not _in_trading_time():
                grade = raw
                change_reason = f"评级切换 {last}→{raw}(收盘口径确认)"
            else:
                # 盘中 5 分钟滚动窗口确认
                now = time.time()
                if _GRADE_PENDING["grade"] == raw and now - _GRADE_PENDING["ts"] <= 300:
                    _GRADE_PENDING["count"] += 1
                else:
                    _GRADE_PENDING.update({"grade": raw, "count": 1, "ts": now})
                if _GRADE_PENDING["count"] >= _GRADE_CONFIRM:
                    grade = raw
                    change_reason = f"评级切换 {last}→{raw}(5分钟窗口连续确认)"
                    _GRADE_PENDING.update({"grade": None, "count": 0, "ts": 0.0})
                else:
                    grade = last
                    change_reason = f"评级切换待确认 {last}→{raw}({_GRADE_PENDING['count']}/{_GRADE_CONFIRM})"

    # 持久化当日评级
    arch[today] = grade
    dal.locked_write(_GRADE_ARCH_FILE, json.dumps(arch, ensure_ascii=False))
    return grade, change_reason


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
    vol_ratio = vol_raw = vp = trend = trend_note = None
    closes = amounts = None
    try:
        from app.review.data import collect_market_daily
        rows = collect_market_daily(30)  # 30行:趋势分需21日收盘序列
        if rows:
            amount_yi = rows[-1].get("amount_yi")
            # 量能比:相同时段对比 + EWMA 平滑;量价配合分绑定大盘涨跌方向
            vol_ratio, vol_raw = _market_vol_ratio(rows, amount_yi)
            vp = _volume_price_score(vol_ratio, rows[-1].get("pct_chg"))
            closes = [r.get("close") for r in rows]
            amounts = [r.get("amount_yi") for r in rows]
            trend, trend_note = _trend_score(closes, amounts)
    except Exception:  # noqa: BLE001
        pass

    # 2.1 环境自适应权重(趋势市/震荡市/情绪市 → 动态权重, 5日均值平滑)
    weights = _adaptive_weights(_fg_series(fg), closes)
    score = _market_score(fg, adv_ratio, zt, vp, trend, weights)
    # 2.4 评级滞回(升级更严/降级更松 + 切换确认 + 单日限幅)
    grade, grade_change = _grade(score, zt, adv_ratio, rules)
    # 阶段全局判定(评级+情绪+量能+连板), 总仓位上限动态化为分阶段上限(全系统天花板)
    phase = _phase_from_permit({"grade": grade, "fear_greed": fg,
                                "vol_ratio": vol_ratio, "limit_up": zt})
    pcfg = _PHASE_CFG[phase]
    cap = pcfg["cap"]
    checks = {
        "大盘打分": {"value": score, "ok": score >= rules["score_full"], "ok_min": score >= rules["score_ok"]},
        "涨停家数": {"value": zt, "ok": zt is not None and zt >= rules["zt_full"],
                     "ok_min": zt is not None and zt >= rules["zt_ok"]},
        "涨跌家数比": {"value": round(adv_ratio, 2) if adv_ratio else None,
                       "ok": adv_ratio is not None and adv_ratio >= rules["adv_ratio_full"],
                       "ok_min": adv_ratio is not None and adv_ratio >= rules["adv_ratio_ok"]},
    }
    _W_MODE_TAG = {"trend": "趋势市", "range": "震荡市", "neutral": "中性市"}
    reasons = []
    if fg is not None:
        reasons.append(f"恐贪指数 {fg:.0f} 分({fear_greed_label(fg)}),贡献评分 {fg / 100 * weights['mood']:.0f}/{weights['mood']:.0f}")
    else:
        reasons.append("恐贪指数数据缺失,暂按中性计入")
    if adv_ratio is not None:
        reasons.append(f"涨停 {zt} 家 / 上涨 {adv} 家 vs 下跌 {dec} 家,家数比 {adv_ratio:.2f}")
    else:
        reasons.append(f"涨停 {zt} 家 / 上涨 {adv} 家 vs 下跌 {dec} 家(涨跌家数比数据缺失)")
    if amount_yi:
        reasons.append(f"两市成交额 {amount_yi:,.0f} 亿")
    if vol_ratio is not None:
        tag = "放量" if vol_ratio >= 1.05 else ("缩量" if vol_ratio <= 0.95 else "平量")
        tag += f",连续{_VR_TREND['count']}次{'放量' if _VR_TREND['dir']=='up' else ('缩量' if _VR_TREND['dir']=='down' else '平量')}" if _VR_TREND["dir"] else ""
        reasons.append(f"量能比 {vol_ratio:.2f}({tag},对比近5日)"
                       + (f",量价配合分 {vp}/5" if vp is not None else ""))
    if trend is not None:
        reasons.append(f"趋势强度 {trend}/20({trend_note})")
    reasons.append(f"权重模式: {_W_MODE_TAG.get(weights['mode'], weights['mode'])}(恐贪{weights['mood']:.0f}/宽度{weights['breadth']:.0f}/"
                   f"涨停{weights['zt']:.0f}/量价{weights['vp']:.0f}/趋势{weights['trend']:.0f})")
    reasons.append(f"大盘综合评分 {score},达 {grade} 级标准,当前市场阶段「{pcfg['label']}」总仓位上限 {cap:.0%}")
    if grade_change:
        reasons.append(grade_change)
    return {
        "grade": grade,
        "grade_label": {"A": "A级·积极配置", "B": "B级·谨慎配置",
                        "C": "C级·持有兑现", "D": "D级·观望为主"}[grade],
        "cap": cap,
        "score": score, "fear_greed": fg, "fear_greed_label": fear_greed_label(fg),
        "limit_up": zt, "advance": adv, "decline": dec, "adv_ratio": adv_ratio,
        "amount_yi": amount_yi, "vol_ratio": vol_ratio, "vol_ratio_raw": vol_raw,
        "vp_score": vp, "trend_score": trend, "checks": checks, "reasons": reasons,
        "weights": {k: weights[k] for k in ("mood", "breadth", "zt", "vp", "trend")},
        "weight_mode": weights.get("mode"),
        "vol_trend": {"dir": _VR_TREND["dir"], "count": _VR_TREND["count"]},
        "grade_change": grade_change,
        "market_phase": phase,
        "phase_label": pcfg["label"],
        "operation_keynote": pcfg["keynote"],
        "date": str(snap.get("market_date") or _today()),
    }


# ---------------------------------------------------------------- 第二层 主线概念自动遴选
def _veto(name, r, dcfg, stats, zt_available=True) -> tuple:
    """一票否决(3.1 保留真否决): 仅 利空关键词命中 与 板块统计严重缺失。

    原「主力净流出 / 涨停不足 / 近3日过热」改为 _veto_penalty 分级扣分(不再直接否决)。
    返回 (是否否决, 理由列表)。"""
    veto = dcfg.get("veto", {})
    reasons = []
    if stats is None and veto.get("data_missing", True):
        reasons.append("板块统计数据严重缺失(概念指数不可得),一票否决")
    for kw in veto.get("bad_news_kw", []):
        if kw in (name + (r.get("leader") or "")):
            reasons.append(f"名称/领涨股命中利空关键词「{kw}」(一票否决)")
    return bool(reasons), reasons


def _veto_penalty(r, dcfg, stats, zt_available=True) -> tuple:
    """一票否决改分级扣分(3.1): 软性项按程度扣分而非直接否决。

    - 主力净流出: 按净流出占板块成交额比例扣 0-10 分;
    - 涨停不足 min_zt: 每缺 1 家扣 per_missing(默认5)分;
    - 近3日涨幅过热: 按超出阈值幅度扣 0-10 分。
    返回 (penalty, reasons)。
    """
    vp = dcfg.get("veto_penalty", {})
    reasons = []
    penalty = 0.0
    # 主力净流出(0-10)
    ncfg = vp.get("net_out", {})
    if ncfg.get("enabled", True) and (r.get("net_yi") or 0) < 0:
        amt = float((r.get("inflow_yi") or 0) + (r.get("outflow_yi") or 0))
        net = float(-r["net_yi"])
        ratio = min(1.0, net / amt) if amt > 0 else min(1.0, net / 50.0)
        pts = float(ncfg.get("max_pts", 10.0)) * ratio
        penalty += pts
        reasons.append(f"主力净流出 {r['net_yi']:.1f} 亿(占成交额 {ratio * 100:.0f}%),扣 {pts:.1f} 分")
    # 涨停不足(每缺1家扣 per_missing)
    zcfg = vp.get("zt_short", {})
    min_zt = int(dcfg.get("veto", {}).get("min_zt_in_sector", 2))
    if zcfg.get("enabled", True) and zt_available:
        missing = max(0, min_zt - int(r.get("zt_count", 0)))
        if missing > 0:
            pts = missing * float(zcfg.get("per_missing", 5.0))
            penalty += pts
            reasons.append(f"涨停 {r.get('zt_count', 0)} 家,不足 {min_zt} 家,扣 {pts:.1f} 分")
    # 近3日过热(0-10)
    ocfg = vp.get("overheat", {})
    thr = float(ocfg.get("threshold", dcfg.get("veto", {}).get("max_gain_3d", 0.15)))
    gain3 = (stats or {}).get("gain3")
    if ocfg.get("enabled", True) and gain3 is not None and gain3 >= thr:
        pts = min(float(ocfg.get("max_pts", 10.0)),
                  (gain3 - thr) / max(thr, 1e-6) * float(ocfg.get("max_pts", 10.0)))
        penalty += pts
        reasons.append(f"近3日涨幅 {gain3 * 100:+.1f}%,超 {thr * 100:.0f}% 过热,扣 {pts:.1f} 分")
    return round(penalty, 1), reasons


def _pass_reasons(r, stats, score=None) -> list:
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
    out.append(f"综合评分 {score if score is not None else r['score']} 分,达标")
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
    """同池板块按风格偏转微调排序(第五轮扩展因子,排序层面兜底)。

    分数层面已由 _style_score_adj 微调(3.4);此处仅在同池相邻分差<=thresh 时
    将与本轮风格对齐的板块提前,作同分/近分时的先后参考。
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


# ---------------------------------------------------------------- 3.2 准入线动态调整
_SECTOR_SCORE_FILE = os.path.join(config.DATA_DIR, "sector_score_history.json")
_SECTOR_SCORE_CACHE = {"t": 0.0, "data": None}   # 60s 缓存,避免每板块每周期读文件


def _record_sector_scores(rows: list) -> None:
    """当日板块评分存档(每日覆盖最新值),供准入线历史分位调整(3.2)。"""
    try:
        arch = _load_sector_score_arch()
        arch[_today()] = {r["industry"]: float(r["score"]) for r in rows
                          if r.get("score") is not None}
        keep = sorted(arch.keys())[-90:]
        arch = {k: arch[k] for k in keep}
        dal.locked_write(_SECTOR_SCORE_FILE, json.dumps(arch, ensure_ascii=False))
        _SECTOR_SCORE_CACHE["data"] = arch
        _SECTOR_SCORE_CACHE["t"] = time.time()
    except Exception as e:  # noqa: BLE001
        dal.record_missing("sector_score_hist", False, f"板块评分存档失败: {e}")


def _load_sector_score_arch() -> dict:
    if _SECTOR_SCORE_CACHE["data"] is not None and time.time() - _SECTOR_SCORE_CACHE["t"] < 60:
        return _SECTOR_SCORE_CACHE["data"]
    try:
        with open(_SECTOR_SCORE_FILE, encoding="utf-8") as f:
            arch = json.load(f)
    except Exception:  # noqa: BLE001
        arch = {}
    _SECTOR_SCORE_CACHE["data"] = arch
    _SECTOR_SCORE_CACHE["t"] = time.time()
    return arch


def _sector_admission_adj(name: str, score: float) -> float:
    """准入线动态调整(3.2): 板块近60日评分分位数(80%分位以上下调5/20%以下上调5)
    + 波动率(标准差>10 上调3 / <5 下调3), 总浮动不超过 ±10。返回调整值(可为0)。"""
    aacfg = _cfg().get("mainline", {}).get("admission_adj", {})
    if not aacfg.get("enabled", True):
        return 0.0
    today = _today()
    hist = []
    try:
        arch = _load_sector_score_arch()
        for d, vals in arch.items():
            if d == today or vals.get(name) is None:
                continue
            hist.append(float(vals[name]))
        hist = hist[-60:]
    except Exception:  # noqa: BLE001
        return 0.0
    if len(hist) < int(aacfg.get("min_hist", 10)):
        return 0.0
    pct = sum(1 for v in hist if v < score) / len(hist)
    m = sum(hist) / len(hist)
    std = (sum((v - m) ** 2 for v in hist) / len(hist)) ** 0.5
    adj = 0.0
    if pct >= float(aacfg.get("pct_up", 0.80)):
        adj -= float(aacfg.get("pct_pts", 5.0))          # 高历史分位 → 下调准入线(更易入选)
    elif pct <= float(aacfg.get("pct_down", 0.20)):
        adj += float(aacfg.get("pct_pts", 5.0))          # 低历史分位 → 上调准入线(更难入选)
    if std > float(aacfg.get("vol_high", 10.0)):
        adj += float(aacfg.get("vol_pts", 3.0))          # 高波动 → 上调(更谨慎)
    elif std < float(aacfg.get("vol_low", 5.0)):
        adj -= float(aacfg.get("vol_pts", 3.0))          # 低波动 → 下调
    return round(max(-float(aacfg.get("max_adj", 10.0)),
                     min(float(aacfg.get("max_adj", 10.0)), adj)), 1)


# ---------------------------------------------------------------- 3.4 风格偏转分数微调
def _style_score_adj(style: dict, size_bias) -> float:
    """风格偏转分数微调(3.4): 与当前市场风格对齐 +1~3 分,背离 -1~3 分(强度定标),单板块上限±5。

    强度: |大小盘动量差| 1%~3% → 1~3 分;style_confidence=low 缩放 0.5(连续一致性不足时降权)。
    """
    scfg = _cfg().get("mainline", {}).get("style_adj", {})
    if not scfg.get("enabled", True) or not style:
        return 0.0
    bias = style.get("bias")
    if bias not in (-1, 1) or size_bias not in (-1, 1):
        return 0.0
    diff = abs(float(style.get("style_diff_pct") or style.get("rel_score") or 0.0))
    lo, hi = float(scfg.get("min_pts", 1.0)), float(scfg.get("max_pts", 3.0))
    strength = lo + (hi - lo) * min(1.0, max(0.0, diff / 0.02))
    if str(style.get("style_confidence")) == "low":
        strength *= float(scfg.get("low_conf_scale", 0.5))
    aligned = 1 if size_bias == bias else -1
    delta = aligned * strength
    return round(max(-float(scfg.get("max_total", 5.0)),
                     min(float(scfg.get("max_total", 5.0)), delta)), 2)


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
    vr = it.get("volume_ratio")
    if vr is not None:
        vtag = ("放量" if vr >= 1.05 else ("缩量" if vr <= 0.95 else "平量")) \
            + ("上涨" if (it.get("pct_chg") or 0) > 0 else "下跌")
        zt += f",量能{vtag}(比 {vr:.2f})"
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
    admission = phase_cfg()["admission"]   # 分阶段准入线(退潮上调收紧, 主升下调放宽)
    rows = _ml.sector_scores(use_cache=True)
    zt_pool = _ml._zt_pool()
    zt_available = len(zt_pool) > 0  # 涨停池为空视为数据缺失,跳过涨停家数扣分项
    # 3.2 板块评分历史存档(每日覆盖,供准入线历史分位调整)
    try:
        _record_sector_scores(rows)
    except Exception:  # noqa: BLE001
        pass
    # 大盘环境联动:量能修正幅度随大盘评级缩放(A/B/C-D -> 100%/80%/50%)
    try:
        mkt_grade = market_permit().get("grade")
    except Exception:  # noqa: BLE001
        mkt_grade = None
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
        # 3.1 真否决(利空关键词/数据严重缺失)
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
                "volume_ratio": (stats or {}).get("volume_ratio"),
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
        # 板块量能修正(固化规则):方向绑定+滞回+过热保护+大盘联动,见 _sector_volume_adj
        if stats and stats.get("volume_ratio") is not None:
            delta, vr_smooth = _sector_volume_adj(
                r["industry"], stats["volume_ratio"], r.get("pct_chg"),
                (stats or {}).get("gain3"), mkt_grade)
            item["volume_ratio"] = vr_smooth
            item["score"] = round(r["score"] + delta, 2)
            item["volume_adj"] = delta
        # 3.1 分级扣分(净流出/涨停不足/过热)替代一票否决
        penalty, pen_reasons = _veto_penalty(r, dcfg, stats, zt_available=zt_available)
        if penalty:
            item["score"] = round(item["score"] - penalty, 2)
            item["veto_penalty"] = penalty
            item["veto_penalty_reasons"] = pen_reasons
        # 3.4 风格偏转分数微调(与当前大小盘风格对齐 +分/背离 -分)
        if ext_cfg and style:
            s_adj = _style_score_adj(style, r.get("size_bias", 0))
            if s_adj:
                item["score"] = round(item["score"] + s_adj, 2)
                item["style_adj"] = s_adj
        # 3.2 准入线动态调整(板块历史分位+波动率)
        admission_adj = _sector_admission_adj(r["industry"], item["score"])
        eff_admission = round(admission + admission_adj, 1)
        if admission_adj:
            item["admission_adj"] = admission_adj
        if item["score"] >= eff_admission:
            item["reasons"] = _pass_reasons(r, stats, item["score"])
            if item.get("volume_adj"):
                vr = item["volume_ratio"] or 0
                vtag = ("放量" if vr >= 1.05 else ("缩量" if vr <= 0.95 else "平量")) \
                    + ("上涨" if (r.get("pct_chg") or 0) > 0 else "下跌")
                item["reasons"].insert(0, f"板块量能比 {vr:.2f}({vtag}),评分{'上调' if item['volume_adj'] > 0 else '下调'} {abs(item['volume_adj']):.1f} 分")
            for _p in pen_reasons:
                item["reasons"].append(f"扣分: {_p}")
            if item.get("style_adj"):
                item["reasons"].append(f"风格偏转 {'对齐' if item['style_adj'] > 0 else '背离'} {item['style_adj']:+.1f} 分")
            item["pool"] = _sector_pool(r["industry"])
            passed.append(item)
        else:
            item["level"] = "watch"
            vtxt = ""
            if item.get("volume_ratio") is not None:
                vr = item["volume_ratio"]
                vtag = ("放量" if vr >= 1.05 else ("缩量" if vr <= 0.95 else "平量")) \
                    + ("上涨" if (r.get("pct_chg") or 0) > 0 else "下跌")
                vtxt = f"(量能比 {vr:.2f} {vtag},量能修正 {item.get('volume_adj') or 0:+.1f} 分)"
            item["reasons"] = [f"综合评分 {item['score']} 分,低于阶段准入线 {eff_admission} 分"
                               + (f"(板块动态{admission_adj:+.1f})" if admission_adj else "")
                               + f",仅跟踪{vtxt}"]
            for _p in pen_reasons:
                item["reasons"].append(f"扣分: {_p}")
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
           "pass_score": admission}
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
    "repair": "补涨优选:回踩 {support} 企稳低吸,放量启动确认后跟进",
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
    pcfg = phase_cfg()   # 分阶段仓位矩阵/操作基调/止损系数(全系统单一来源)
    risk_rate = dcfg.get("risk", {}).get(taste, 0.015)
    batch = dcfg.get("batch", {"first": 0.60, "second": 0.40})
    plancfg = dcfg.get("plan", {})
    price = float(target.get("price") or 0)
    if price <= 0:
        return {"ok": False, "reason": "无有效现价"}
    if pcfg["add_cap"] <= 0:
        # 高潮加速/退潮: 禁止新增开仓/加仓(风控前置拦截, 而非事后提示)
        return {"ok": False, "name": target.get("name"), "code": target.get("code"),
                "reason": f"当前阶段「{pcfg['label']}」禁止新增开仓/加仓(单次上限 0)"}
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
    max_mv = total_asset * min(market_cap or 1.0, single_cap, pcfg["single_cap"])  # 阶段单票上限并入

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

    # ---- 分阶段单次新增/加仓上限(硬约束, 开仓前拦截)
    if pcfg["add_cap"] > 0:
        add_mv = total_asset * pcfg["add_cap"]
        if pos_value > add_mv:
            pos_value = add_mv
            shares = int(pos_value / avg_cost // 100) * 100
            shares = max(shares, 100)
            pos_value = shares * avg_cost
            first = int(shares * batch.get("first", 0.60) // 100) * 100
            second = shares - first
            max_loss = shares * loss_per_share

    # ---- 止损阶段化(退潮收紧20% / 主升放宽20% / 高潮收紧), 且不得高于现价
    stop = min(stop * pcfg["stop_adj"], price * 0.98)

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
        "market_phase": pcfg["phase"],
        "phase_label": pcfg["label"],
        "operation_keynote": pcfg["keynote"],
        "trade_mode": target.get("trade_mode"),
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
def _match_targets(sector_name: str, sector_level: str = "watch",
                   sector_status: str = "core") -> dict:
    """第三层标的匹配入口(第六轮外挂接入点)。

    全关时输出与 match_level_targets 逐字段一致(兼容历史回测);
    开启优化时 aggressive/steady/etf 为稳定输出正式标的,并附加
    raw_targets / stable_targets / candidate_targets / fallback_targets 分层字段。
    """
    from app.support import target_match as _tm
    res = _tm.match_targets_v2(sector_name, sector_level, sector_status)
    raw = res.get("raw_targets") or {}
    st = res.get("stable_targets") or {}
    # 兼容视图:保留原有 per-role 结构(供 plan/页面按原方式读取),items 用稳定输出
    out = {"sector": sector_name, "raw_targets": raw, "stable_targets": st}
    _LABEL = {"steady": "稳健型·中军龙头", "aggressive": "激进型·情绪龙头",
              "etf": "工具型·对应ETF", "repair": "补涨优选·高低切换"}
    for role in ("aggressive", "steady", "etf", "repair"):
        seg = raw.get(role) or {}
        out[role] = {"label": seg.get("label", "") or _LABEL.get(role, ""),
                     "mood": seg.get("mood", []),
                     "items": st.get(role, [])}
    out["candidate_targets"] = st.get("candidate", [])
    out["fallback_targets"] = st.get("fallback", [])
    return out


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
        t = _match_targets(core["name"], sector_level="core", sector_status="core")
        targets[core["name"]] = t
        role_slots = (("steady", "mid"), ("aggressive", "mood"),
                      ("repair", "repair"), ("etf", "etf"))
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
            # 三档差异化仓位系数: 中军×1.5 / 情绪×0.5 / 补涨×0.6(随标的输出)
            coef = float(item.get("position_coef") or 1.0)
            if p.get("ok") and p.get("position_pct"):
                p["position_pct"] = round(min(1.0, p["position_pct"] * coef), 4)
            plans.setdefault(core["name"], {})[role] = p
            if p.get("ok"):
                blk_used = min(1.0, blk_used + (p.get("position_pct") or 0.0))
    if defensive:
        t = _match_targets(defensive["name"], sector_level="defensive", sector_status="defensive")
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
