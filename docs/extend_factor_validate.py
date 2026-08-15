# -*- coding: utf-8 -*-
"""第五轮:扩展因子校验(连板梯队 + 大小盘风格偏转)。

用法:
    python docs/extend_factor_validate.py   # 离线确定性测试
    python docs/extend_factor_validate.py --live  # 追加联网冒烟(market_style_bias 真实数据)

覆盖:
    1. enable_extend_factor=False 时 sector_scores 输出与改造前一致(无 ladder 字段);
    2. ladder_score:无连板低分不淘汰 / 健康梯队高分 / 断层识别 / 中军加分;
    3. trend 公式重组:pct*0.6 + 涨停*0.2 + ladder*0.2(trend 总权重30不变);
    4. market_style_bias:数据源失败降级均衡(0);
    5. 风格偏转排序:同池分差<=阈值时小市值板块在"小盘风格"下提前,分差大保持原始顺序;
    6. 稳定器梯队变差确认:炸板导致梯队变差不立即生效,连续N周期才反映。
"""
import sys
import io

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
sys.path.insert(0, r"D:\guga")

from app.support import mainline as _ml
from app.decision import engine as _en
from app.support import mainline_stabilizer as _stab

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"PASS | {name} | {detail}")
    else:
        FAIL += 1
        print(f"FAIL | {name} | {detail}")


# ---------------------------------------------------------------- 数据桩
LCFG = {
    "enabled": True, "pct_w": 0.6, "zt_w": 0.2, "ladder_w": 0.2,
    "base_board": {0: 0.10, 1: 0.50, 2: 0.75, 3: 0.88, 4: 1.00},
    "tier_bonus": 0.06, "gap_penalty": 0.10, "zhongjun_bonus": 0.12,
    "zhongjun_float_yi": 100.0, "gap_from_board": 3,
    "drop_confirm": 3, "drop_delta": 0.25,
}
SCFG = {"enabled": True, "mom_days": [10, 20], "mom_weight": [0.5, 0.5],
        "bias_thresh": 0.02, "sort_bias_thresh": 3.0}
EXT = {"ladder": LCFG, "style": SCFG}


def mk_zt(code, boards=1, float_yi=0.0):
    return {"code": code, "name": f"股{code}", "pct": 10.0, "reason": "",
            "boards": boards, "float_yi": float_yi}


def mk_flow(name, pct=1.0, net=5.0):
    return {"industry": name, "pct_chg": pct, "net_yi": net, "inflow_yi": 10.0,
            "outflow_yi": 5.0, "num": 20, "leader": "L", "leader_pct": 5.0}


def mk_flow5(name, net5=8.0, pct5=5.0):
    return {"industry": name, "pct_5d": pct5, "net_5d_yi": net5,
            "inflow_5d_yi": 12.0, "outflow_5d_yi": 4.0}


def _stub_scores(rows, zt_rows=None, cons_map=None, ext=None):
    """桩全部网络/配置依赖,返回造好的 rows 供 sector_scores 内部使用。"""
    _ml._market_grade = lambda: "B"
    _ml._capital_split = lambda grade: (0.40, 0.60)
    _ml._zt_pool = lambda: zt_rows or []
    _ml._news_mentions = lambda: {}
    _ml.market_snapshot = lambda: {"market": {"market_fear_greed": 60}}
    _ml._concept_cons = lambda name, allow_net=True: (cons_map or {}).get(name, [])
    _ml._extend_cfg = lambda: (ext or {})


# ---------------------------------------------------------------- 1) 关闭开关=原逻辑
def test_extend_off():
    _stub_scores(rows=[mk_flow("A", 1.0, 5.0)], zt_rows=[], cons_map={"A": []}, ext={})
    rows = _ml.sector_scores(
        use_cache=True, flows=[mk_flow("A", 1.0, 5.0)],
        flows_5d={mk_flow5("A", 8.0)["industry"]: mk_flow5("A", 8.0)})
    a = rows[0]
    check("关闭扩展因子:无ladder字段", "ladder_score" not in a and "ladder_tag" not in a
          and "size_bias" not in a)
    # 与原逻辑一致:pct归一单板块=0 => trend = 0.2*zt_norm;无涨停 => 0.2*pct(0)=0
    check("关闭扩展因子:trend沿用原公式", a["breakdown"]["trend"] == 0.0, str(a["breakdown"]["trend"]))


# ---------------------------------------------------------------- 2) ladder_score
def test_ladder():
    # 无连板:低分不淘汰
    out0 = _ml._sector_ladder("A", [], ["000001"], LCFG)
    check("梯队:无连板低分", out0["score"] == 0.10 and out0["tag"] == "无连板", out0["tag"])
    # 健康梯队:首板+2板+3板+中军 -> 高分满格
    zt = [mk_zt("000001", 1, 8.0), mk_zt("000002", 2, 30.0),
          mk_zt("000003", 3, 150.0)]
    out1 = _ml._sector_ladder("A", zt, ["000001", "000002", "000003"], LCFG)
    check("梯队:健康3板+中军满格", out1["score"] == 1.0 and out1["tag"] == "3板梯队·有中军"
          and not out1["gap"], f"{out1['tag']} score={out1['score']}")
    # 断层:3板却无2板
    zt2 = [mk_zt("000001", 1, 8.0), mk_zt("000003", 3, 20.0)]
    out2 = _ml._sector_ladder("A", zt2, ["000001", "000003"], LCFG)
    check("梯队:断层识别", out2["gap"] and "断层" in out2["tag"]
          and out2["score"] < out1["score"], f"{out2['tag']} score={out2['score']}")
    # 中军加分:仅流通市值>=阈值
    zt3 = [mk_zt("000001", 2, 20.0), mk_zt("000002", 2, 150.0)]
    out3 = _ml._sector_ladder("A", zt3, ["000001", "000002"], LCFG)
    check("梯队:中军加分", out3["zhongjun"] and out3["score"] > _ml._sector_ladder(
        "A", [mk_zt("000001", 2, 20.0), mk_zt("000002", 2, 30.0)], ["000001", "000002"], LCFG)["score"])


# ---------------------------------------------------------------- 3) trend 公式重组
def test_trend_formula():
    # 单板块 pct 归一=0;有3板+中军 => ladder=1.0
    _stub_scores(rows=[mk_flow("A", 1.0, 5.0)],
                 zt_rows=[mk_zt("000001", 1, 8.0), mk_zt("000002", 2, 30.0), mk_zt("000003", 3, 150.0)],
                 cons_map={"A": ["000001", "000002", "000003"]}, ext=EXT)
    rows = _ml.sector_scores(
        use_cache=True, flows=[mk_flow("A", 1.0, 5.0)],
        flows_5d={mk_flow5("A", 8.0)["industry"]: mk_flow5("A", 8.0)})
    a = rows[0]
    zt_norm = 3 / 8.0   # 3家涨停
    exp_trend = 0.6 * 0.0 + 0.2 * zt_norm + 0.2 * 1.0
    check("trend公式=pct*0.6+涨停*0.2+梯队*0.2",
          abs(a["breakdown"]["trend"] - round(30 * exp_trend, 2)) < 0.01
          and a["ladder_score"] == 1.0 and "3板梯队" in a["ladder_tag"],
          f"trend={a['breakdown']['trend']} ladder={a['ladder_score']} tag={a['ladder_tag']}")
    # 市值风格:涨停股流通市值中位数=30亿(<阈值50) -> 小盘(+1)
    check("市值风格:小盘", a["size_bias"] == 1, str(a["size_bias"]))


# ---------------------------------------------------------------- 4) style_bias 降级
def test_style_bias_fallback():
    import akshare as ak
    from app.data import market as mk
    orig_ak = ak.index_zh_a_hist
    orig_mh = mk.get_index_history
    ak.index_zh_a_hist = lambda **kw: (_ for _ in ()).throw(RuntimeError("no"))
    mk.get_index_history = lambda symbol=None, days=None, use_cache=True: \
        (_ for _ in ()).throw(RuntimeError("no"))
    _ml._style_cache.update(date=None, data=None)
    _ml._extend_cfg = lambda: EXT
    try:
        out = _ml.market_style_bias(refresh=True)
    finally:
        ak.index_zh_a_hist = orig_ak
        mk.get_index_history = orig_mh
    check("style_bias:数据源失败降级均衡", out["bias"] == 0 and out["tag"] == "均衡", out["tag"])


# ---------------------------------------------------------------- 5) 风格偏转排序
def test_style_order():
    items = [{"name": "大盘A", "score": 90.0, "size_bias": -1},
             {"name": "小盘B", "score": 88.0, "size_bias": 1}]
    # 小盘风格:分差2<=3 -> 小盘B提前
    ordered = _en._style_order(list(items), {"bias": 1, "tag": "小盘风格"}, 3.0)
    check("风格偏转:小盘风格下小市值提前", [x["name"] for x in ordered] == ["小盘B", "大盘A"],
          str([x["name"] for x in ordered]))
    # 分数差距大(分差10>3):保持原始顺序
    items2 = [{"name": "大盘A", "score": 90.0, "size_bias": -1},
              {"name": "小盘B", "score": 80.0, "size_bias": 1}]
    ordered2 = _en._style_order(list(items2), {"bias": 1, "tag": "小盘风格"}, 3.0)
    check("风格偏转:分差大保持原始score排序", [x["name"] for x in ordered2] == ["大盘A", "小盘B"],
          str([x["name"] for x in ordered2]))
    # 均衡风格:不变
    ordered3 = _en._style_order(list(items), {"bias": 0, "tag": "均衡"}, 3.0)
    check("风格偏转:均衡不动", [x["name"] for x in ordered3] == ["大盘A", "小盘B"])


# ---------------------------------------------------------------- 6) 稳定器梯队变差确认
CFG = {
    "pass_score": 60.0, "watch_n": 3, "enable_stabilizer": True,
    "intraday_smooth_window": 0, "rank_delta_thresh": None,
    "STABILIZE_CYCLE": 3, "COOL_DOWN_MINUTE": 20,
    "PASS_HYSTERESIS_UP": 62.0, "PASS_HYSTERESIS_DOWN": 58.0,
    "weaken_news_on_no_5d_money": False,
    "enable_extend_factor": True, "extend_factor": EXT,
}


def mk_row(name, score, ladder=None, **kw):
    r = {"industry": name, "score": score, "pct_chg": 1.0, "net_yi": 5.0,
         "zt_count": 3, "leader": "领涨", "news_hits": 0,
         "rate_1d": 0.05, "fund_rank_1d": 1, "fund_status": "持续流入",
         "rate_5d": 0.05, "fund_rank_5d": 1, "breakdown": {"fund": 20.0, "trend": 5.0},
         "level": None}
    if ladder is not None:
        r["ladder_score"] = ladder
        r["ladder_tag"] = f"{'高' if ladder >= 0.6 else '低'}梯队"
        r["size_bias"] = 0
    r.update(kw)
    return r


def _patch(rows):
    _en._cfg = lambda: {"value": {"enabled": False}, "mainline_check": {"enforce": True},
                        "pool": {"default": "aggressive"}}
    _en._sector_stats_many = lambda names, max_workers=8: {n: {} for n in names}
    _en._sector_pool = lambda name: "aggressive"
    _en._pass_reasons = lambda r, st: [f"综合评分 {r['score']} 分,达标"]
    _en._veto = lambda name, r, dcfg, stats, zt_available=True: (False, [])
    _ml._zt_pool = lambda: [{"code": "600000"}]
    _ml.market_style_bias = lambda: {"bias": 0, "tag": "均衡"}
    _ml.sector_scores = lambda **kw: list(rows)
    _stab._cf = lambda: []
    _stab._cf5d = lambda: []


def test_ladder_hold():
    _stab.reset()
    good = mk_row("A", 80.0, ladder=0.9)
    for _ in range(3):
        _patch([good])
        _stab._build_stable(CFG)                     # A 晋升 resident(梯队0.9)
    st = _stab._SECTOR_STATE["A"]
    check("梯队确认:先驻留成功", st.get("in_passed") is True and st.get("ladder_score") == 0.9)
    # 盘中炸板:梯队掉到0.5(降0.4>=0.25)
    bad = mk_row("A", 66.0, ladder=0.5)              # 原始分66仍>=60
    scores = []
    notes = []
    for i in range(3):
        _patch([bad])
        c = _stab._build_stable(CFG)
        scores.append((c["core"] or {}).get("score"))
        notes.append((c["core"] or {}).get("reasons", []))
    # 前2周期按持有梯队回补(score被抬高),第3周期确认后按新梯队
    held = 80.0 + 30 * 0.2 * (0.9 - 0.5)             # 回补后=82.4? 实际依赖raw score
    check("梯队确认:前2周期不立即掉分", scores[0] == scores[1] and scores[0] > bad["score"],
          f"scores={scores}")
    check("梯队确认:确认期附说明", any("梯队变差确认中" in " ".join(n) for n in notes[:2]),
          str([n[:1] for n in notes]))
    check("梯队确认:第3周期起按新梯队", scores[2] == bad["score"], f"scores={scores}")


# ---------------------------------------------------------------- --live 端到端
def test_live_integration():
    import importlib
    importlib.reload(_ml)
    importlib.reload(_en)
    importlib.reload(_stab)
    real_cfg = _stab._mainline_cfg()
    _ml._extend_cfg = lambda: EXT                       # 打开扩展因子(梯队/风格)
    _stab._mainline_cfg = lambda: {**real_cfg, "enable_extend_factor": True,
                                   "extend_factor": EXT}
    _stab.reset()
    out = _stab.stabilize()
    st = out["stable"]
    check("live扩展:stable结构", set(st) >= {"core", "defensive", "watch", "candidate"})
    items = ([st["core"]] if st["core"] else []) + ([st["defensive"]] if st["defensive"] else []) \
        + st["watch"] + st["candidate"]
    check("live扩展:梯队标签存在", any(it.get("ladder_tag") for it in items if it))
    check("live扩展:风格标签存在", any("market_style_tag" in it for it in items if it))
    check("live扩展:raw含market_style", "market_style" in (out["raw"] or {}))
    print("live扩展: style=", ((out["raw"] or {}).get("market_style") or {}).get("tag"),
          "| raw.core=", ((out["raw"] or {}).get("core") or {}).get("name"),
          "| stable.core=", (st["core"] or {}).get("name"),
          "| ladder=", sorted({it.get("ladder_tag") for it in items if it.get("ladder_tag")})[:5])
    _stab._mainline_cfg = lambda: real_cfg


if __name__ == "__main__":
    test_extend_off()
    test_ladder()
    test_trend_formula()
    test_style_bias_fallback()
    test_style_order()
    test_ladder_hold()
    if "--live" in sys.argv:
        import importlib
        test_live_integration()
    print(f"\n汇总: PASS={PASS} FAIL={FAIL}")