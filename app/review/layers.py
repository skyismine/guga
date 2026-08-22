"""P0-1 主线三层分级法深度研判。

口径与实盘决策系统 100% 对齐:
- 榜单源: `mainline.sector_scores()`(概念板块综合评分,core/branch/watch 三层,与决策引擎同源);
- 决策主线: `mainline_stabilizer.get_output()["stable"]`(今日决策引擎实际采纳的核心/防御/观察);
- 梯队数据: `mainline._zt_pool()`(涨停池)按板块成分裁剪,与决策引擎 `_zt_of_sector` 同口径;
- 资金验证: 单日 `sector_flow` + 5 日 `sector_flow_5d`(与决策引擎资金因子同源)。

演进追踪基于 `archive` 前 3 个交易日的三层榜单存档,自动标注 延续/新晋/退潮;
生命周期为轻量规则判定(启动/发酵/分歧/退潮),输出格局判定与强度评级。

设计取舍: 生命周期采用「资金方向 + 排名变动 + 梯队高度」三条可量化信号打分,
避免引入复杂时序模型;数据缺失时相应信号不计分,不阻塞输出。
"""
from app.review import archive


def _col(v, c: str = "") -> dict:
    return {"v": v, "c": c}


def _cell(v) -> str:
    return str(v) if v is not None else "-"


def _sector_flows(d: dict):
    """今日单日/5日资金映射:{板块: {net_yi, net_5d_yi, pct_5d}}。

    同花顺 5 日排行概念命名可能与即时表略有出入(如「稀土永磁」vs「稀土」),做包含匹配兜底。
    """
    today = {f["industry"]: f for f in d.get("sector_flow", [])}
    five_rows = d.get("sector_flow_5d", [])
    five_exact = {f["industry"]: f for f in five_rows}
    out = {}
    for name, f in today.items():
        t5 = five_exact.get(name)
        if t5 is None:  # 包含匹配(短名可能出现在长名中)
            cand = [ff for ff in five_rows if name in ff["industry"] or ff["industry"] in name]
            t5 = cand[0] if cand else {}
        out[name] = {"net_yi": f.get("net_yi"), "net_5d_yi": t5.get("net_5d_yi"),
                     "pct_5d": t5.get("pct_5d")}
    return today, out


def _sector_zt(name: str, zt: list, cons: list) -> dict:
    """板块内涨停梯队(与决策引擎同口径:代码命中成分)。"""
    codes = set(cons)
    inside = [z for z in zt if z.get("code") in codes]
    boards = [z.get("boards") or 1 for z in inside]
    return {"count": len(inside),
            "max_board": max(boards) if boards else 0,
            "top": sorted(inside, key=lambda z: -(z.get("boards") or 1))[:3]}


def _lifecycle(row: dict, zt: dict, prev_names: set, prev_map: dict) -> str:
    """生命周期判定: 启动 / 发酵 / 分歧 / 退潮。"""
    name = row["industry"]
    net = row.get("net_yi", 0) or 0
    net5 = row.get("net_5d_yi", 0) or 0
    pct = row.get("pct_chg", 0) or 0
    is_new = name not in prev_names          # 前3日榜单未出现 → 新晋候选
    rank_now = row.get("rank", 99)
    rank_prev = prev_map.get(name, 99)
    decl = rank_prev < rank_now              # 排名下滑
    if (net5 < 0 and net < 0) or (decl and net < 0) or (name in prev_names and row.get("level") == "rejected"):
        return "退潮"
    if is_new:
        return "启动"
    if net < 0 and pct > 0:
        return "分歧"
    if net5 > 0 and zt.get("max_board", 0) >= 2:
        return "发酵"
    if net5 > 0:
        return "发酵"
    if net < 0:
        return "分歧"
    return "启动"


def _prev_trend(name: str, prev_days: list) -> tuple:
    """演进状态: (标签, 存续天数)。"""
    days = 0
    for rec in prev_days:
        names = set(rec.get("top_names") or [])
        if name in names:
            days += 1
    if days > 0:
        return f"延续 {days} 日", days
    return "新晋", 0


def _driver_text(name: str, row: dict, events: list) -> str:
    """驱动因素: 电报命中 + 财联社事件关键词匹配。"""
    parts = []
    hits = row.get("news_hits", 0) or 0
    if hits:
        parts.append(f"电报 {hits} 次")
    from app.support.mainline import _concept_kw
    kw = _concept_kw(name)
    if kw and events:
        matched = [e["title"][:18] for e in events if kw and kw in e.get("title", "")][:2]
        if matched:
            parts.append("事件:" + "、".join(matched))
    return "；".join(parts) or "-"


def layer_summary(d: dict = None) -> dict:
    """轻量主线摘要(供 30秒速览 / 明日策略 复用,与实盘决策引擎同源,避免口径漂移)。"""
    from app.support import mainline as _ml
    rows = _ml.sector_scores(use_cache=True) or []
    core = [r["industry"] for r in rows if r.get("level") == "core"]
    branch = [r["industry"] for r in rows if r.get("level") == "branch"]
    active = len(core) + len(branch)
    strength = "强" if core and len(branch) >= 1 else ("中" if core else "弱")
    if core and active <= 2:
        pattern = "单主线抱团"
    elif core and active >= 3:
        pattern = "多主线轮动"
    else:
        pattern = "无明确主线"
    return {"core": core[:4], "branch": branch[:4], "strength": strength, "pattern": pattern}


def layer_review(d: dict) -> list:
    """生成「主线三层分级研判」结构化 items。"""
    from app.support import mainline as _ml
    rows = _ml.sector_scores(use_cache=True)
    if not rows:
        return [{"t": "主线榜单数据暂缺(决策引擎未就绪)。"}]

    # 决策引擎稳定主线(今日决策实际采纳),口径对齐展示
    try:
        from app.support import mainline_stabilizer as _stab
        stable = _stab.get_output()["stable"] or {}
        dec_core = (stable.get("core") or {}).get("name")
        dec_def = (stable.get("defensive") or {}).get("name")
    except Exception:  # noqa: BLE001
        dec_core = dec_def = None

    today, five = _sector_flows(d)
    zt = _ml._zt_pool()
    prev_days = archive.load_days(4)[:-1]      # 前3个交易日(不含今日)
    prev_names = set()
    prev_map = {}
    for rec in prev_days:
        for t in rec.get("top", []):
            prev_names.add(t["name"])
            prev_map.setdefault(t["name"], t.get("rank", 99))
    events = (d.get("events") or {}).get("hot") or []

    items = []
    items.append({"head": "主线分层总览(与决策引擎同源评分)"})
    if dec_core or dec_def:
        tag = f"今日决策引擎采纳:核心 **{_cell(dec_core)}**" + (f" / 防御备选 **{_cell(dec_def)}**" if dec_def else "")
        items.append({"t": tag})

    tier_cfg = {"core": "核心主线层", "branch": "发酵轮动层", "watch": "异动观察层"}
    tier_cells = {"core": "b-core", "branch": "b-branch", "watch": "b-watch"}
    _LAYER_TAG = {"core": "核心", "branch": "发酵", "watch": "观察"}

    tier_rows = {"core": [], "branch": [], "watch": []}
    retired = []   # 退潮主线(前3日在榜、今日已退出)
    for r in rows:
        name = r["industry"]
        lv = r.get("level")
        if lv == "rejected":
            continue
        if lv not in tier_cfg:
            lv = "watch"
        t5 = five.get(name, {})
        cons = _ml._concept_cons(name)
        zt_info = _sector_zt(name, zt, cons)
        row_ext = {**r, "net_5d_yi": t5.get("net_5d_yi"), "pct_5d": t5.get("pct_5d")}
        life = _lifecycle(row_ext, zt_info, prev_names, prev_map)
        trend, days = _prev_trend(name, prev_days)
        if name not in prev_names and life == "启动" and r.get("level") in ("core", "branch"):
            trend = "新晋"
        tier_rows[lv].append({
            "name": name, "level": lv, "score": r.get("score", 0),
            "pct_chg": r.get("pct_chg", 0), "net_yi": r.get("net_yi", 0) or 0,
            "net_5d": t5.get("net_5d_yi"), "zt": zt_info.get("count", 0),
            "max_board": zt_info.get("max_board", 0), "life": life, "trend": trend,
            "driver": _driver_text(name, row_ext, events),
            "fund_status": r.get("fund_status"),
        })
        if life == "退潮" and name not in prev_names:
            retired.append(name)

    # 今日在榜但前3日在榜的板块已通过 tier_rows 的 trend 标注延续;退潮名单 = 前3日在榜、今日不在
    today_names = {t["name"] for lv in tier_rows for t in tier_rows[lv]}
    for rec in prev_days:
        for t in rec.get("top", []):
            if t["name"] not in today_names:
                retired.append(t["name"])
    retired = sorted(set(retired))

    for lv in ("core", "branch", "watch"):
        grp = sorted(tier_rows[lv], key=lambda x: -x["score"])
        if not grp:
            continue
        items.append({"head": f"{tier_cfg[lv]}({len(grp)} 个)"})
        rows_tbl = []
        for s in grp:
            net5 = _cell(f"{s['net_5d']:+.1f}" if s["net_5d"] is not None else "-")
            rows_tbl.append([
                s["name"], _col(f"{s['pct_chg']:+.2f}%", "up" if s["pct_chg"] >= 0 else "down"),
                f"{s['score']:.0f}",
                _col(f"{s['net_yi']:+.1f}亿", "up" if s['net_yi'] >= 0 else "down"),
                net5,
                f"{s['zt']}家/{s['max_board']}板",
                s["life"], s["trend"], s["driver"],
            ])
        items.append({"table": {
            "title": "",
            "cols": ["板块", "当日涨幅", "综合评分", "单日净流入", "5日净流入", "涨停/最高板", "生命周期", "演进", "驱动"],
            "rows": rows_tbl,
        }})

    # 演进追踪汇总
    items.append({"head": "主线演进追踪(对比前 3 个交易日)"})
    cont = [t["name"] for lv in tier_rows for t in tier_rows[lv] if t["trend"].startswith("延续")]
    fresh = [t["name"] for lv in tier_rows for t in tier_rows[lv] if t["trend"] == "新晋"]
    if cont:
        items.append({"t": "延续主线(存续):" + "、".join(f"**{c}**" for c in cont[:6]) + "。"})
    if fresh:
        items.append({"t": "新晋主线:" + "、".join(f"**{c}**" for c in fresh[:4]) + ",持续性待验证。"})
    if retired:
        items.append({"t": "退潮主线:" + "、".join(f"**{c}**" for c in retired[:4])
                      + ",资金/热度衰减,谨防反弹乏力。"})
    if not cont and not fresh and not retired:
        items.append({"t": "前 3 个交易日无历史存档,演进追踪暂缺(需连续生成复盘后自动补齐)。"})

    # 格局判定 + 强度评级 + 核心风险
    core_n = len(tier_rows["core"])
    branch_n = len(tier_rows["branch"])
    active_n = core_n + branch_n
    _pat = layer_summary(d).get("pattern")
    pattern_map = {"单主线抱团": "**单主线抱团**:核心主线高度集中,资金聚焦单一方向,弹性大但拥挤度高",
                   "多主线轮动": "**多主线轮动**:核心+发酵多线并进,结构健康但需跟踪资金切换节奏",
                   "无明确主线": "**无明确主线**:缺乏资金共振板块,短线以超跌反弹与个股行情为主"}
    pattern = pattern_map.get(_pat, pattern_map["无明确主线"])

    # 强度评级:核心层涨停梯队 + 资金 + 连板高度
    strength_pts = 0
    for s in tier_rows["core"]:
        strength_pts += min(4, s["zt"] // 2) + min(3, s["max_board"])
        if s["net_yi"] > 0:
            strength_pts += 1
        if (s["net_5d"] or 0) > 0:
            strength_pts += 1
    if core_n == 0:
        strength = "弱"
    elif strength_pts >= 8:
        strength = "强"
    elif strength_pts >= 4:
        strength = "中"
    else:
        strength = "弱"
    items.append({"head": "主线格局与强度判定"})
    items.append({"t": f"格局: {pattern}。整体强度评级 **{strength}**。"})
    risks = []
    if any(s["life"] == "分歧" for s in tier_rows["core"]):
        risks.append("核心主线出现涨但资金流出,存在兑现压力,谨防次日高开回落")
    if any(s["life"] == "退潮" for s in tier_rows["branch"] + tier_rows["core"]):
        risks.append("部分主线进入退潮,谨防高位补跌与情绪降温")
    if core_n == 0:
        risks.append("无核心主线,题材缺乏合力,追高胜率低")
    if retired:
        risks.append(f"退潮方向({ '、'.join(retired[:3]) })反弹不接力")
    if risks:
        items.append({"t": "核心风险点:" + ";".join("· " + x for x in risks[:4])})
    return items
