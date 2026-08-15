# -*- coding: utf-8 -*-
"""第四轮:主线防抖稳定器校验(离线确定性状态机测试 + 可选线上冒烟)。

用法:
    python docs/stabilizer_validate.py            # 离线确定性测试
    python docs/stabilizer_validate.py --live     # 追加一次真实网络冒烟(需联网)

覆盖:
    1. enable_stabilizer=False 直接透传原始结果(兼容历史回测);
    2. 驻留晋升:新板块需连续 STABILIZE_CYCLE 个快照周期才进入正式池;
    3. 滞回保级/移出:池内板块 score<DOWN 才移出;
    4. 瞬时否决防抖:连续 N 周期命中才淘汰,未满 N 仅进 candidate;
    5. 中长期否决:过热/利空瞬时直接生效;
    6. 同池替换:第二名连续 N 周期领先才替换,未满周期前任保留;
    7. 防倒挂:观察池高于防御备选,连续 N 周期才升格;
    8. 切换统计:raw / stable 主线切换次数按日累计;
    9. 单日资金平滑:窗口=0 直接透传(5日表不参与);
    10. 输出结构:stable 含 core/defensive/watch/rejected/candidate/pass_score。
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


# ---------------------------------------------------------------- 测试装备
CFG = {
    "pass_score": 60.0, "core_n": 1, "defensive_n": 1, "watch_n": 3,
    "enable_stabilizer": True,
    "intraday_smooth_window": 0,      # 离线测试关闭平滑
    "rank_delta_thresh": None,        # 关闭阻尼,专注状态机
    "STABILIZE_CYCLE": 3,
    "COOL_DOWN_MINUTE": 20,
    "PASS_HYSTERESIS_UP": 62.0,
    "PASS_HYSTERESIS_DOWN": 58.0,
    "weaken_news_on_no_5d_money": False,
}


def mk_row(name, score, pct=1.0, net=1.0, zt=5, **kw):
    r = {"industry": name, "score": score, "pct_chg": pct, "net_yi": net,
         "zt_count": zt, "leader": "领涨", "news_hits": 0,
         "rate_1d": 0.05, "fund_rank_1d": 1, "fund_status": "持续流入",
         "rate_5d": 0.05, "fund_rank_5d": 1, "breakdown": {"fund": 20.0, "trend": 5.0, "sentiment": 2.0, "news": 0.0},
         "level": None}
    r.update(kw)
    return r


# 各板块属性池(默认进攻)
POOLS = {"defA": "defensive", "defB": "defensive"}


def _patch(rows=None, veto=None, pool=None, cfg_extra=None):
    """替换所有网络/真实依赖为确定性桩数据,返回 (桩函数引用列表)。"""
    pool = pool or {}
    stats_map = {r["industry"]: {"gain3": 0.02, "ret20": 0.05, "vol20": 0.02,
                                 "res20": 11.0, "sup20": 8.0, "price": 10.0, "dd20": -0.03}
                 for r in (rows or [])}

    def fake_cfg():
        d = {"value": {"enabled": False}, "mainline_check": {"enforce": True},
             "pool": {"default": "aggressive"}}
        if cfg_extra:
            d.update(cfg_extra)
        return d

    def fake_stats(names, max_workers=8):
        return {n: stats_map.get(n, {}) for n in names}

    def fake_pool(name):
        return pool.get(name) or POOLS.get(name) or "aggressive"

    def fake_pass(r, st):
        return [f"综合评分 {r['score']} 分,达标"]

    def fake_zt_pool():
        return [{"code": "600000"}]

    _en._cfg = fake_cfg
    _en._sector_stats_many = fake_stats
    _en._sector_pool = fake_pool
    _en._pass_reasons = fake_pass
    _ml._zt_pool = fake_zt_pool
    _stab._cf = lambda: []      # 单日资金流桩(网络源,离线测试不触网)
    _stab._cf5d = lambda: []    # 5日资金流桩
    if veto is not None:
        _en._veto = veto
    else:
        _en._veto = lambda name, r, dcfg, stats, zt_available=True: (False, [])
    _ml.sector_scores = lambda **kw: list(rows or [])


def _cycle(rows, cfg=None):
    """单次快照周期,返回 stable 字典。"""
    _patch(rows=rows)
    return _stab._build_stable(cfg or CFG)


# ---------------------------------------------------------------- 1) 关闭开关透传
def test_passthrough():
    _stab.reset()
    sentinel = {"core": {"name": "富士康概念"}, "defensive": None, "watch": [], "rejected": [], "pass_score": 60.0}
    _en.mainline_select = lambda: sentinel
    _stab._mainline_cfg = lambda: {**CFG, "enable_stabilizer": False}
    out = _stab.stabilize()
    check("关闭防抖=透传raw/stable", out["stable"] == sentinel and out["raw"] == sentinel
          and out["stabilizer_enabled"] is False, str(out["stabilizer_enabled"]))
    _stab._mainline_cfg = lambda: dict(CFG)


# ---------------------------------------------------------------- 2) 驻留晋升
def test_residency():
    _stab.reset()
    rows = [mk_row("A", 80.0), mk_row("defA", 70.0)]
    c1 = _cycle(rows)
    c2 = _cycle(rows)
    c3 = _cycle(rows)
    check("驻留:前2周期core=None", c1["core"] is None and c2["core"] is None)
    check("驻留:第3周期晋升core=A", (c3["core"] or {}).get("name") == "A",
          str((c3["core"] or {}).get("name")))
    check("驻留:防御同步晋升", (c3["defensive"] or {}).get("name") == "defA")
    check("驻留:候选列表含驻留确认板块", any(x["name"] == "A" for x in c1["candidate"]),
          str([x["name"] for x in c1["candidate"]]))


# ---------------------------------------------------------------- 3) 滞回保级/移出
def test_hysteresis():
    _stab.reset()
    rows = [mk_row("A", 80.0), mk_row("defA", 70.0)]
    for _ in range(3):
        _cycle(rows)                      # A 晋升 core
    c_keep = _cycle([mk_row("A", 59.0), mk_row("defA", 70.0)])   # 59 >= DOWN(58)
    check("滞回保级:59分>=58仍在池", (c_keep["core"] or {}).get("name") == "A")
    c_drop = _cycle([mk_row("A", 57.0), mk_row("defA", 70.0)])   # 57 < DOWN
    check("滞回移出:57分<58移出", c_drop["core"] is None, str(c_drop["core"]))
    check("滞回移出:进candidate冷却", any(x["name"] == "A" and "冷却" in " ".join(x["reasons"])
                                       for x in c_drop["candidate"]),
          str([x["reasons"] for x in c_drop["candidate"]]))
    check("日志字段cool_down_remain", any(x.get("cool_down_remain") is not None
                                         for x in c_drop["candidate"]))


# ---------------------------------------------------------------- 4) 瞬时否决防抖
def test_veto_debounce():
    _stab.reset()
    rows = [mk_row("A", 80.0), mk_row("defA", 70.0)]
    for _ in range(3):
        _cycle(rows)                      # A 晋升 core

    def trans_veto(name, r, dcfg, stats, zt_available=True):
        return (True, [f"当日主力净流出 {r['net_yi']:.1f} 亿(一票否决)"])

    _patch(rows=[mk_row("A", 80.0, net=-5.0), mk_row("defA", 70.0)], veto=trans_veto)
    c1 = _stab._build_stable(CFG)        # 第1次命中瞬时否决
    c2 = _stab._build_stable(CFG)        # 第2次
    c3 = _stab._build_stable(CFG)        # 第3次 -> 确认淘汰
    check("瞬时否决:前2次不淘汰(进candidate)", c1["core"] is None and c2["core"] is None
          and not any(x["name"] == "A" for x in c1["rejected"]),
          f"c1.rejected={[x['name'] for x in c1['rejected']]}")
    check("瞬时否决:第3次确认淘汰", any(x["name"] == "A" for x in c3["rejected"]),
          f"c3.rejected={[x['name'] for x in c3['rejected']]}")
    check("瞬时否决:冷却后仅候选", _stab._SECTOR_STATE.get("A", {}).get("cool_until", 0) > 0)


# ---------------------------------------------------------------- 5) 中长期否决瞬时生效
def test_perm_veto():
    _stab.reset()
    rows = [mk_row("A", 80.0), mk_row("defA", 70.0)]
    for _ in range(3):
        _cycle(rows)
    perm_veto = lambda name, r, dcfg, stats, zt_available=True: (
        True, ["近3日累计涨幅 +25.0%,已超 15% 过热上限(一票否决)"])
    _patch(rows=[mk_row("A", 80.0), mk_row("defA", 70.0)], veto=perm_veto)
    c = _stab._build_stable(CFG)
    check("过热否决:瞬时直接淘汰", any(x["name"] == "A" for x in c["rejected"]),
          str([x["name"] for x in c["rejected"]]))


# ---------------------------------------------------------------- 6) 同池替换防抖
def test_pool_replace():
    _stab.reset()
    rows = [mk_row("A", 80.0), mk_row("defA", 70.0)]
    for _ in range(3):
        _cycle(rows)                      # A 晋升 core
    rows2 = [mk_row("C", 90.0), mk_row("A", 80.0), mk_row("defA", 70.0)]   # C 领先 A
    names = []
    for _ in range(6):
        c = _cycle(rows2)
        names.append((c["core"] or {}).get("name"))
    # 新挑战者 C 需先 N 周期驻留进入正式池,再 N 周期领先才能替换前任 A
    check("同池替换:驻留+领先期间前任A保留", names[0] == "A" and names[1] == "A"
          and names[2] == "A" and names[3] == "A", str(names))
    check("同池替换:第5周期起C接任", names[4] == "C" and names[5] == "C", str(names))


# ---------------------------------------------------------------- 7) 防倒挂升格
def test_anti_inversion():
    _stab.reset()
    # defA 防御 resident; A 进攻 pool leader(进攻), W 进攻但分数低于A -> 进 watch
    rows = [mk_row("A", 75.0), mk_row("W", 74.0), mk_row("defA", 70.0)]
    for _ in range(3):
        _cycle(rows)
    # 确认 W 在 watch 而非 defensive
    c0 = _cycle(rows)
    check("防倒挂:W初始在观察池", any(w["name"] == "W" for w in c0["watch"]),
          str([w["name"] for w in c0["watch"]]) + " def=" + str((c0["defensive"] or {}).get("name")))
    # 连续驻留:第3周期后 W 应升格防御(高于 defA)
    c3 = None
    for _ in range(3):
        c3 = _cycle(rows)
    check("防倒挂:连续3周期后W升格防御", (c3["defensive"] or {}).get("name") == "W",
          str((c3["defensive"] or {}).get("name")))


# ---------------------------------------------------------------- 8) 切换统计
def test_switch_stats():
    _stab.reset()
    _stab._today = lambda: "2026-08-15"
    _stab._update_switches({"core": {"name": "A"}, "defensive": {"name": "D1"}},
                           {"core": {"name": "A"}, "defensive": {"name": "D1"}})
    _stab._update_switches({"core": {"name": "B"}, "defensive": {"name": "D1"}},
                           {"core": {"name": "A"}, "defensive": {"name": "D1"}})
    _stab._update_switches({"core": {"name": "B"}, "defensive": {"name": "D2"}},
                           {"core": {"name": "C"}, "defensive": {"name": "D1"}})
    st = _stab._sw_stats()
    check("切换统计:raw切换计数", st["raw_switches"] == 2, str(st["raw_switches"]))
    check("切换统计:stable切换计数", st["stable_switches"] == 1, str(st["stable_switches"]))
    _stab._today = _stab._real_today


# ---------------------------------------------------------------- 9) 平滑窗口
def test_smoothing():
    _stab.reset()
    flows = [{"industry": "X", "net_yi": 10.0, "inflow_yi": 30.0, "outflow_yi": 20.0,
              "pct_chg": 2.0, "num": 50, "leader": "L", "leader_pct": 5.0}]
    _stab._cf = lambda: flows
    _stab._mainline_cfg = lambda: {**CFG, "intraday_smooth_window": 0}
    out0 = _stab._smoothed_flows()
    check("平滑:窗口0直接透传", out0 == flows)
    _stab._mainline_cfg = lambda: {**CFG, "intraday_smooth_window": 5}
    _stab._smoothed_flows()
    _stab._smoothed_flows()
    out5 = _stab._smoothed_flows()
    check("平滑:窗口5返回均值结构", len(out5) == 1 and out5[0]["industry"] == "X"
          and out5[0]["net_yi"] == 10.0, str(out5))
    _stab._mainline_cfg = lambda: dict(CFG)


# ---------------------------------------------------------------- 9.5) 单周期单次抓取
def test_single_fetch():
    _stab.reset()
    from app.review import data as _data
    orig = (_data.collect_sector_flow, _data.collect_sector_flow_5d)
    n = {"c": 0, "c5": 0}

    def flow():
        n["c"] += 1
        return [{"industry": "A", "pct_chg": 1.0, "net_yi": 1.0, "inflow_yi": 2.0,
                 "outflow_yi": 1.0, "num": 10, "leader": "L", "leader_pct": 1.0}]

    def flow5():
        n["c5"] += 1
        return [{"industry": "A", "pct_5d": 5.0, "net_5d_yi": 5.0,
                 "inflow_5d_yi": 8.0, "outflow_5d_yi": 3.0}]

    _stab._cf, _stab._cf5d = flow, flow5
    _data.collect_sector_flow, _data.collect_sector_flow_5d = flow, flow5
    try:
        with _stab._cycle_flow_cache():
            a = _data.collect_sector_flow()      # raw 路径 sector_scores 内部调用
            b = _stab._cf()                      # stable 路径 _smoothed_flows 调用
            c = _data.collect_sector_flow_5d()   # raw 路径 sector_scores 内部调用
            d = _stab._cf5d()                    # stable 路径 _cf5d 调用
        check("单周期单日资金流只抓一次", n["c"] == 1 and a == b, f"calls={n['c']}")
        check("单周期5日资金流只抓一次", n["c5"] == 1 and c == d, f"calls={n['c5']}")
        check("上下文退出恢复原引用", _data.collect_sector_flow is flow
              and _data.collect_sector_flow_5d is flow5)
    finally:
        _data.collect_sector_flow, _data.collect_sector_flow_5d = orig
        _stab._cf = lambda: []
        _stab._cf5d = lambda: []


# ---------------------------------------------------------------- 10) 输出结构
def test_output_structure():
    _stab.reset()
    rows = [mk_row("A", 80.0), mk_row("defA", 70.0)]
    for _ in range(3):
        _cycle(rows)
    c = _cycle(rows)
    for k in ("core", "defensive", "watch", "rejected", "candidate", "pass_score"):
        check(f"输出结构包含 {k}", k in c)
    for it in ([c["core"]] if c["core"] else []) + ([c["defensive"]] if c["defensive"] else []) + c["watch"]:
        check(f"防抖日志字段({it['name']})", it.get("is_stable_result") is True
              and "continue_valid_cycle" in it and "cool_down_remain" in it
              and "hysteresis_trigger" in it)


# ---------------------------------------------------------------- 11) 交易时段判定
def test_trading_time():
    import datetime as _dt
    cases = [(_dt.datetime(2026, 8, 12, 9, 29), False),   # 周三 9:29 未开盘
             (_dt.datetime(2026, 8, 12, 9, 31), True),     # 9:31 盘中
             (_dt.datetime(2026, 8, 12, 11, 30), True),    # 11:30 午盘收盘
             (_dt.datetime(2026, 8, 12, 12, 59), False),   # 午休
             (_dt.datetime(2026, 8, 12, 13, 0), True),     # 13:00 午后开盘
             (_dt.datetime(2026, 8, 12, 15, 0), True),     # 15:00 收盘
             (_dt.datetime(2026, 8, 12, 15, 1), False),    # 已收盘
             (_dt.datetime(2026, 8, 15, 10, 0), False)]    # 周六
    for now, exp in cases:
        got = _stab._is_trading_time(now)
        check(f"交易时段 {now:%m-%d %H:%M}", got == exp, f"got={got}")


# ---------------------------------------------------------------- 12) 后台轮询 + get_output
def test_polling():
    _stab.reset()
    _stab._mainline_cfg = lambda: {**CFG, "intraday_smooth_window": 0}
    # 离线桩:mainline_select 与全部数据源均不触网
    _patch(rows=[mk_row("A", 80.0), mk_row("defA", 70.0)])
    _en.mainline_select = lambda: {"core": {"name": "A", "score": 80.0},
                                   "defensive": None, "watch": [], "rejected": [],
                                   "pass_score": 60.0}
    # get_output 复用最近结果(同对象)
    out1 = _stab.stabilize()
    out2 = _stab.get_output(max_age=60.0)
    check("get_output复用最近输出", out2["stable"] is out1["stable"])
    _stab._is_trading_time = lambda now=None: True    # 强制交易时段,避免收盘复用
    out3 = _stab.get_output(max_age=-1.0)             # 缓存过期 -> 同步重算
    check("get_output缓存过期同步重算", out3 is not None)
    # 轮询线程:启动后按间隔反复调用 stabilize(计数代理,不触网)
    import time as _t
    calls = {"n": 0}
    real_stab = _stab.stabilize

    def fake_stab():
        calls["n"] += 1
        return {"raw": {"core": {"name": "A"}}, "stable": {"core": {"name": "A"}},
                "stabilizer_enabled": True, "stats": {}}

    _stab.stabilize = fake_stab
    _stab._mainline_cfg = lambda: {**CFG, "poll_interval_sec": 1,
                                   "poll_trading_hours_only": True}
    t = _stab.start_polling(interval_sec=1)
    check("轮询线程启动", t is not None and t.is_alive())
    _t.sleep(2.5)
    _stab.stop_polling()
    _t.sleep(0.2)
    check("轮询按间隔推进周期", calls["n"] >= 2, f"calls={calls['n']}")
    check("轮询线程已停止", not t.is_alive())
    _stab.stabilize = real_stab
    _stab._mainline_cfg = lambda: dict(CFG)


# ---------------------------------------------------------------- --live 冒烟
def test_live():
    import importlib
    importlib.reload(_ml)
    importlib.reload(_en)
    importlib.reload(_stab)
    out = _stab.stabilize()
    st = out["stable"]
    check("live:stabilize结构", set(st) >= {"core", "defensive", "watch", "rejected", "candidate", "pass_score"})
    check("live:raw/stable区分", out["raw"] is not None and out["stabilizer_enabled"] is True)
    check("live:切换统计存在", "raw_switches" in (out.get("stats") or {}))
    print("live: raw.core=", (out["raw"]["core"] or {}).get("name"),
          "| stable.core=", (st["core"] or {}).get("name"),
          "| candidate=", [x["name"] for x in st["candidate"]][:5])


if __name__ == "__main__":
    _stab._real_today = _stab._today
    test_passthrough()
    test_residency()
    test_hysteresis()
    test_veto_debounce()
    test_perm_veto()
    test_pool_replace()
    test_anti_inversion()
    test_switch_stats()
    test_smoothing()
    test_single_fetch()
    test_output_structure()
    test_trading_time()
    test_polling()
    if "--live" in sys.argv:
        test_live()
    print(f"\n汇总: PASS={PASS} FAIL={FAIL}")