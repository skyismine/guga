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
from app.support.signals import (_SIGNAL_LEVELS, _SIGNAL_RANK, _ACTION_TO_SIGNAL,
                                 shift_signal as _shift_signal, _TRIGGER_TPL)

def _fault(e: BaseException, note: str = ""):
    """记录一次被降级吞掉的异常(接入 fault 统一日志, 不再静默; 保持原降级语义)。"""
    try:
        from app.support import fault as _flt
        _flt.warning("engine", note or "处理降级(按缺省继续)", exc=e)
    except Exception as _e:  # noqa: BLE001
        _fault(_e)


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
                    last = float(v.iloc[-1])
                    base = float(v.tail(5).mean())
                    if base > 0 and last > 0:
                        out["volume_ratio"] = float(last / base)
                    elif base > 0:
                        # 当日量缺失/停牌: 用近5日均量(比值=1.0)填充并标注, 不做板块均值外推
                        out["volume_ratio"] = 1.0
                        out["volume_ratio_filled"] = "history_mean"
            return out
    except Exception as _e:  # noqa: BLE001
        _fault(_e)
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
    - 平滑改用 EWMA 替代简单移动平均,近期样本权重更高(α 可配置,无窗口边界跳变);
    - 原始量能比先做 Winsorize 截断(clip_lo~clip_hi),防冷启动折算/数据毛刺污染 EWMA;
    - 增加量能趋势判定(连续放量/缩量),供前端与评分参考;
    - 冷启动: 相同时段存档不足5日时,用近5日全天均额按当日交易进度折算,
      而非直接对比全天均额(避免开盘初期被误判为大幅缩量)。
    """
    _ARCH_KEEP_DAYS = 7   # 存档保留天数(只需覆盖近5个交易日)
    _SMOOTH_SEC = 300     # 滚动平滑窗口=5分钟
    # 平滑参数(settings.decision.exec_param.vol_ratio 可调)
    _vr_cfg = (_cfg().get("exec_param") or {}).get("vol_ratio", {}) or {}
    _EWMA_ALPHA = float(_vr_cfg.get("ewma_alpha", 0.5) or 0.5)
    _CLIP_LO = float(_vr_cfg.get("clip_lo", 0.3) or 0.3)
    _CLIP_HI = float(_vr_cfg.get("clip_hi", 3.0) or 3.0)
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
    # Winsorize 截断: 异常原始量能比(冷启动折算可放大至 5+ / 数据毛刺)先夹到合理区间, 防污染 EWMA
    vr_raw_c = max(_CLIP_LO, min(_CLIP_HI, vr_raw))
    t = time.time()
    _VR_SMOOTH[:] = [(x, v) for x, v in _VR_SMOOTH if t - x <= _SMOOTH_SEC]
    _VR_SMOOTH.append((t, vr_raw_c))
    # EWMA 平滑(近期权重更高),替代简单移动平均;无窗口边界跳变(旧值指数衰减而非突然剔除)
    ewma = None
    for _, v in sorted(_VR_SMOOTH):
        ewma = v if ewma is None else _EWMA_ALPHA * v + (1 - _EWMA_ALPHA) * ewma
    vr = ewma if ewma is not None else vr_raw_c
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


def _adaptive_weights(fg_hist: list, closes: list, amounts: list = None) -> dict:
    """环境自适应评分权重(2.1 升级: 情绪市方向修正 + 判据细化)。

    - 趋势市(|20日涨幅|≥5% 且 MA20 斜率>0.3%/日): trend 30 / mood 20 / vp 10
    - 震荡市(|20日涨幅|<2% 且 20日振幅<8%): breadth 35 / mood 25 / trend 10
    - 情绪市(恐贪10日波动率>20 或 量能比近5日标准差>0.3): mood 20 / vp 15 / zt 25 / trend 15
      —— 情绪市恐贪剧烈波动应低配恐贪降噪、高配量价/涨停(量能才是情绪持续性关键),
         修正 2.1「恐贪35/量价0」的相反方向;
    - 中性市: 默认(恐贪30/宽度25/涨停20/量价5/趋势20); 情绪市优先于趋势/震荡判定。
    近5组权重均值平滑 + 单维度±50%限幅 + 归一化总分100。
    """
    _BASE = {"mood": 30.0, "breadth": 25.0, "zt": 20.0, "vp": 5.0, "trend": 20.0}
    _SMOOTH_N, _LIMIT = 5, 0.5
    w = dict(_BASE)
    mode = "neutral"
    ret20 = ma_slope = amp20 = None
    c = [float(x) for x in (closes or []) if x]
    if len(c) >= 26:
        ret20 = c[-1] / c[-21] - 1
        ma_now = sum(c[-20:]) / 20.0
        ma_prev = sum(c[-25:-5]) / 20.0 if len(c) >= 25 else ma_now
        ma_slope = (ma_now / ma_prev - 1) / 5.0 if ma_prev else 0.0    # MA20 每日斜率(小数/日)
        lo20 = min(c[-20:])
        amp20 = (max(c[-20:]) / lo20 - 1) if lo20 else 0.0             # 20日振幅
    mood_vol = 0.0
    if fg_hist and len(fg_hist) >= 5:
        m = sum(fg_hist) / len(fg_hist)
        mood_vol = (sum((v - m) ** 2 for v in fg_hist) / len(fg_hist)) ** 0.5
    amt_std5 = 0.0
    a = [float(x) for x in (amounts or []) if x]
    if len(a) >= 6:
        ratios = [a[i] / a[i - 1] for i in range(1, len(a)) if a[i - 1] and a[i]]
        if len(ratios) >= 5:
            r = ratios[-5:]
            rm = sum(r) / len(r)
            amt_std5 = (sum((v - rm) ** 2 for v in r) / len(r)) ** 0.5   # 量能比近5日标准差
    trend_ok = (ret20 is not None and abs(ret20) >= 0.05
                and (ma_slope is not None and abs(ma_slope) > 0.003))
    range_ok = (ret20 is not None and abs(ret20) < 0.02
                and amp20 is not None and amp20 < 0.08)
    emotion_ok = mood_vol > 20 or amt_std5 > 0.3
    if emotion_ok:                       # 情绪市优先(题材炒作期恐贪/量能剧变须特殊处理)
        mode = "emotion"
        w.update(mood=20.0, vp=15.0, zt=25.0, trend=15.0)
    elif trend_ok:
        mode = "trend"
        w.update(trend=30.0, mood=20.0, vp=10.0)
    elif range_ok:
        mode = "range"
        w.update(breadth=35.0, mood=25.0, trend=10.0)
    w["mode"] = mode
    # 5组权重均值平滑(当日重复调用稳定,避免单次环境抖动引发权重跳变)
    _WEIGHT_HIST.append(dict(w))
    if len(_WEIGHT_HIST) > _SMOOTH_N:
        _WEIGHT_HIST.pop(0)
    keys = ("mood", "breadth", "zt", "vp", "trend")
    if len(_WEIGHT_HIST) > 1:
        avg = {k: sum(h[k] for h in _WEIGHT_HIST) / len(_WEIGHT_HIST) for k in keys}
        w.update(avg)
    # 单维度 ±50% 限幅
    for k in keys:
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


def _grade_cfg() -> dict:
    """评级滞回参数(settings.decision.grade_hysteresis, 缺省回退内置常量)。"""
    try:
        return _cfg().get("grade_hysteresis", {}) or {}
    except Exception:  # noqa: BLE001
        return {}


def _last_grade_floor(last: str, rules: dict) -> float:
    """当前评级维持所需的最低评分(用于熔断式降级判定)。"""
    if last == "A":
        return float(rules.get("score_full", 70))
    if last == "B":
        return float(rules.get("score_ok", 50))
    if last == "C":
        return float(rules.get("score_hold", 30))
    return 0.0


def _grade(score, zt, adv_ratio, rules) -> tuple:
    """评级滞回(2.4): 抑制 A→B→A 式频繁切换。

    - 滞回: 升级用更严阈值(bias=+up), 降级用更松阈值(bias=-down);
    - 切换确认: 盘中需连续 confirm 次(5分钟窗口)满足, 收盘后单次即定;
    - 熔断式降级: 盘中评分骤降(低于当前评级门槛 crash_score_drop 分)或极端指标时,
      跳过确认立即降级(防恐慌/断崖被确认周期拖住);
    - 单日最多变化1级(限幅);
    - 切换/维持输出明确原因, 供复盘; 当日评级持久化到 grade_history.json。
    参数均来自 settings.decision.grade_hysteresis(可随市场环境调节)。
    """
    _gc = _grade_cfg()
    _up = float(_gc.get("up_bias", _GRADE_UP_BIAS) or _GRADE_UP_BIAS)
    _down = float(_gc.get("down_bias", _GRADE_DOWN_BIAS) or _GRADE_DOWN_BIAS)
    _confirm = int(_gc.get("confirm", _GRADE_CONFIRM) or _GRADE_CONFIRM)
    _step = int(_gc.get("max_step", _GRADE_MAX_STEP) or _GRADE_MAX_STEP)
    _crash = float(_gc.get("crash_score_drop", 10.0) or 10.0)
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
        if abs(rv - lv) > _step:
            direction = 1 if rv > lv else -1
            raw = [g for g, o in order.items() if o == lv + direction][0]
            rv = order[raw]
        if raw == last:
            grade = last
        else:
            # 滞回判定: 升级需更严(bias>0)/降级需更松(bias<0)
            if rv > lv:
                cand = _grade_raw(score, zt, adv_ratio, rules, bias=_up)
                justified = order[cand] > lv
            else:
                cand = _grade_raw(score, zt, adv_ratio, rules, bias=-_down)
                justified = order[cand] < lv
            if not justified:
                grade = last
                change_reason = (f"滞回: 未满足「{'升级' if rv > lv else '降级'}」确认阈值"
                                 f"(需更{'严' if rv > lv else '松'}边界),维持 {last}")
                _GRADE_PENDING.update({"grade": None, "count": 0, "ts": 0.0})
            elif rv < lv and _in_trading_time() and (
                    score < _last_grade_floor(last, rules) - _crash):
                # 熔断式降级: 盘中评分骤降 → 跳过连续确认立即降级
                grade = raw
                change_reason = (f"熔断式降级 {last}→{raw}(评分 {score:.1f} 低于当前评级门槛"
                                 f" {_last_grade_floor(last, rules):.0f}-{_crash:.0f} 分,跳过确认)")
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
                if _GRADE_PENDING["count"] >= _confirm:
                    grade = raw
                    change_reason = f"评级切换 {last}→{raw}(5分钟窗口连续确认)"
                    _GRADE_PENDING.update({"grade": None, "count": 0, "ts": 0.0})
                else:
                    grade = last
                    change_reason = f"评级切换待确认 {last}→{raw}({_GRADE_PENDING['count']}/{_confirm})"

    # 持久化当日评级
    arch[today] = grade
    dal.locked_write(_GRADE_ARCH_FILE, json.dumps(arch, ensure_ascii=False))
    return grade, change_reason


_PERMIT_CACHE = {"t": 0.0, "data": None}
_PERMIT_TTL = 60


def market_permit() -> dict:
    """第一层:市场可操作度评级 + 总仓位上限(60s 记忆, 避免 decision_brief 内重复重算)。"""
    now = time.time()
    if _PERMIT_CACHE["data"] is not None and now - _PERMIT_CACHE["t"] < _PERMIT_TTL:
        return _PERMIT_CACHE["data"]
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
    except Exception as _e:  # noqa: BLE001
        _fault(_e)

    # 2.1 环境自适应权重(趋势市/震荡市/情绪市 → 动态权重, 5日均值平滑)
    weights = _adaptive_weights(_fg_series(fg), closes, amounts)
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
    _W_MODE_TAG = {"trend": "趋势市", "range": "震荡市", "emotion": "情绪市", "neutral": "中性市"}
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
    # 数据质量: 行情日期是否当日 + 关键维度是否缺失(供决策层降权)
    _dq, _dq_note = 1.0, ""
    try:
        _mdate = str(snap.get("market_date") or "")
        _today_s = str(dt.date.today())
        if _mdate and _mdate[:10] < _today_s:
            _dq, _dq_note = 0.55, f"行情日期滞后(快照 {_mdate},今日 {_today_s})"
        elif fg is None or adv_ratio is None or amount_yi is None:
            _dq, _dq_note = 0.65, "关键维度缺失(恐贪/涨跌家数/成交额)"
    except Exception as _e:  # noqa: BLE001
        _fault(_e)
    if _dq < 0.8:
        reasons.append(f"数据质量 {_dq:.2f}:{_dq_note}")
    out = {
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
        "data_quality": round(_dq, 2),
        "data_quality_note": _dq_note,
        "market_phase": phase,
        "phase_label": pcfg["label"],
        "operation_keynote": pcfg["keynote"],
        "date": str(snap.get("market_date") or _today()),
    }
    _PERMIT_CACHE["t"] = time.time()
    _PERMIT_CACHE["data"] = out
    return out


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
    # 涨停不足(每缺1家扣 per_missing; 0家但有趋势 → 豁免降扣防误杀大金融/消费等趋势板块)
    zcfg = vp.get("zt_short", {})
    min_zt = int(dcfg.get("veto", {}).get("min_zt_in_sector", 2))
    if zcfg.get("enabled", True) and zt_available:
        zt_n = int(r.get("zt_count", 0))
        missing = max(0, min_zt - zt_n)
        if missing > 0:
            _zt_trend_exempt = float(zcfg.get("trend_exempt_pct", 3.0) or 3.0)
            _leader_exempt = float(zcfg.get("trend_exempt_leader", 5.0) or 5.0)
            _exempt_pts = float(zcfg.get("trend_exempt_pts", 3.0) or 3.0)
            if (zt_n == 0 and (r.get("pct_chg") or 0) > _zt_trend_exempt
                    and (r.get("leader_pct") or 0) > _leader_exempt):
                # 无涨停但有趋势: 板块强势且领涨股大涨(中军趋势型板块), 只轻扣
                penalty += _exempt_pts
                reasons.append(f"涨停 {zt_n} 家,但板块涨幅 {r.get('pct_chg', 0):+.1f}%>"
                               f"{_zt_trend_exempt:.0f}% 且领涨 {r.get('leader_pct', 0):+.1f}%>"
                               f"{_leader_exempt:.0f}%(无涨停但有趋势),仅扣 {_exempt_pts:.0f} 分")
            else:
                pts = missing * float(zcfg.get("per_missing", 5.0))
                penalty += pts
                reasons.append(f"涨停 {zt_n} 家,不足 {min_zt} 家,扣 {pts:.1f} 分")
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
    """准入线动态调整(3.2): 返回调整值(可为0)。明细见 _sector_admission_adj_detail。"""
    return _sector_admission_adj_detail(name, score)[0]


def _sector_admission_adj_detail(name: str, score: float) -> tuple:
    """准入线动态调整(3.2+): 板块近60日评分分位数(分档) + 波动率 + 防御属性。

    - 分位分档: ≥80%分位→-5 / 60-80%→-2 / 40-60%→0 / 20-40%→+2 / ≤20%→+5;
    - 波动率: 标准差>10→+3 / <5→-3;
    - 防御属性板块(评分天然偏低)→准入线下调 defensive_adj(默认2);
    - 总浮动不超过 ±max_adj(10)。返回 (adj, 调整明细字符串)。
    """
    aacfg = _cfg().get("mainline", {}).get("admission_adj", {})
    if not aacfg.get("enabled", True):
        return 0.0, ""
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
        return 0.0, ""
    if len(hist) < int(aacfg.get("min_hist", 10)):
        return 0.0, ""
    pct = sum(1 for v in hist if v < score) / len(hist)
    m = sum(hist) / len(hist)
    std = (sum((v - m) ** 2 for v in hist) / len(hist)) ** 0.5
    adj = 0.0
    notes = []
    _pts = float(aacfg.get("pct_pts", 5.0))
    _mid = float(aacfg.get("pct_mid_pts", 2.0))
    _pu, _pum = float(aacfg.get("pct_up", 0.80)), float(aacfg.get("pct_up_mid", 0.60))
    _pd, _pdm = float(aacfg.get("pct_down", 0.20)), float(aacfg.get("pct_down_mid", 0.40))
    if pct >= _pu:
        adj -= _pts
        notes.append(f"历史{pct:.0%}分位-{_pts:.0f}")
    elif pct >= _pum:
        adj -= _mid
        notes.append(f"历史{pct:.0%}分位-{_mid:.0f}")
    elif pct <= _pd:
        adj += _pts
        notes.append(f"历史{pct:.0%}分位+{_pts:.0f}")
    elif pct <= _pdm:
        adj += _mid
        notes.append(f"历史{pct:.0%}分位+{_mid:.0f}")
    if std > float(aacfg.get("vol_high", 10.0)):
        adj += float(aacfg.get("vol_pts", 3.0))
        notes.append(f"高波动+{float(aacfg.get('vol_pts', 3.0)):.0f}")
    elif std < float(aacfg.get("vol_low", 5.0)):
        adj -= float(aacfg.get("vol_pts", 3.0))
        notes.append(f"低波动-{float(aacfg.get('vol_pts', 3.0)):.0f}")
    # 防御板块属性: 评分天然偏低, 准入线下调(退潮期也能发挥防御作用)
    _def_adj = float(aacfg.get("defensive_adj", 2.0) or 0.0)
    if _def_adj and _sector_pool(name) == "defensive":
        adj -= _def_adj
        notes.append(f"防御属性-{_def_adj:.0f}")
    adj = round(max(-float(aacfg.get("max_adj", 10.0)),
                    min(float(aacfg.get("max_adj", 10.0)), adj)), 1)
    return adj, "、".join(notes) + f",净调整{adj:+.1f}" if notes else ""


# ---------------------------------------------------------------- 3.4 风格偏转分数微调
def _style_score_adj(style: dict, size_bias, scale: float = 1.0) -> float:
    """风格偏转分数微调(3.4): 与当前市场风格对齐 +1~3 分,背离 -1~3 分(强度定标),单板块上限±5。

    强度: |大小盘动量差| 1%~3% → 1~3 分;style_confidence=low 缩放 0.5(连续一致性不足时降权)。
    scale: 弱市(无达标主线, 板块分聚集在观察线附近)时由调用方传入 0.5,
    避免 3 分偏转在 52-59 聚集区主导观察池归属(对齐任务约束"避免风格因素主导")。
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
    delta = aligned * strength * scale
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
    except Exception as _e:  # noqa: BLE001
        _fault(_e)
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
    # 弱市判定: 最高非淘汰板块评分 < 准入线 → 风格偏转降权(避免3分在低分聚集区主导)
    _top_score = max((r.get("score") or 0) for r in rows if r.get("level") != "rejected") if rows else 0
    _style_scale = 0.5 if _top_score < admission else 1.0
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
        # 3.4 风格偏转分数微调(与当前大小盘风格对齐 +分/背离 -分, 弱市降权)
        if ext_cfg and style:
            s_adj = _style_score_adj(style, r.get("size_bias", 0), scale=_style_scale)
            if s_adj:
                item["style_raw_score"] = item["score"]   # 调整前(扣分/量价修正后)原始分
                item["score"] = round(item["score"] + s_adj, 2)
                item["style_adj"] = s_adj
        # 3.2 准入线动态调整(板块历史分位+波动率+防御属性, 带调整明细透明化)
        admission_adj, _adj_note = _sector_admission_adj_detail(r["industry"], item["score"])
        eff_admission = round(admission + admission_adj, 1)
        if admission_adj:
            item["admission_adj"] = admission_adj
            item["admission_adj_note"] = _adj_note
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
                _sraw = item.get("style_raw_score")
                item["reasons"].append(
                    f"风格偏转 {'对齐' if item['style_adj'] > 0 else '背离'} {item['style_adj']:+.1f} 分"
                    + (f"({_sraw:.1f}→{item['score']:.1f})" if _sraw is not None else ""))
            if item.get("admission_adj_note"):
                item["reasons"].append(f"准入线调整: {item['admission_adj_note']}(生效线 {eff_admission:.0f})")
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
                               + (f"(板块动态{admission_adj:+.1f}:{_adj_note})" if admission_adj else "")
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
# 统一 5 档操作信号口径/信号位移/触发模板已迁至 app.support.signals(顶部导入)


def _adjust_signal(item: dict, sector_level: str) -> dict:
    """多维信号修正(4.4): 板块等级 + 位置 + 市场阶段 + 量价 + 技术形态 + 资金流。

    累计上修/下修不超过 max_delta 档(默认±2);每项修正输出原因与数据支撑。
    item 需含 action_key / ret3d / levels / pct_chg / volume_ratio;
    修改 item["signal"]/item["action"]/item["adj_notes"]。
    """
    dcfg = _cfg()
    scfg = dcfg.get("signal", {})
    if not scfg.get("enabled", True) or item.get("error"):
        return item
    base = _ACTION_TO_SIGNAL.get(item.get("action_key"), "观望")
    sig = base
    notes = []
    _MAX = int(scfg.get("max_delta", 2) or 2)

    def _adj(d, why):
        nonlocal sig
        if not d:
            return
        sig = _shift_signal(sig, d)
        notes.append(f"{why}({'上修' if d > 0 else '下修'} {abs(d)} 档)")

    # 1) 板块等级修正:核心主攻上修 1 档
    boost = scfg.get("sector_boost", {}).get(sector_level, 0)
    if boost:
        _adj(boost, f"核心主线溢价 {sector_level}")

    # 2) 位置修正:低位启动上修 / 短期高位下修
    low_pos = scfg.get("low_pos_ret3d", 0.05)
    high_pos = scfg.get("high_pos_ret3d", 0.15)
    ret3d = item.get("ret3d")
    if ret3d is not None:
        if ret3d < low_pos and ret3d > 0:
            _adj(1, f"低位启动(近3日 {ret3d:+.1%})")
        elif ret3d >= high_pos:
            _adj(-1, f"短期高位(近3日 {ret3d:+.1%})")

    # 3) 市场阶段修正:退潮期整体下修1档;主升期核心板块上修1档
    try:
        _phase = get_market_phase()
        _phase_label = phase_cfg(_phase).get("label", _phase)
    except Exception:  # noqa: BLE001
        _phase, _phase_label = "main", "主升发酵期"
    padj = scfg.get("phase_adjust", {})
    pd = int(padj.get(_phase, 0) or 0)
    if pd:
        _adj(pd, f"市场阶段「{_phase_label}」")
    if _phase == "main" and sector_level == "core":
        b2 = int(padj.get("main_core_boost", 0) or 0)
        if b2:
            _adj(b2, f"主升期核心板块 {sector_level}")

    # 4) 量价配合修正:放量上涨上修/缩量上涨下修/放量下跌下修2档
    vp = scfg.get("vol_price", {})
    if vp.get("enabled", True):
        vr = item.get("volume_ratio")
        pct = item.get("pct_chg")
        if vr is not None and pct is not None:
            if pct > 0 and vr >= 1.05:
                _adj(int(vp.get("up_vol", 1)), f"放量上涨(量比 {vr:.2f})")
            elif pct > 0 and vr < 0.95:
                _adj(int(vp.get("up_shrink", -1)), f"缩量上涨(量比 {vr:.2f})")
            elif pct < 0 and vr >= 1.05:
                _adj(int(vp.get("down_vol", -2)), f"放量下跌(量比 {vr:.2f})")

    # 5) 技术形态修正: 突破压力/跌破支撑 + 支撑/压力"临近"(低吸/谨慎)信号
    tech = scfg.get("technical", {})
    if tech.get("enabled", True):
        lv = item.get("levels") or {}
        price = item.get("price")
        _np = float(tech.get("near_pct", 0.02) or 0.02)
        if price and lv.get("resistance") and float(lv["resistance"]) > 0:
            _res = float(lv["resistance"])
            if price >= _res:
                _adj(int(tech.get("break_up", 1)), f"突破压力位 {_res}")
            elif price >= _res * (1 - _np):
                _adj(int(tech.get("near_resistance", -1)),
                     f"逼近压力位(现价 {price:.2f} 距 {_res:.2f} {(1 - price / _res) * 100:.1f}%)")
        if price and lv.get("support") and float(lv["support"]) > 0:
            _sup = float(lv["support"])
            if price <= _sup:
                _adj(int(tech.get("break_down", -2)), f"跌破支撑位 {_sup}")
            elif price <= _sup * (1 + _np):
                _adj(int(tech.get("near_support", 1)),
                     f"回踩支撑位(现价 {price:.2f} 距 {_sup:.2f} {(price / _sup - 1) * 100:.1f}%)")

    # 5b) 技术指标形态修正(RSI/MACD/KDJ/布林带 → 信号层; 矛盾以模型集成为准)
    tir = scfg.get("tech_signal", {})
    if tir.get("enabled", True):
        t = item.get("tech") or {}
        if t:
            _rsi = t.get("rsi")
            _macd = t.get("macd")
            _kdj = t.get("kdj") or {}
            _bb = t.get("bb_pos")
            _kg = _kdj.get("k") is not None and _kdj.get("d") is not None and _kdj["k"] > _kdj["d"]
            _kd = _kdj.get("k") is not None and _kdj.get("d") is not None and _kdj["k"] < _kdj["d"]
            if (_rsi is not None and _rsi < 35 and _macd == "金叉") or (_kg and _kdj.get("j") is not None and _kdj["j"] < 20):
                _adj(int(tir.get("oversold_gold", 1)), "超卖+金叉(技术面低吸机会)")
            elif (_rsi is not None and _rsi > 70 and _macd == "死叉") or (_kd and _kdj.get("j") is not None and _kdj["j"] > 80):
                _adj(int(tir.get("overbought_dead", -1)), "超买+死叉(技术面兑现压力)")
            if _bb is not None:
                if _bb > 0.9:
                    _adj(int(tir.get("bb_over", -1)), f"突破布林上轨(位置 {_bb:.2f}),超买回归风险")
                elif _bb < 0.1:
                    _adj(int(tir.get("bb_under", 1)), f"跌破布林下轨(位置 {_bb:.2f}),超卖反弹机会")

    # 6) 资金流修正:近3日连续流入(近似)/连续流出
    fund = scfg.get("fund_flow", {})
    if fund.get("enabled", True):
        try:
            from app.data.fetcher import _load_cache as _flc
            df = _flc(str(item.get("code") or "").zfill(6))
            if df is not None and len(df) >= 5 and "close" in df.columns:
                rets = df["close"].astype(float).pct_change().dropna().tail(3)
                if len(rets) == 3 and float(rets.min()) > 0:
                    _adj(int(fund.get("up", 1)), "近3日资金连续流入(连续收涨)")
                elif len(rets) == 3 and float(rets.max()) < 0:
                    _adj(int(fund.get("down", -1)), "近3日资金连续流出(连续收跌)")
        except Exception as _e:  # noqa: BLE001
            _fault(_e)

    # 累计修正限幅 ±max_delta 档
    b_idx = _SIGNAL_RANK.get(base, 0)
    f_idx = _SIGNAL_RANK.get(sig, 0)
    if f_idx - b_idx > _MAX:
        sig = _SIGNAL_LEVELS[b_idx + _MAX]
        notes.append(f"累计修正已达上限(+{_MAX}档)")
    elif b_idx - f_idx > _MAX:
        sig = _SIGNAL_LEVELS[b_idx - _MAX]
        notes.append(f"累计修正已达下限(-{_MAX}档)")

    item["signal"] = sig
    item["action"] = sig
    item["adj_notes"] = notes if scfg.get("note", True) else []
    return item


def _tech_indicators(df) -> dict:
    """技术指标(4.3): RSI14 / MACD(金叉死叉) / KDJ / 布林带位置。数据不足返回 {}。"""
    out = {}
    try:
        c = df["close"].astype(float)
        if len(c) < 30 or "low" not in df.columns or "high" not in df.columns:
            return out
        diff = c.diff()
        up = diff.clip(lower=0).rolling(14).mean()
        dn = (-diff.clip(upper=0)).rolling(14).mean()
        rs = up / dn.replace(0, 1e-9)
        rsi = 100 - 100 / (1 + rs)
        if rsi.notna().iloc[-1]:
            out["rsi"] = round(float(rsi.iloc[-1]), 1)
        ema12 = c.ewm(span=12, adjust=False).mean()
        ema26 = c.ewm(span=26, adjust=False).mean()
        dif = ema12 - ema26
        dea = dif.ewm(span=9, adjust=False).mean()
        out["macd"] = "金叉" if float(dif.iloc[-1]) > float(dea.iloc[-1]) else "死叉"
        lo = df["low"].astype(float)
        hi = df["high"].astype(float)
        rsv = (c - lo.rolling(9).min()) / (hi.rolling(9).max() - lo.rolling(9).min()).replace(0, 1e-9)
        k = rsv.ewm(com=2).mean()
        d = k.ewm(com=2).mean()
        j = 3 * k - 2 * d
        out["kdj"] = {"k": round(float(k.iloc[-1]), 1), "d": round(float(d.iloc[-1]), 1),
                      "j": round(float(j.iloc[-1]), 1)}
        ma20 = c.rolling(20).mean()
        sd = c.rolling(20).std()
        if sd.notna().iloc[-1] and float(sd.iloc[-1]) > 0:
            out["bb_pos"] = round(float((c.iloc[-1] - (ma20.iloc[-1] - 2 * sd.iloc[-1]))
                                        / max(4 * float(sd.iloc[-1]), 1e-9)), 2)
    except Exception as _e:  # noqa: BLE001
        _fault(_e)
    return out


def _ensemble_vote(tech: dict, pred: dict) -> dict:
    """规则-模型集成投票(4.3): GBM 方向 + 技术指标(RSI/MACD/KDJ/BB) + 市场环境加权投票。

    不替换 GBM 主判: 一致时保持原 p_up, 分歧时按集成方向轻度调权(±0.03)并注解。
    """
    gbm_up = float(pred.get("p_up") or 0.5)
    gbm_dir = 1 if gbm_up >= 0.55 else (-1 if float(pred.get("p_down") or 0) >= 0.55 else 0)
    votes = [("GBM", gbm_dir, 0.5)]
    if tech.get("rsi") is not None:
        tdir = 1 if tech["rsi"] >= 55 else (-1 if tech["rsi"] <= 45 else 0)
        votes.append(("RSI", tdir, 0.2))
    if tech.get("macd") == "金叉":
        votes.append(("MACD", 1, 0.15))
    elif tech.get("macd") == "死叉":
        votes.append(("MACD", -1, 0.15))
    kdj = tech.get("kdj") or {}
    if kdj.get("k") is not None and kdj.get("d") is not None:
        if kdj["k"] > kdj["d"] and kdj["j"] > 80:
            votes.append(("KDJ", 1, 0.1))
        elif kdj["k"] < kdj["d"] and kdj["j"] < 20:
            votes.append(("KDJ", -1, 0.1))
    if tech.get("bb_pos") is not None:
        votes.append(("BB", 1 if tech["bb_pos"] >= 0.8 else (-1 if tech["bb_pos"] <= 0.2 else 0), 0.1))
    env_dir = 0
    try:
        _g = market_permit().get("grade")
        env_dir = 1 if _g in ("A", "B") else (-1 if _g in ("C", "D") else 0)
    except Exception as _e:  # noqa: BLE001
        _fault(_e)
    votes.append(("ENV", env_dir, 0.2))
    wsum = sum(w for _, d, w in votes if d != 0)
    ssum = sum(d * w for _, d, w in votes)
    ens_dir = 0 if wsum <= 0 else (1 if ssum / wsum >= 0.3 else (-1 if ssum / wsum <= -0.3 else 0))
    agree = (ens_dir == 0) or (gbm_dir == ens_dir)
    return {"agree": agree, "ensemble_dir": ens_dir,
            "p_up_adj": round(gbm_up + 0.03 * ens_dir, 4) if not agree else round(gbm_up, 4),
            "note": f"集成投票 {'一致' if agree else '分歧'}: " + "/".join(v[0] for v in votes)}


def _predict_one(code, predictor, quotes, market):
    try:
        df, pred, adv = _one(code, predictor, quotes, market, _st.load())
        lv = adv.get("levels") or {}
        close = df["close"]
        ret3d = float(close.iloc[-1] / close.iloc[-4] - 1) if len(close) >= 4 else None
        ma5 = float(close.rolling(5).mean().iloc[-1]) if len(close) >= 5 else None
        ma10 = float(close.rolling(10).mean().iloc[-1]) if len(close) >= 10 else None
        # 4.3 技术指标 + 量能比 + 集成投票注解 + 模型监控
        tech = _tech_indicators(df)
        vr = None
        if len(df) >= 20 and "volume" in df.columns:
            v = df["volume"].astype(float)
            base = float(v.iloc[:-1].tail(19).mean())
            if base > 0:
                vr = round(float(v.iloc[-1] / base), 3)
        out = {
            "p_up": round(pred["p_up"], 4), "p_flat": round(pred["p_flat"], 4),
            "p_down": round(pred["p_down"], 4), "direction": pred["direction_cn"],
            "action": adv["action_cn"], "action_key": adv["action"],
            "levels": lv,
            "atr14": (adv.get("technical") or {}).get("atr14"),
            "ret3d": round(ret3d, 4) if ret3d is not None else None,
            "ma5": round(ma5, 2) if ma5 is not None else None,
            "ma10": round(ma10, 2) if ma10 is not None else None,
            "volume_ratio": vr,
            "reasons": adv.get("reasons", [])[:3],
        }
        if tech:
            out["tech"] = tech
        try:
            tmc = _st.load().get("target_match") or {}
            if tmc.get("enable_model_ensemble"):
                out["ensemble"] = _ensemble_vote(tech, out)
            if (tmc.get("model_monitor") or {}).get("enabled"):
                from app.support import model_monitor as _mm
                _mm.record(code, out)
        except Exception as _e:  # noqa: BLE001
            _fault(_e)
        from app.support import fault as _flt
        _flt.record_success("model.predict")
        return out
    except Exception as e:  # noqa: BLE001
        from app.support import fault as _flt
        _flt.record_failure("model.predict", str(e))
        _flt.error("model.predict", f"模型预测失败 {code},降级为技术面规则替代", exc=e,
                   context={"code": code})
        # 6.1 模型不可用 → 技术面规则替代(RSI/MACD/KDJ/BB 定方向)
        try:
            from app.data.fetcher import get_daily_history as _gdh
            df = _gdh(code)
            tech = _tech_indicators(df)
            up = 0
            if tech.get("rsi") is not None and tech["rsi"] >= 55:
                up += 1
            if tech.get("macd") == "金叉":
                up += 1
            if (tech.get("kdj") or {}).get("j") is not None and tech["kdj"]["j"] > 80:
                up += 1
            if tech.get("bb_pos") is not None and tech["bb_pos"] >= 0.8:
                up += 1
            direction = "上涨" if up >= 2 else ("下跌" if up == 0 and tech else "震荡")
            close = df["close"].astype(float)
            ret3d = float(close.iloc[-1] / close.iloc[-4] - 1) if len(close) >= 4 else None
            return {
                "p_up": round(0.6 if direction == "上涨" else (0.3 if direction == "震荡" else 0.2), 4),
                "p_flat": 0.3, "p_down": round(0.2 if direction == "上涨" else (0.4 if direction == "震荡" else 0.6), 4),
                "direction": direction, "action": "观望", "action_key": "wait",
                "levels": {}, "atr14": None, "ret3d": round(ret3d, 4) if ret3d is not None else None,
                "ma5": None, "ma10": None, "volume_ratio": None, "tech": tech,
                "reasons": ["模型不可用,技术面规则替代"],
                "degraded": True, "degrade_reason": "模型不可用,技术面规则替代",
            }
        except Exception as e2:  # noqa: BLE001
            return {"error": f"{type(e).__name__}: {e}; 技术面替代失败: {type(e2).__name__}"}


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

    # 6.x 并行化 _predict_one(多标的并发预测, 加速板块成分多场景)
    from concurrent.futures import ThreadPoolExecutor
    _pairs = [(role, c, rank) for role, cands in (("aggressive", emo), ("steady", mid))
              for rank, c in enumerate(cands[:2], 1)]
    if _pairs:
        with ThreadPoolExecutor(max_workers=min(4, len(_pairs))) as _ex:
            _preds = list(_ex.map(lambda pc: _predict_one(pc[1]["code"], predictor, quotes, market),
                                  _pairs))
    else:
        _preds = []
    for (role, c, rank), pred in zip(_pairs, _preds):
        item = _base(c)
        item["rank"] = rank
        item["role"] = {"aggressive": "情绪龙头", "steady": "中军龙头"}[role]
        item.update(pred)
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
                   sector_used_pct: float = 0.0, sector_cap_pct: float = None,
                   data_quality: float = None) -> dict:
    """6.1 执行参数计算守卫: 异常 → 保守输出(空仓建议) + "计算异常" 标注。"""
    try:
        return _execution_plan_impl(target, total_asset, taste, market_cap, single_cap,
                                    grade, asset_type, sector_used_pct, sector_cap_pct,
                                    data_quality)
    except Exception as e:  # noqa: BLE001
        from app.support import fault as _flt
        _flt.error("execution_plan", f"执行参数计算异常 {target.get('code')}", exc=e,
                   context={"code": target.get("code"), "name": target.get("name")})
        _flt.record_failure("execution_plan", str(e))
        return {"ok": False, "name": target.get("name"), "code": target.get("code"),
                "reason": f"计算异常,建议观望/空仓({type(e).__name__})",
                "degraded": True, "degrade_reason": "执行参数计算异常"}


def _execution_plan_impl(target: dict, total_asset: float, taste: str,
                         market_cap: float = None, single_cap: float = None,
                         grade: str = None, asset_type: str = None,
                         sector_used_pct: float = 0.0, sector_cap_pct: float = None,
                         data_quality: float = None) -> dict:
    """第四层:单标的精确执行参数(ATR止损 / 分批建仓 / 目标价 / 仓位)。

    数据质量联动(最小方案):
    - data_quality < 0.5: 拒绝该数据驱动决策, 输出观望;
    - 0.5 <= data_quality < 0.8: 仓位 ×0.8 并标注「数据质量一般」。

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
    if data_quality is not None and data_quality < 0.5:
        # 数据质量过低: 不使用该数据驱动决策, 回退观望(最小方案)
        return {"ok": False, "name": target.get("name"), "code": target.get("code"),
                "reason": f"数据质量过低({data_quality:.2f}),建议观望/使用最近有效值",
                "degraded": True, "degrade_reason": f"数据质量过低({data_quality:.2f})"}
    _dq_factor = 1.0
    _dq_note = ""
    if data_quality is not None and data_quality < 0.8:
        _dq_factor = 0.8
        _dq_note = f";数据质量一般({data_quality:.2f}),仓位×{_dq_factor}"
    lv = target.get("levels") or {}
    atr = target.get("atr14")
    atr = float(atr) if atr else None
    ma5 = target.get("ma5")
    ma10 = target.get("ma10")
    eparam = dcfg.get("exec_param", {})
    # 标的类型归一(供止损/分批/目标分类型参数)
    _atype = asset_type or ("mood" if target.get("type") == "aggressive" else
                            "repair" if target.get("type") == "repair" else
                            "etf" if target.get("type") == "etf" else "mid")
    if _atype in ("etf", "def_etf"):
        _atype = "etf"

    # ---- 5.1 止损:VectorBT 止损位优先,缺失回退「分标的类型」ATR/现价比例
    scfg = eparam.get("stop", {}).get(_atype, {"atr": 1.5, "pct": 0.05})
    stop = float(lv.get("stop_loss") or 0) or \
        (price - (float(scfg.get("atr", 1.5)) * atr if atr else price * float(scfg.get("pct", 0.05))))
    if stop >= price:
        stop = price - (float(scfg.get("atr", 1.5)) * atr if atr else price * float(scfg.get("pct", 0.05)))
    # 5.1 止损位验证: 必须低于关键支撑位, 否则调整至支撑位下方
    support0 = float(lv.get("support") or 0) or price * 0.95
    if support0 and stop >= support0:
        stop = support0 * 0.98

    # ---- 5.3 目标价: 分类型 目标1/2/3 + 评级动态调整 + 递增校验
    tcfg = eparam.get("target", {}).get(_atype, {"atr1": 0.5, "t2": "res20", "t3": "hist_high"})
    gd = eparam.get("target_dynamic", {})
    gmult = 1.0
    if grade in ("A",):
        gmult = float(gd.get("grade_up", 1.05))
    elif grade in ("C", "D"):
        gmult = float(gd.get("grade_down", 0.95))
    try:
        from app.data.fetcher import _load_cache as _flc
        _df = _flc(str(target.get("code") or "").zfill(6))
    except Exception:  # noqa: BLE001
        _df = None
    def _hist_high(days):
        if _df is not None and len(_df) and "high" in _df.columns:
            h = _df["high"].astype(float).tail(days)
            if len(h):
                return float(h.max())
        return None
    resistance = float(lv.get("resistance") or 0) or price * 1.08
    target1 = price * (1 + float(tcfg.get("atr1", 0.5)) * (atr / price if atr else 0.04))
    target1 = max(target1, price * (1 + plancfg.get("target1_min_gain", 0.03)))  # 至少+3%
    target1 = max(target1, price * 1.001) * gmult   # 评级动态调整
    _t_src = {"prev_high": _hist_high(20), "hist_high": _hist_high(60),
              "year_high": _hist_high(250), "res20": resistance}
    target2 = _t_src.get(tcfg.get("t2", "res20")) or resistance
    target3 = (_t_src.get(tcfg.get("t3", "hist_high")) if tcfg.get("t3") else None) or target2
    target2 = (target2 * gmult) if target2 else None
    target3 = (target3 * gmult) if target3 else None
    # 递增校验: 目标1<目标2<目标3 且均高于现价(达标后目标2上移 ×run_mult 让利润奔跑)
    _run = float(gd.get("run_mult", 1.05))
    if target2 is None or target2 <= target1:
        target2 = target1 * _run
    if target3 is None or target3 <= target2:
        target3 = target2 * _run
    _td_note = (f"目标价按评级{'上' if gmult > 1 else ('下' if gmult < 1 else '平')}调"
                f"{'×' + format(gmult, '.2f') if gmult != 1.0 else ''};达标后目标2上移×{format(_run, '.2f')}")

    # ---- 5.2 分批比例: 分标的类型(可三批) × 分阶段首批系数
    bcfg = eparam.get("batch_type", {})
    ratios = list(bcfg.get(_atype, [0.50, 0.50]) or [0.50, 0.50])
    if len(ratios) < 2:
        ratios = [0.5, 0.5]
    pfirst = float(eparam.get("batch_phase_first", {}).get(pcfg["phase"], 1.0) or 1.0)
    if pfirst > 0:
        ratios[0] = ratios[0] * pfirst
        _s = sum(ratios)
        ratios = [r / _s for r in ratios]
    _BT_NAME = {"mood": "情绪龙头", "mid": "中军龙头", "etf": "ETF", "repair": "补涨优选"}
    _btrig = ["首批:满足基础买入条件(信号触发/回踩企稳)",
              "二批:首批浮盈>0 或 回踩支撑位企稳",
              "三批:二批浮盈>0 或 突破关键压力位"]

    # ---- 分批模式(与触发条件强绑定)
    mode = plancfg.get("mode", "auto")
    if mode == "auto":
        sig = target.get("signal")
        ret3d = target.get("ret3d") or 0
        mode = "breakout" if (sig in ("突破跟进", "关注低吸") and ret3d >= 0.05) else "pullback"
    if mode == "breakout":
        # 突破跟进:首批=压力位突破价,二批=回踩确认价,三批=突破确认加仓(三批类型)
        first_price = resistance
        second_price = first_price * (1 - 0.01)
        third_price = first_price * (1 + 0.02) if len(ratios) >= 3 else None
        deep_support = float(lv.get("support") or 0) or price * 0.92
        mode_note = "突破跟进:首批放量突破压力位,二批回踩确认" \
            + ("+三批突破确认加仓" if third_price else "")
        trigger = f"放量突破压力位 {first_price:.2f} 确认,回踩 {second_price:.2f} 不破再关注"
        first_note = f"首批:突破压力位({ratios[0]:.0%})"
        second_note = f"二批:回踩确认({ratios[1]:.0%})"
        third_note = f"三批:突破确认({ratios[2]:.0%})" if third_price else ""
    else:
        # 回踩低吸:支撑上沿(5日线附近) -> 支撑下沿(10日线附近) -> 中期强支撑(三批类型)
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
        third_price = min(deep_support, second_price) if len(ratios) >= 3 else None
        mode_note = "回踩低吸:首批5日线附近,二批10日线附近" \
            + ("+三批中期强支撑" if third_price else "")
        trigger = f"回踩支撑区间 {second_price:.2f}~{first_price:.2f} 缩量企稳"
        first_note = f"首批:回踩支撑上沿({ratios[0]:.0%})"
        second_note = f"二批:回踩支撑下沿({ratios[1]:.0%})"
        third_note = f"三批:中期强支撑({ratios[2]:.0%})" if third_price else ""
    # 逐级方向强制校验:回踩二批低于一批,突破二批低于一批但整体在压力位上方
    if first_price <= 0 or second_price <= 0:
        first_price, second_price = price, price * 0.97
        third_price = min(deep_support, second_price) if len(ratios) >= 3 else None

    # ---- 仓位股数(风险公式,三者自洽;以加权买入成本为基准, 支持三批)
    prices = [first_price, second_price] + ([third_price] if third_price else [])
    avg_cost = sum(r * p for r, p in zip(ratios, prices))
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
    if _dq_factor < 1.0:
        max_mv = max_mv * _dq_factor   # 数据质量一般: 仓位 ×0.8

    # ---- 5.4 板块集中度: 单板块总仓位上限(主升放宽40%), 超出则压缩 + 预警
    pm_block = pm if use_matrix else {}
    conc = eparam.get("concentration", {}) or {}
    if pm_block and sector_cap_pct is None:
        sector_cap_pct = pm.get("sector_cap", {}).get(grade or "B", 0.20)
    if conc.get("enabled"):
        _s_cap = float(conc.get("single_sector_main", 0.40)) if pcfg["phase"] == "main" \
            else float(conc.get("single_sector", 0.30))
        sector_cap_pct = min(sector_cap_pct or _s_cap, _s_cap)
    block_note = ""
    if sector_cap_pct and pm.get("enforce", True):
        remaining = max(0.0, sector_cap_pct - (sector_used_pct or 0.0))
        max_blk = total_asset * remaining
        max_mv = min(max_mv, max_blk)
        block_note = f";板块集中度已用 {sector_used_pct or 0:.1%},上限 {sector_cap_pct:.0%},本票最多 {remaining:.1%}"

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
    first = int(shares * ratios[0] // 100) * 100
    second = int(shares * ratios[1] // 100) * 100
    third = max(0, shares - first - second)
    max_loss = shares * loss_per_share

    # ---- 分阶段单次新增/加仓上限(硬约束, 开仓前拦截)
    if pcfg["add_cap"] > 0:
        add_mv = total_asset * pcfg["add_cap"]
        if pos_value > add_mv:
            pos_value = add_mv
            shares = int(pos_value / avg_cost // 100) * 100
            shares = max(shares, 100)
            pos_value = shares * avg_cost
            first = int(shares * ratios[0] // 100) * 100
            second = int(shares * ratios[1] // 100) * 100
            third = max(0, shares - first - second)
            max_loss = shares * loss_per_share

    # ---- 止损阶段化(退潮收紧20% / 主升放宽20% / 高潮收紧), 且不得高于现价
    stop = min(stop * pcfg["stop_adj"], price * 0.98)

    # ---- 5.1 动态止损阶梯(基于浮盈阈值, 按标的类型差异化): 保本 → 锁定浮盈 → 跟踪回撤
    ds = eparam.get("dynamic_stop", {}) or {}
    _dscfg = dict(ds.get(_atype) or ds.get("mid") or {})
    _ds_defaults = {"breakeven_pct": 0.05, "lock_pct": 0.10, "trail_pct": 0.20, "trail_drawdown": 0.08}
    _dscfg = {**_ds_defaults, **_dscfg}
    dynamic_stop = None
    if ds.get("enabled", True):
        dynamic_stop = {
            "breakeven": {"threshold_pct": float(_dscfg.get("breakeven_pct", 0.05)),
                          "stop": round(avg_cost, 2)},
            "lock": {"threshold_pct": float(_dscfg.get("lock_pct", 0.10)),
                     "stop": round(avg_cost * (1 + float(_dscfg.get("breakeven_pct", 0.05))), 2)},
            "trailing": {"threshold_pct": float(_dscfg.get("trail_pct", 0.20)),
                         "trail_drawdown": float(_dscfg.get("trail_drawdown", 0.08))},
        }

    # ---- 5.4 风险预算: 单只标的风险(仓位×止损幅度) ≤ 总资金 single_pct
    rb = eparam.get("risk_budget", {})
    rb_note = ""
    if rb.get("enabled"):
        stop_pct = loss_per_share / avg_cost if avg_cost > 0 else 0.0
        max_single_risk = total_asset * float(rb.get("single_pct", 0.015))
        if stop_pct > 0:
            risk_cap_mv = max_single_risk / stop_pct
            if pos_value > risk_cap_mv:
                pos_value = risk_cap_mv
                shares = int(pos_value / avg_cost // 100) * 100
                shares = max(shares, 100)
                pos_value = shares * avg_cost
                first = int(shares * ratios[0] // 100) * 100
                second = int(shares * ratios[1] // 100) * 100
                third = max(0, shares - first - second)
                max_loss = shares * loss_per_share
                rb_note = (f";单票风险预算(止损幅度 {stop_pct:.1%}×仓位≤{max_single_risk:,.0f}元)"
                           f"压缩至 {pos_value:,.0f} 元")

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
        "target3": round(target3, 2),
        "target_note": _td_note,
        "stop_type": _BT_NAME.get(_atype, _atype),
        "dynamic_stop": dynamic_stop,
        "position_value": round(pos_value, 2),
        "shares": shares,
        "position_pct": round(pos_value / total_asset, 4) if total_asset else 0,
        "max_loss": round(max_loss, 2),
        "batch": {
            "first": {"ratio": ratios[0], "shares": first,
                      "price": round(first_price, 2), "note": first_note},
            "second": {"ratio": ratios[1], "shares": second,
                       "price": round(second_price, 2), "note": second_note},
            "third": {"ratio": ratios[2], "shares": third,
                      "price": round(third_price, 2), "note": third_note}
            if third_price and len(ratios) >= 3 else None,
            "deep_support": round(deep_support, 2),
            "trigger": _btrig[:len(ratios)],
        },
        "note": (f"单笔风险 {risk_money:,.0f} 元(总资金 {risk_rate:.1%}),预计最大亏损 {max_loss:,.0f} 元"
                 f"({max_loss / total_asset:.2%} 占总资金),止损 {stop:.2f}(亏 {loss_per_share:.2f}/股),"
                 f"建议仓位 {shares} 股 / {pos_value:,.0f} 元({pos_value / total_asset:.1%})。{mode_note}"
                 f"{matrix_note}{block_note}{rb_note}{_dq_note}。{_td_note}"),
        "data_quality": data_quality,
    }


# ---------------------------------------------------------------- 5.4 跨标的约束后处理
def _halve_plan(x: dict) -> None:
    """对某计划执行减半压缩并注记。"""
    p = x.get("ref")
    if not p or not p.get("ok"):
        return
    old = p.get("position_pct") or 0
    p["position_pct"] = round(old * 0.5, 4)
    if p.get("position_value"):
        p["position_value"] = round(p["position_value"] * 0.5, 2)
    p.setdefault("risk_check", []).append(f"超限压缩: 仓位减半({old:.1%}→{p['position_pct']:.1%})")
    x["pos_pct"] = p["position_pct"]


def _corr_matrix(codes: list, window: int = 30) -> dict:
    """持仓标的两两收益相关系数(近 window 日, 本地日线缓存)。返回 {(codeA,codeB): corr}。"""
    import pandas as pd
    rets = {}
    for c in set(codes):
        try:
            from app.data.fetcher import _load_cache as _flc
            df = _flc(str(c).zfill(6))
            if df is not None and len(df) > window and "close" in df.columns:
                r = df["close"].astype(float).pct_change().dropna().tail(window)
                if len(r) >= 5:
                    rets[str(c).zfill(6)] = r
        except Exception:  # noqa: BLE001
            continue
    out = {}
    keys = list(rets.keys())
    for i in range(len(keys)):
        for j in range(i + 1, len(keys)):
            a, b = keys[i], keys[j]
            al = pd.concat([rets[a], rets[b]], axis=1, join="inner").dropna()
            if len(al) >= 5:
                c = float(al.iloc[:, 0].corr(al.iloc[:, 1]))
                if c == c:
                    out[(a, b)] = c
    return out


def _risk_postpass(plans: dict, total_asset: float, grade: str, phase: str) -> tuple:
    """5.4 跨标的约束后处理(校验+注记, 超限对最高风险标的温和压缩)。

    - 板块集中度: 前两大板块合计仓位≤50%; 产业链(chain_sectors 配置)合计≤40%;
    - 相关性: 持仓间收益相关>corr_high 的两只合计仓位≤单票上限×sum_mult;
    - 风险预算: Σ(仓位×止损幅度)≤总资金 total_pct, 超限优先压缩风险最高标的。
    返回 (plans, check)。数据不足维度跳过, 不抛错。
    """
    dcfg = _cfg()
    ep = dcfg.get("exec_param", {})
    conc = ep.get("concentration", {}) or {}
    corr_cfg = ep.get("correlation", {}) or {}
    rb = ep.get("risk_budget", {}) or {}
    notes = []
    items = []
    for sector, seg in (plans or {}).items():
        for role, p in (seg or {}).items():
            if not (p and p.get("ok")):
                continue
            avg = p.get("price") or 0
            stop = p.get("stop") or 0
            stop_pct = max(0.0, (avg - stop) / avg) if avg and avg > stop else 0.0
            items.append({"sector": sector, "role": role,
                          "code": str(p.get("code") or "").zfill(6),
                          "name": p.get("name"), "pos_pct": p.get("position_pct") or 0,
                          "stop_pct": stop_pct, "ref": p})
    if not items:
        return plans, {"notes": notes, "total_pos": 0.0, "total_risk": 0.0}
    check = {"notes": notes, "total_pos": round(sum(x["pos_pct"] for x in items), 4),
             "total_risk": round(sum(x["pos_pct"] * x["stop_pct"] for x in items), 4)}
    # 1) 板块集中度: 前两大板块合计 / 产业链合计
    if conc.get("enabled"):
        by_sector = {}
        for x in items:
            by_sector[x["sector"]] = by_sector.get(x["sector"], 0.0) + x["pos_pct"]
        top2 = sum(sorted(by_sector.values(), reverse=True)[:2])
        t2 = float(conc.get("top2_total", 0.50))
        if top2 > t2:
            notes.append(f"前两大板块合计仓位 {top2:.1%} 超集中度上限 {t2:.0%},压缩最高仓位标的")
            _halve_plan(max(items, key=lambda x: x["pos_pct"]))
        chains = [s for s in (conc.get("chain_sectors") or []) if s in by_sector]
        if chains:
            ct = sum(by_sector[s] for s in chains)
            cl = float(conc.get("chain_total", 0.40))
            if ct > cl:
                notes.append(f"产业链板块 {chains} 合计 {ct:.1%} 超上限 {cl:.0%},建议减仓")
    # 2) 相关性
    if corr_cfg.get("enabled") and len(items) >= 2:
        high = float(corr_cfg.get("corr_high", 0.8))
        mult = float(corr_cfg.get("sum_mult", 1.5))
        corr = _corr_matrix([x["code"] for x in items],
                            int(corr_cfg.get("window_days", 30) or 30))
        for i in range(len(items)):
            for j in range(i + 1, len(items)):
                r = corr.get((items[i]["code"], items[j]["code"]))
                if r is None or r <= high:
                    continue
                ssum = items[i]["pos_pct"] + items[j]["pos_pct"]
                if ssum > mult * max(items[i]["pos_pct"], items[j]["pos_pct"]):
                    notes.append(f"「{items[i]['name']}」与「{items[j]['name']}」相关性 {r:.2f}> {high:.1f},"
                                 "合计仓位超限,压缩较小者")
                    _halve_plan(min((items[i], items[j]), key=lambda x: x["pos_pct"]))
    # 3) 风险预算: Σ(仓位×止损幅度) ≤ 总资金 total_pct
    if rb.get("enabled"):
        total = float(rb.get("total_pct", 0.05))
        rsum = sum(x["pos_pct"] * x["stop_pct"] for x in items)
        if rsum > total:
            notes.append(f"总风险 {rsum:.2%} 超预算 {total:.0%},优先压缩风险最高标的")
            for _ in range(10):     # 最多迭代10轮, 防死循环
                rsum = sum(x["pos_pct"] * x["stop_pct"] for x in items)
                if rsum <= total:
                    break
                _mx = max(items, key=lambda x: x["pos_pct"] * x["stop_pct"])
                if _mx["pos_pct"] * _mx["stop_pct"] <= 1e-9:
                    break
                _halve_plan(_mx)
    check["notes"] = notes
    check["total_risk"] = round(sum(x["pos_pct"] * x["stop_pct"] for x in items), 4)
    return plans, check


# ---------------------------------------------------------------- 6.x 术语表与通俗解读
_GLOSSARY = {
    "rr_left": "左侧低吸盈亏比门槛(预期涨幅/止损幅度,不达标不买)",
    "rr_right": "右侧突破盈亏比门槛(None=当前阶段禁止右侧突破)",
    "stab_cycle_adj": "主线稳定器驻留周期系数(越大越谨慎,主线切换越慢)",
    "admission": "主线准入线(板块综合评分须达到才可入选)",
    "cap": "当前市场阶段总仓位上限(占总资金)",
    "single_cap": "单只标的仓位上限(占总资金)",
    "add_cap": "单次新增/加仓上限(0=禁止新增)",
    "tiers": "当前阶段允许的标的三档(中军/情绪/补涨)",
    "trade_mode": "交易模式: 左侧低吸(回踩支撑分批买) / 右侧突破(放量突破后跟进)",
    "stop_adj": "止损幅度阶段化系数(×止损)",
    "lead_margin": "同池替换需超越前任主线的分数幅度",
    "confidence": "主线稳定器置信度(0-1, 越高主线越稳定)",
    "p_up": "模型预测上涨概率",
    "p_down": "模型预测下跌概率",
    "position_coef": "标的三档仓位系数(中军×1.5 / 情绪×0.5 / 补涨×0.6)",
    "stop_loss_pct": "止损幅度(相对买入价)",
    "target_profit_pct": "目标盈利幅度(相对买入价)",
}


def plain_plan(p: dict) -> str:
    """把执行计划的专业字段转成通俗解读(页面展示/复盘用)。"""
    if not p or not p.get("ok"):
        return (p or {}).get("reason") or "无执行计划"
    b = p.get("batch") or {}
    f, s, t3 = b.get("first") or {}, b.get("second") or {}, b.get("third")
    batch_txt = f"分{('2' if not t3 else '3')}批买入: 首批{f.get('ratio', 0):.0%}@{f.get('price')}"
    batch_txt += f"、二批{s.get('ratio', 0):.0%}@{s.get('price')}"
    if t3:
        batch_txt += f"、三批{t3.get('ratio', 0):.0%}@{t3.get('price')}"
    return (f"买入 {p.get('name')}({p.get('code')}),现价 {p.get('price')}。{batch_txt}。"
            f"止损 {p.get('stop')},目标 {p.get('target1')}/{p.get('target2')}/{p.get('target3')}。"
            f"模式: {p.get('trade_mode') or '-'}。"
            + ("".join(f"[{x}]" for x in (p.get("risk_check") or [])) if p.get("risk_check") else ""))


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
    from app.support import fault as _flt
    trace_id = _flt.new_trace_id()
    _flt.info("decision", "决策链路开始(第一层市场许可)", trace_id=trace_id,
              context={"grade": p1["grade"], "phase": p1["market_phase"], "score": p1["score"]})
    # 第四轮改造:主线输出统一经外层防抖稳定器取「stable」稳定结果;
    # 原始流水线结果进 layer2_raw 仅作调试/回测,不再直接驱动今日决策。
    # 惰性导入避免 engine->mainline_stabilizer->engine 的循环依赖。
    # get_output() 优先复用后台定时轮询的最近结果,页面访问不重复抓取数据。
    from app.support import mainline_stabilizer as _stab
    mout = _stab.get_output()
    p2 = mout["stable"]
    core = p2.get("core")
    defensive = p2.get("defensive")
    _flt.info("decision", "第二层主线遴选", trace_id=trace_id,
              context={"core": (core or {}).get("name"),
                       "defensive": (defensive or {}).get("name"),
                       "core_conf": (core or {}).get("confidence"),
                       "degraded": bool((p2 or {}).get("degraded"))})

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
                grade=p1["grade"], asset_type=atype, sector_used_pct=blk_used,
                data_quality=p1.get("data_quality"))
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
                    sector_used_pct=0.0, data_quality=p1.get("data_quality"))
                plans.setdefault(defensive["name"], {})["etf"] = p

    # 5.4 跨标的约束后处理: 板块集中度(前二≤50%/产业链) + 相关性 + 总风险预算
    plans, risk_check = _risk_postpass(plans, total_asset, p1["grade"], p1["market_phase"])
    # 6.x 通俗解读: 每个计划附 explain(供页面/复盘直接展示)
    for _seg in (plans or {}).values():
        for _p in (_seg or {}).values():
            if _p and _p.get("ok") and "explain" not in _p:
                _p["explain"] = plain_plan(_p)
    # 6.x 历史决策效果追踪: 记录当日决策快照(自动复盘统计, 不阻塞)
    try:
        from app.support import decision_tracker as _dt
        _dt.record({"date": p1["date"], "layers": layers, "plans": plans})
    except Exception as _e:  # noqa: BLE001
        _fault(_e)
    _flt.info("decision", "决策链路完成", trace_id=trace_id,
              context={"plans_ok": {s: [r for r, p in (seg or {}).items() if p and p.get("ok")]
                                    for s, seg in plans.items()},
                       "risk": risk_check.get("total_risk"),
                       "health": _flt.global_health()["global_open"]})

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
        "risk_check": risk_check,
        "glossary": _GLOSSARY,
        "trace_id": trace_id,
        "trace": {
            "trace_id": trace_id,
            "layer1": {"grade": p1["grade"], "score": p1["score"],
                       "phase": p1["market_phase"], "reasons": p1["reasons"][-3:]},
            "layer2": {"core": core and core["name"], "defensive": defensive and defensive["name"],
                       "degraded": bool((p2 or {}).get("degraded"))},
            "risk_check": risk_check,
            "health": _flt.global_health(),
        },
        "health": _flt.global_health(),
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
