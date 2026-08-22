"""P0-3 当日决策效果验证(交易决策闭环的「验证」环节)。

- 读取前一交易日存档(`archive.prev_day`),用当日收盘数据回验昨日决策:
  主线(核心/防御/观察)当日涨跌幅、是否跑赢大盘、排名变动;
  推荐标的(激进/中军/ETF)平均收益、上涨命中率、跑赢板块比例;
  GBM 模型预测方向 vs 实际涨跌,输出当日预测准确率。
- `build_archive_record` 在每日复盘生成时落库当日决策快照与核心指标(P3),
  供次日验证与长周期统计。

设计取舍: 验证对象以「决策引擎稳定器 + 分档选股」实际输出为准(与实盘一致);
标的/主线当日涨幅取自收盘快照(sector_flow / 全A spot),模型方向以存档 p_up 判定。
任一环节数据缺失均单独降级为「暂缺」,不阻塞整体验证。
"""
import datetime as dt

from app.review import archive


def _col(v, c: str = "") -> dict:
    return {"v": v, "c": c}


def _cell(v) -> str:
    return str(v) if v is not None else "-"


def build_archive_record(d: dict) -> dict:
    """构建当日决策快照 + 核心指标,供次日决策验证与 P3 指标落库。"""
    rec = {"date": str(d.get("date") or dt.date.today()), "top": [], "stable": {}, "targets": {}, "metrics": {}}
    try:
        from app.support import mainline as _ml
        rows = _ml.sector_scores(use_cache=True) or []
        rec["top"] = [{"name": r["industry"], "level": r.get("level"), "rank": r.get("rank"),
                       "score": r.get("score", 0)} for r in rows]
    except Exception:  # noqa: BLE001
        pass
    try:
        from app.support import mainline_stabilizer as _stab
        stable = _stab.get_output()["stable"] or {}
        rec["stable"] = {
            "core": (stable.get("core") or {}).get("name"),
            "defensive": (stable.get("defensive") or {}).get("name"),
            "watch": [w.get("name") for w in (stable.get("watch") or [])],
        }
    except Exception:  # noqa: BLE001
        pass
    # 兜底: 稳定器未就绪(周末/未轮询)时,回退到 sector_scores 三层榜单,
    # 保证每日仍能归档推荐标的供次日决策验证(口径仍与决策系统同源)。
    if not rec["stable"].get("core") and not rec["stable"].get("watch"):
        core_t = [t["name"] for t in rec["top"] if t.get("level") == "core"][:1]
        branch_t = [t["name"] for t in rec["top"] if t.get("level") == "branch"][:3]
        rec["stable"] = {"core": core_t[0] if core_t else None,
                         "defensive": None, "watch": branch_t}

    # 推荐标的(核心 + 防御 + 观察,分档存 code+p_up,供次日方向验证)
    target_sectors = []
    if rec["stable"].get("core"):
        target_sectors.append((rec["stable"]["core"], "core"))
    if rec["stable"].get("defensive"):
        target_sectors.append((rec["stable"]["defensive"], "defensive"))
    for w in (rec["stable"].get("watch") or [])[:3]:
        target_sectors.append((w, "watch"))
    try:
        from app.decision import engine as _en
        for name, lv in target_sectors:
            segs = {}
            res = _en.match_level_targets(name, lv)
            for k in ("aggressive", "steady", "etf"):
                segs[k] = [{"code": it.get("code"), "p_up": it.get("p_up")}
                           for it in (res.get(k) or {}).get("items", [])
                           if it.get("code")]
            rec["targets"][name] = segs
    except Exception:  # noqa: BLE001
        pass

    # 核心指标(P3 落库)
    m = {}
    try:
        from app.decision.engine import market_permit
        p = market_permit()
        m.update({"market_grade": p.get("grade"), "cap": p.get("cap"),
                  "fear_greed": p.get("fear_greed"), "adv_ratio": p.get("adv_ratio"),
                  "limit_up": p.get("limit_up"), "amount_yi": p.get("amount_yi")})
    except Exception:  # noqa: BLE001
        pass
    try:
        from app.features.market_features import market_snapshot
        snap = market_snapshot().get("market") or {}
        m["hot_ratio"] = snap.get("market_hot_ratio")
        m["adv_ratio_mkt"] = snap.get("market_adv_ratio")
    except Exception:  # noqa: BLE001
        pass
    try:
        from app.support.mainline import market_style_bias
        st = market_style_bias() or {}
        m["style_tag"] = st.get("tag")
    except Exception:  # noqa: BLE001
        pass
    lu = d.get("limit_up") or {}
    m["max_lian"] = lu.get("max_lian")
    m["zhadan_total"] = lu.get("zhadan_total")
    # 情绪温度(与 P1 大盘情绪周期同源口径): 基于涨停家数 + 恐贪
    fg = m.get("fear_greed")
    zt_n = m.get("limit_up")
    if zt_n is not None:
        if zt_n >= 60 or (fg is not None and fg >= 70):
            m["emotion"] = "高温"
        elif zt_n >= 40 or (fg is not None and fg >= 55):
            m["emotion"] = "中性偏暖"
        elif zt_n <= 20 or (fg is not None and fg <= 30):
            m["emotion"] = "低温"
        else:
            m["emotion"] = "中性"
    core_n = sum(1 for t in rec["top"] if t.get("level") == "core")
    branch_n = sum(1 for t in rec["top"] if t.get("level") == "branch")
    m["mainline_core"] = core_n
    m["mainline_branch"] = branch_n
    m["strength"] = "强" if core_n and branch_n >= 1 else ("中" if core_n else "弱")
    rec["metrics"] = {k: v for k, v in m.items() if v is not None}
    return rec


def _today_rank_map(d: dict) -> dict:
    """当日板块榜单排名映射(score 降序):{板块: rank}。"""
    try:
        from app.support import mainline as _ml
        rows = _ml.sector_scores(use_cache=True) or []
        return {r["industry"]: r.get("rank", 99) for r in rows}
    except Exception:  # noqa: BLE001
        return {}


def verify_review(d: dict) -> list:
    """生成「当日决策效果验证」结构化 items。"""
    today = str(d.get("date") or dt.date.today())
    prev = archive.prev_day(today)
    if not prev:
        return [{"t": "前一日决策存档缺失(需连续两日生成复盘后自动补齐验证),本轮跳过效果验证。"}]

    flows = {f["industry"]: f for f in d.get("sector_flow", [])}
    idx = {i["name"]: i["pct_chg"] for i in d.get("indices", [])}
    bench = idx.get("上证指数") or idx.get("上证综指")
    rank_now = _today_rank_map(d)
    prev_rank = {t["name"]: t.get("rank", 99) for t in prev.get("top", [])}

    items = []

    # ---------- 主线验证 ----------
    items.append({"head": "主线验证(昨日核心/防御/观察)"})
    mrows = []
    stable = prev.get("stable") or {}
    prev_lines = [("核心", stable.get("core")), ("防御", stable.get("defensive"))]
    for w in (stable.get("watch") or [])[:3]:
        prev_lines.append(("观察", w))
    for lv, name in prev_lines:
        if not name:
            continue
        f = flows.get(name)
        pct = (f or {}).get("pct_chg")
        if pct is None:
            mrows.append([lv, name, "数据暂缺", "-", "-"])
            continue
        beat = (pct > bench) if bench is not None else None
        pr, nr = prev_rank.get(name), rank_now.get(name)
        rank_chg = ""
        if pr is not None and nr is not None and pr != nr:
            rank_chg = f"{pr}→{nr} ({'升' if nr < pr else '降'}{abs(nr - pr)})"
        mrows.append([lv, name, _col(f"{pct:+.2f}%", "up" if pct >= 0 else "down"),
                      _col("跑赢" if beat else ("跑输" if beat is False else "无基准"), "up" if beat else ("down" if beat is False else "mut")),
                      rank_chg or "持平"])
    if mrows:
        items.append({"table": {"title": "", "cols": ["层级", "昨日主线", "今日涨幅", "跑赢大盘", "排名变动"],
                                "rows": mrows}})
    hits = [r for r in mrows if r[3].get("v") if isinstance(r[3], dict) and r[3].get("v") == "跑赢"]
    if hits:
        items.append({"t": f"主线跑赢大盘 **{len(hits)}/{len(mrows)}** 个,主线质量{('达标' if len(hits) >= max(1, len(mrows) // 2) else '未达标')}。"})

    # ---------- 标的验证 ----------
    items.append({"head": "标的验证(昨日推荐 激进/中军/ETF)"})
    spot = {}
    try:
        from app.support import mainline as _ml
        spot = _ml._a_spot_map()
    except Exception:  # noqa: BLE001
        pass
    targets = prev.get("targets") or {}
    trole_rows = []
    total, up_hit, beat_cnt = 0, 0, 0
    for sector, segs in targets.items():
        for seg_key, label in (("aggressive", "激进"), ("steady", "中军"), ("etf", "ETF")):
            for it in segs.get(seg_key) or []:
                code = it.get("code")
                s = spot.get(code)
                if not s or s.get("pct_chg") is None:
                    continue
                pct = s.get("pct_chg")
                f = flows.get(sector)
                sec_pct = (f or {}).get("pct_chg")
                up = pct >= 0
                beat = (pct > sec_pct) if sec_pct is not None else None
                total += 1
                if up:
                    up_hit += 1
                if beat:
                    beat_cnt += 1
                trole_rows.append([label, sector, code, (s.get("name") or ""),
                                   _col(f"{pct:+.2f}%", "up" if up else "down"),
                                   _col("命中" if up else "未中", "up" if up else "down"),
                                   _col("跑赢板块" if beat else ("跑输板块" if beat is False else "-"),
                                        "up" if beat else ("down" if beat is False else "mut"))])
    if trole_rows:
        items.append({"table": {"title": "", "cols": ["档位", "板块", "代码", "名称", "今日涨幅", "命中", "相对板块"],
                                "rows": trole_rows[:12]}})
        avg_up = up_hit / total if total else 0
        items.append({"t": f"推荐标的共 **{total}** 只,上涨命中率 **{avg_up:.0%}**,"
                           f"跑赢板块 **{beat_cnt}/{total}**;"
                           + ("效果达标,决策链路正向反馈。" if avg_up >= 0.5 else "效果未达标,明日下调相关主线优先级。" )})

    # ---------- 模型验证 ----------
    items.append({"head": "模型验证(GBM 方向预测 vs 实际)"})
    n_total, n_correct = 0, 0
    for sector, segs in targets.items():
        for seg_key in ("aggressive", "steady", "etf"):
            for it in segs.get(seg_key) or []:
                s = spot.get(it.get("code"))
                if not s or s.get("pct_chg") is None or it.get("p_up") is None:
                    continue
                pred_up = it["p_up"] >= 0.5
                actual_up = s["pct_chg"] >= 0
                n_total += 1
                if pred_up == actual_up:
                    n_correct += 1
    if n_total:
        acc = n_correct / n_total
        items.append({"t": f"模型方向预测样本 **{n_total}** 个,准确率 **{acc:.0%}**"
                           + ("(>55% 达标;样本小,仅作参考)。" if n_total < 10 else "。" )})
    else:
        items.append({"t": "昨日推荐标的收盘数据不可得,模型验证暂缺。"})
    return items
