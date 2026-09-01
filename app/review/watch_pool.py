"""P0-2 明日重点观察标的池(两个子池)。

子池1 主线龙头观察池: 完全复用 `engine.match_level_targets` 分档选股结果——
核心层每板块取情绪龙头(aggressive首档)+中军龙头(steady首档);
发酵层取弹性领涨(aggressive首档);观察层取异动领涨(aggressive首档)。
支撑/压力复用模型 levels 输出,与今日决策页面完全同源。

子池2 有承接的超跌标的池: 仅从三层主线及关联赛道标的中选取(不全市场海选)。
超跌判定: 近10日相对所属板块概念指数超额回撤 >= 15% 且处于近20日相对低位。
承接信号(满足 >=2 项): A 近2日缩量止跌 / B 当日收低位阳线未创新低 /
C 回踩关键均线(MA20)企稳。
设计取舍: 个股主力净流入不在全A快照字段中,若逐股调资金接口会新增外部请求压力,
故第三信号用「均线企稳」代替「当日主力净流入」,并在备注标注口径;
候选池做市值/涨停/ST 过滤并限流(<=80只),kline 走本地缓存,控制数据压力。
"""
import time

from app.review import archive


def _cell(v) -> str:
    return str(v) if v is not None else "-"


def _pct(v) -> str:
    return f"{v * 100:+.1f}%" if v is not None else "-"


def _sector_tiers() -> tuple:
    """返回 (core_sectors, branch_sectors, watch_sectors) 榜单。"""
    from app.support import mainline as _ml
    rows = _ml.sector_scores(use_cache=True) or []
    core, branch, watch = [], [], []
    for r in rows:
        if r.get("level") == "rejected":
            continue
        lv = r.get("level")
        if lv == "core":
            core.append(r)
        elif lv == "branch":
            branch.append(r)
        else:
            watch.append(r)
    return core, branch, watch


# ---------------------------------------------------------------- 子池1 主线龙头
_OBSERVE_NOTE = {
    "aggressive": "开盘承接:竞价低开需快速翻红;缩量回踩支撑可低吸,放量滞涨即兑现",
    "steady": "量能持续性:成交额能否维持放大,作板块强度锚点;缩量回踩支撑企稳可关注",
    "branch": "弹性领涨:关注连板晋级与板块联动,断板即离场,不隔日追高",
    "watch": "异动领涨:次日需放量确认,持续性未验证前仅观察不操作",
}


def _role_of(seg_key: str, level: str) -> str:
    if level == "core":
        return "情绪龙头" if seg_key == "aggressive" else "中军龙头"
    return "弹性领涨" if seg_key == "aggressive" else "异动领涨"


def _rr(it: dict):
    """盈亏比 = (目标 - 买入)/ (买入 - 止损),取 entry_low 为买入参考。"""
    lv = it.get("levels") or {}
    entry = lv.get("entry_low") or lv.get("support")
    tgt, stop = lv.get("target"), lv.get("stop_loss")
    if entry and tgt and stop and (entry - stop) > 0:
        return (tgt - entry) / (entry - stop)
    return None


def _pick(seg: dict) -> dict:
    for it in seg.get("items", []):
        if it.get("error"):
            continue
        return it
    return {}


def _tier_suggest_pos(it: dict, role: str) -> str:
    """建议仓位: 由 tier 差异化仓位系数推导(基准1% × 系数),受单票红线约束。"""
    from app.support import settings as _st
    cap = float((_st.load().get("discipline") or {}).get("single_cap", 0.02) or 0.02)
    coef = float(it.get("position_coef") or 1.0)
    base = {"情绪龙头": 0.005, "中军龙头": 0.01, "补涨优选": 0.006,
            "弹性领涨": 0.005, "异动领涨": 0.005}.get(role, 0.005)
    return f"{min(base * coef, cap) * 100:.1f}%"


def _watch_pool_leader(d: dict) -> list:
    """子池1: 主线龙头观察池(三档梯队: 中军/情绪/补涨)。返回行列表。"""
    core, branch, watch = _sector_tiers()
    from app.decision import engine as _en
    rows_out = []
    _ROLE_TAG = {"steady": "中军龙头", "aggressive": "情绪龙头", "repair": "补涨优选"}
    _NOTE = {"steady": _OBSERVE_NOTE["steady"], "aggressive": _OBSERVE_NOTE["aggressive"],
             "repair": "补涨优选:高低切博弈,低位滞涨+承接企稳后低吸,放量启动确认跟进"}
    # 核心层: 三档配齐(中军为主,情绪为辅,补涨高低切换)
    for r in core[:2]:
        name = r["industry"]
        res = _en._match_targets(name, "core", "core")
        for seg_key, role in (("steady", "中军龙头"), ("aggressive", "情绪龙头"),
                              ("repair", "补涨优选")):
            it = _pick(res.get(seg_key) or {})
            if not it:
                continue
            lv = it.get("levels") or {}
            rows_out.append([name, role, it.get("code"), it.get("name"),
                             _cell(f"{it.get('pct_chg', 0):+.2f}%"),
                             _cell(f"{lv.get('entry_low') or lv.get('support')}~{lv.get('entry_high') or lv.get('resistance')}"),
                             _cell(lv.get("stop_loss")), _cell(lv.get("target")),
                             _cell(f"{_rr(it):.1f}" if _rr(it) else "-"),
                             _tier_suggest_pos(it, role), _NOTE[seg_key]])
    # 发酵层: 情绪标配 + 补涨(符合则)
    for r in branch[:3]:
        name = r["industry"]
        res = _en._match_targets(name, "branch", "branch")
        for seg_key, role in (("aggressive", "弹性领涨"), ("repair", "补涨优选")):
            it = _pick(res.get(seg_key) or {})
            if not it:
                continue
            lv = it.get("levels") or {}
            rows_out.append([name, role, it.get("code"), it.get("name"),
                             _cell(f"{it.get('pct_chg', 0):+.2f}%"),
                             _cell(f"{lv.get('entry_low') or lv.get('support')}~{lv.get('entry_high') or lv.get('resistance')}"),
                             _cell(lv.get("stop_loss")), _cell(lv.get("target")),
                             _cell(f"{_rr(it):.1f}" if _rr(it) else "-"),
                             _tier_suggest_pos(it, role),
                             _OBSERVE_NOTE["branch"] if seg_key == "aggressive" else _NOTE["repair"]])
    # 观察层: 异动领涨
    for r in watch[:3]:
        name = r["industry"]
        res = _en._match_targets(name, "watch", "watch")
        it = _pick(res.get("aggressive") or {})
        if not it:
            continue
        lv = it.get("levels") or {}
        rows_out.append([name, "异动领涨", it.get("code"), it.get("name"),
                         _cell(f"{it.get('pct_chg', 0):+.2f}%"),
                         _cell(f"{lv.get('entry_low') or lv.get('support')}~{lv.get('entry_high') or lv.get('resistance')}"),
                         _cell(lv.get("stop_loss")), _cell(lv.get("target")),
                         _cell(f"{_rr(it):.1f}" if _rr(it) else "-"),
                         _tier_suggest_pos(it, "异动领涨"), _OBSERVE_NOTE["watch"]])
    return rows_out


# ---------------------------------------------------------------- 子池2 超跌承接
def _sector_idx_r10(name: str):
    """所属板块概念指数近10日涨跌幅(缺失返回 None,触发口径降级)。"""
    try:
        from app.features.concept_features import _get_concept_daily
        close = _get_concept_daily(name)["close"].dropna()
        if len(close) >= 11:
            return close.iloc[-1] / close.iloc[-11] - 1
    except Exception:  # noqa: BLE001
        pass
    return None


def _kline(code: str):
    """个股日线(带缓存); 不足20日返回 None。"""
    try:
        from app.data.fetcher import get_daily_history
        df = get_daily_history(code, days=25)
        if df is not None and len(df) >= 20 and {"close", "low", "volume"}.issubset(df.columns):
            return df
    except Exception:  # noqa: BLE001
        pass
    return None


def _oversold_signals(df) -> tuple:
    """承接信号: (满足项, 明细)。可用 3 项,满足 >=2 入选。"""
    close = df["close"].tolist()
    low = df["low"].tolist()
    vol = df["volume"].tolist()
    n = len(df)
    price = close[-1]
    base_vol = sum(vol[max(0, n - 8):n - 2]) / max(1, min(6, n - 2))
    sigs, notes = [], []
    # A 近2日缩量止跌(近2日成交持续低于前6日均量且当日跌幅温和)
    if base_vol > 0 and vol[-1] < base_vol * 0.85 and vol[-2] < base_vol * 0.95 \
            and close[-1] >= close[-2] * 0.985:
        sigs.append("缩量止跌")
        notes.append("近2日量能持续萎缩,抛压收敛")
    # B 当日收低位阳线未创新低
    if close[-1] > close[-2] and low[-1] >= low[-2]:
        sigs.append("低位阳线")
        notes.append("收阳且未创新低,短线止跌信号")
    # C 回踩关键均线(MA20)企稳
    ma20 = sum(close[-20:]) / 20
    if ma20 > 0 and abs(price - ma20) / ma20 <= 0.02 and price >= ma20 * 0.995:
        sigs.append("均线企稳")
        notes.append(f"回踩MA20({ma20:.2f})附近企稳")
    return sigs, notes


def _watch_pool_oversold(d: dict) -> list:
    """子池2: 有承接的超跌标的池。仅从三层主线及关联赛道候选。"""
    from app.support import mainline as _ml
    core, branch, watch = _sector_tiers()
    try:
        from app.support import mainline_stabilizer as _stab
        dec_name = ((_stab.get_output()["stable"] or {}).get("defensive") or {}).get("name")
    except Exception:  # noqa: BLE001
        dec_name = None

    # 候选板块: 核心(2) + 发酵(2) + 观察(3) + 决策防御备选(如有)
    names = [r["industry"] for r in (core[:2] + branch[:2] + watch[:3])]
    if dec_name and dec_name not in names:
        names.append(dec_name)

    spot = _ml._a_spot_map()
    bad = ("ST", "退")
    cand_codes = []
    for name in names:
        try:
            cons = _ml._concept_cons(name)
        except Exception:  # noqa: BLE001
            continue
        picked = 0
        for c in cons:
            s = spot.get(c)
            if not s or not s.get("price"):
                continue
            if any(b in (s.get("name") or "").upper() for b in bad):
                continue
            fmv = s.get("float_mv") or 0
            if fmv and not (20 <= fmv <= 500):    # 市值区间过滤(亿),避免过度样本
                continue
            cand_codes.append((c, s, name))
            picked += 1
            if picked >= 15:
                break
        if len(cand_codes) >= 80:                 # 全池限流,控制 kline 抓取压力
            break

    rows_out = []
    for code, s, sector in cand_codes:
        df = _kline(code)
        if df is None:
            continue
        r10 = df["close"].iloc[-1] / df["close"].iloc[-11] - 1
        idx10 = _sector_idx_r10(sector)
        excess = r10 - idx10 if idx10 is not None else None
        # 超跌判定: 超额回撤>=15%; 概念指数不可得时降级为绝对回撤>=15%(口径注明)
        if excess is None:
            if r10 > -0.15:
                continue
        elif excess > -0.15:
            continue
        # 近20日相对低位: 现价距20日最低点 <= 12%
        low20 = df["low"].iloc[-20:].min()
        if low20 <= 0 or (df["close"].iloc[-1] - low20) / low20 > 0.12:
            continue
        sigs, notes = _oversold_signals(df)
        if len(sigs) < 2:
            continue
        rows_out.append([
            code, s.get("name"), sector,
            _cell(f"{r10 * 100:.1f}%"),
            _cell(f"{excess * 100:.1f}%" if excess is not None else f"{r10 * 100:.1f}%(无板块口径)"),
            f"{low20:.2f}",
            "/".join(sigs),
            "次日确认企稳+量能放大再进场;跌破近期新低止损",
        ])
        if len(rows_out) >= 10:
            break
    return rows_out


# ---------------------------------------------------------------- 入口
def _prev_day_targets(d: dict) -> list:
    """决策引擎数据未就绪时,复用前一日推荐标的并标注「T日数据待更新」(杜绝空值)。

    设计目的: 观察池不出现空档,口径仍对齐系统三档(激进/中军/补涨)。
    """
    try:
        import datetime as _dt
        from app.review import archive
        prev = archive.prev_day(str(d.get("date") or _dt.date.today()))
        if not prev:
            return []
        spot = {}
        try:
            from app.support import mainline as _ml
            spot = _ml._a_spot_map()
        except Exception:  # noqa: BLE001
            pass
        rows = []
        _seen = set()   # 同标的多板块/多角色只保留首个, 避免 920021 等重复三行
        for sector, segs in (prev.get("targets") or {}).items():
            for seg_key, role in (("aggressive", "情绪龙头"), ("steady", "中军龙头")):
                for it in segs.get(seg_key) or []:
                    code = it.get("code")
                    if code in _seen:
                        continue
                    _seen.add(code)
                    s = spot.get(code) or {}
                    rows.append([sector, role, code, _cell(s.get("name") or code),
                                 _cell(f"{s.get('pct_chg', 0):+.2f}%"),
                                 "-", "-", "-", "-", "-",
                                 "复用前一日推荐(买入区间/止损/目标待更新,当日涨幅为实际收盘)"])
        return rows
    except Exception:  # noqa: BLE001
        return []


def watch_pool_review(d: dict) -> list:
    """生成「明日重点观察标的池」结构化 items。"""
    items = []
    items.append({"head": "子池1 · 主线龙头观察池(三档梯队: 中军/情绪/补涨)"})
    rows1 = _watch_pool_leader(d)
    if rows1:
        items.append({"table": {"title": "",
                                "cols": ["板块", "角色", "代码", "名称", "当日涨幅",
                                         "买入区间", "止损", "目标", "盈亏比", "建议仓位", "观察要点"],
                                "rows": rows1}})
    else:
        # 数据未就绪: 复用前一日三档推荐, 标注 T日待更新(废弃「暂缺」空档)
        prev_rows = _prev_day_targets(d)
        if prev_rows:
            items.append({"t": "决策引擎当日标的未就绪,以下复用前一日三档推荐并标注「T日数据待更新」。"})
            items.append({"table": {"title": "",
                                    "cols": ["板块", "角色", "代码", "名称", "当日涨幅",
                                             "买入区间", "止损", "目标", "盈亏比", "建议仓位", "观察要点"],
                                    "rows": prev_rows}})
        else:
            items.append({"t": "决策引擎当日标的未就绪,且无前一日推荐可复用。"})

    items.append({"head": "子池2 · 有承接的超跌标的池(三层主线及关联赛道)"})
    rows2 = _watch_pool_oversold(d)
    if rows2:
        items.append({"t": "筛选口径:近10日相对板块概念指数超额回撤≥15% + 近20日相对低位;"
                           "承接信号满足 2/3 项(缩量止跌/低位阳线/均线企稳)入选。"
                           "个股主力净流入不在全A快照内,以均线企稳近似资金承接,轻仓套利定位。"})
        items.append({"table": {"title": "",
                                "cols": ["代码", "名称", "板块", "10日跌幅", "超额回撤", "支撑位", "承接信号", "观察要点"],
                                "rows": rows2}})
    else:
        items.append({"t": "当前无同时满足「超跌+承接」的标的,短线不盲目抄底。"})
    return items
