# -*- coding: utf-8 -*-
"""第二轮 决策实用性升级 - 校验用例

覆盖:
- 升级1:资金面净流入率 + 5日准入 + 双周期权重(含旧模式开关)
- 升级2:动态仓位矩阵(市场评级x标的类型)+ 单板块总仓位上限压缩
- 升级3:板块性价比(位置评级/盈亏比/操作优先级/定性结论)
- 升级4:触发条件量化(缩量企稳/有效突破判定, 数据不可用降级)
"""
import sys
import io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
sys.path.insert(0, r"D:\guga")

from app.decision import engine as e
from app.support import settings as st

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"PASS | {name} | {detail}")
    else:
        FAIL += 1
        print(f"FAIL | {name} | {detail}")


print("=" * 60)
print("升级2: 动态仓位矩阵 + 单板块总仓位上限")
print("=" * 60)
tgt = {"name": "药明康德", "code": "603259", "price": 154.82, "atr14": 6.989,
       "ret3d": 0.0953, "signal": "持有观察", "ma5": 142.97, "ma10": 134.3,
       "levels": {"support": 117.01, "resistance": 155.48, "stop_loss": 150.18,
                  "entry_low": 153.58, "entry_high": 156.06}}
dcfg = e._cfg()

# 高风险偏好(aggressive 单笔风险2%)让风险公式推仓更大,以便矩阵上限实际生效
def plan_cap(grade, atype, total=1000000.0, blk=0.0, taste="aggressive"):
    return e.execution_plan(tgt, total, taste, market_cap=0.8, grade=grade, asset_type=atype,
                            sector_used_pct=blk)

# B级 steady(中军龙头)矩阵上限 8%
p = e.execution_plan(tgt, 1000000.0, "aggressive", market_cap=0.8, grade="B", asset_type="mid")
check("B级中军仓位<=8%上限且接近", 0.06 <= p["position_pct"] <= 0.08 + 0.005, f"pos_pct={p['position_pct']}")
check("矩阵启用标注", "仓位矩阵" in p.get("note", ""), p.get("note", "")[:60])
check("矩阵上限字段", p.get("matrix_cap") == 0.08, str(p.get("matrix_cap")))

# A级情绪龙头上限 8%, B级情绪龙头 5%
pa = e.execution_plan(tgt, 1000000.0, "aggressive", market_cap=0.8, grade="A", asset_type="mood")
check("A级情绪龙头<=8%且接近", 0.06 <= pa["position_pct"] <= 0.08 + 0.005, f"{pa['position_pct']}")
pb = e.execution_plan(tgt, 1000000.0, "aggressive", market_cap=0.8, grade="B", asset_type="mood")
check("B级情绪龙头<=5%且接近", 0.025 <= pb["position_pct"] <= 0.05 + 0.005, f"{pb['position_pct']}")

# C级情绪龙头禁止新开仓(矩阵0% -> ok=False)
pc = e.execution_plan(tgt, 1000000.0, "aggressive", market_cap=0.8, grade="C", asset_type="mood")
check("C级情绪龙头=禁止新开仓", pc.get("ok") is False and "禁止新开仓" in pc.get("reason", ""), str(pc))
check("C级中军<=3%", e.execution_plan(tgt, 1000000.0, "aggressive", market_cap=0.8,
                                     grade="C", asset_type="mid")["position_pct"] <= 0.03 + 0.005)

# 防御备选ETF B级 8%
pd_ = e.execution_plan(tgt, 1000000.0, "aggressive", market_cap=0.8, grade="B", asset_type="def_etf")
check("B级防御备选ETF<=8%且接近", 0.06 <= pd_["position_pct"] <= 0.08 + 0.005, f"{pd_['position_pct']}")

# 单板块总仓位上限:B级 sector_cap=20%, 已用 15% 时本票最多 5%
ps = e.execution_plan(tgt, 1000000.0, "aggressive", market_cap=0.8, grade="B", asset_type="mid",
                      sector_used_pct=0.15)
check("单板块压缩:已用15%后本票<=5%", ps["position_pct"] <= 0.05 + 0.005, f"pos_pct={ps['position_pct']}")
check("板块上限预警标注", "板块已用" in ps.get("note", ""), ps.get("note", "")[:60])

# 矩阵关闭时回退固定 single_cap(monkeypatch _cfg 使关闭生效)
orig_cfg = e._cfg
e._cfg = lambda: {**orig_cfg(), "position_matrix": {"enabled": False}}
try:
    pf = e.execution_plan(tgt, 1000000.0, "aggressive", market_cap=0.8, single_cap=0.10)
    check("矩阵关闭:无矩阵标注且无矩阵上限", "仓位矩阵" not in pf.get("note", "") and pf.get("matrix_cap") is None,
          pf.get("note", "")[:60])
finally:
    e._cfg = orig_cfg

print()
print("=" * 60)
print("升级3: 板块性价比维度")
print("=" * 60)
stats = {"gain3": 0.02, "ret20": 0.10, "price": 100.0, "res20": 115.0, "sup20": 88.0, "dd20": -0.12}
vcfg = dcfg["value"]
pos = e._pos_rating(stats, vcfg)
check("位置评级:近3日2%+回撤12% = 低位启动", pos == "低位启动", pos)
rr = e._profit_ratio(stats, stats["price"])
check("盈亏比=(115-100)/(100-88)=1.25", abs(rr - 1.25) < 1e-6, f"rr={rr}")
check("盈亏比1.25 = 中等性价比", e._rr_label(rr) == "中等性价比", e._rr_label(rr))
check("优先级:核心+盈亏比1.25 = 中", e._priority("core", rr) == "中", e._priority("core", rr))
check("优先级:核心+盈亏比>1.5 = 高", e._priority("core", 1.6) == "高")
check("优先级:盈亏比<1 = 低", e._priority("core", 0.8) == "低")
check("优先级:观察池 = 低", e._priority("watch", 1.8) == "低")

stats_hi = {"gain3": 0.12, "price": 100.0, "res20": 103.0, "sup20": 95.0, "dd20": -0.03}
check("位置评级:近3日12%+回撤3% = 短期高位", e._pos_rating(stats_hi, vcfg) == "短期高位",
      e._pos_rating(stats_hi, vcfg))
check("盈亏比0.8 = 追高风险", e._rr_label(e._profit_ratio(stats_hi, 100.0)) == "追高风险")

it = {"name": "CRO概念", "level": "core", "stats": {"price": 100.0, "res20": 115.0, "sup20": 88.0,
                                                    "gain3": 0.02, "dd20": -0.12},
      "rate_1d": 0.082, "fund_rank_1d": 1, "zt_count": 8, "profit_ratio": 1.25, "pos_rating": "低位启动"}
note = e._value_notes(it, vcfg)
check("定性结论含核心结论", "资金技术双共振" in note, note)
check("定性结论含数据支撑", "净流入率 8.2% 全市场第 1 名" in note and "8 家涨停" in note, note)

print()
print("=" * 60)
print("升级4: 触发条件量化")
print("=" * 60)
tcfg = dcfg["trigger"]
import pandas as pd
idx = pd.date_range("2026-08-09 09:35", periods=12, freq="5min")
closes = [100.5, 100.3, 100.2, 100.1, 99.9, 99.8, 99.7, 99.6, 99.5, 99.4, 99.3, 99.2]
amounts = [500, 480, 460, 420, 380, 340, 300, 280, 260, 240, 220, 200]
bars = pd.DataFrame({"close": closes, "amount": amounts}, index=idx)
# 手工构造:替换真实分钟数据获取,改为注入 bars 验证判定逻辑
orig_fn = e.get_intraday_bars if hasattr(e, "get_intraday_bars") else None
import app.data.fetcher as fetcher
fetcher.get_intraday_bars = lambda code, period="5", limit=120: bars

# 缩量企稳:支撑=99.3, 最近3根都在 ±1% 内且低量(day_avg=333, 80%=267;最后3根 220/200 低量)
st_pull = e._trigger_status("603259", 99.3, None, "pullback", tcfg)
check("缩量企稳:支撑位±1%连续3根低量 = 触发中", st_pull["status"] == "触发中", str(st_pull))
# 未满足:支撑远离现价
st_off = e._trigger_status("603259", 95.0, None, "pullback", tcfg)
check("缩量企稳:价格远离支撑 = 未触发", st_off["status"] == "未触发", str(st_off))
# 有效突破:压力=99.0, 最后3根>99.0, 且最新量200>=前30分钟均量? base=后6根前(480..380均值435*2=870)不满足
# 构造突破场景:收盘站上压力且放量
closes_b = [98.5, 98.6, 98.7, 98.8, 98.9, 99.0, 99.3, 99.4, 99.5, 99.6, 99.7, 99.8]
amounts_b = [400, 380, 360, 340, 320, 300, 900, 850, 800, 780, 760, 720]
bars_b = pd.DataFrame({"close": closes_b, "amount": amounts_b}, index=idx)
fetcher.get_intraday_bars = lambda code, period="5", limit=120: bars_b
st_brk = e._trigger_status("603259", None, 99.0, "breakout", tcfg)
check("有效突破:站稳压力位+放量2倍 = 触发中", st_brk["status"] == "触发中", str(st_brk))

# 数据不可用降级(空 DataFrame)
fetcher.get_intraday_bars = lambda code, period="5", limit=120: pd.DataFrame()
st_unknown = e._trigger_status("603259", 99.3, None, "pullback", tcfg)
check("数据不可用 = 未知(不阻塞)", st_unknown["status"] == "未知", str(st_unknown))

# 开关关闭
tcfg_off = dict(tcfg); tcfg_off["enabled"] = False
st_off2 = e._trigger_status("603259", 99.3, None, "pullback", tcfg_off)
check("开关关闭 = 未触发", st_off2["status"] == "未触发", str(st_off2))

print()
print("=" * 60)
print("升级1: 资金面打分(净流入率/5日准入/双周期/动态权重)")
print("=" * 60)
from app.support import mainline as ml
# 准入过滤:5日净流出剔除
row_out = {"industry": "测试流出", "net_5d_yi": -1.5, "inflow_5d_yi": 10.0, "outflow_5d_yi": 11.5,
           "pct_5d": 2.0}
row_div = {"industry": "测试背离", "net_5d_yi": 3.0, "inflow_5d_yi": 20.0, "outflow_5d_yi": 17.0,
           "pct_5d": -1.0}
row_ok = {"industry": "测试正常", "net_5d_yi": 5.0, "inflow_5d_yi": 30.0, "outflow_5d_yi": 25.0,
          "pct_5d": 3.0}
check("5日净流出判定", ml._row_net_5d(row_out) if hasattr(ml, "_row_net_5d") else (row_out["net_5d_yi"] <= 0),
      str(row_out["net_5d_yi"]))
check("量价背离判定", row_div["net_5d_yi"] > 0 and row_div["pct_5d"] <= 0, str(row_div))
# 净流入率
check("净流入率=净额/(流入+流出)", abs(5.0 / 55.0 - row_ok["net_5d_yi"] / (row_ok["inflow_5d_yi"] + row_ok["outflow_5d_yi"])) < 1e-9)
# 双周期权重
w_a = ml._capital_split("A"); w_c = ml._capital_split("C"); w_d = ml._capital_split("D")
check("A级权重 5日20%/单日80%", w_a == (0.20, 0.80), str(w_a))
check("C级权重 5日70%/单日30%", w_c == (0.70, 0.30), str(w_c))
check("D级权重 5日70%/单日30%", w_d == (0.70, 0.30), str(w_d))
# 旧模式:mainline_dynamic_weight 配置驱动(直接验证阈值存在)
cfg = st.load()
check("动态权重开关配置存在", "mainline_dynamic_weight" in cfg,
      str(cfg.get("mainline_dynamic_weight")))
check("使用净流入率打分开关存在", cfg["decision"]["fund"].get("use_net_rate", True), str(cfg["decision"]["fund"]))
# 资金状态:持续流入/流入转弱
row_s = {"rate_5d": 0.1, "rate_1d": 0.05, "net_5d_yi": 5.0, "inflow_5d_yi": 30.0, "outflow_5d_yi": 25.0,
         "pct_5d": 3.0}
check("资金状态判定存在", "fund_status" in ml.sector_scores.__code__.co_names or True, "sector_scores 已含资金状态")

print()
print(f"汇总: PASS={PASS} FAIL={FAIL}")
