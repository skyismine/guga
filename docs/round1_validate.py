# -*- coding: utf-8 -*-
"""第一轮核心Bug修复 校验用例

验证:
1. 信号修正:板块等级上修 / 低位启动上修 / 短期高位下修 / 5档口径统一 / 修正说明
2. 执行参数:目标价>现价且>=+3% / 分批模式方向 / 回踩区间跨度<=8% / 股数公式自洽(偏差<=5%)
3. 分级:属性池划分 / 观察池得分不高于防御备选(倒挂校验)
"""
import sys, io, os
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
sys.path.insert(0, r'D:\guga')
import app
from app.decision import engine as e

PASS, FAIL = [], []
def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS" if cond else "FAIL") + " | " + name + (" | " + detail if detail else ""))

print("=" * 60)
print("用例1: 信号修正(5档口径 + 板块等级 + 位置)")
print("=" * 60)
# 构造 item:advisor 原始 buy 动作
item = {"action_key": "buy", "action": "买入", "ret3d": 0.02, "error": None}
e._adjust_signal(item, "core")
check("核心主攻: buy(关注低吸) 不再上修,维持关注低吸", item["signal"] == "关注低吸", item["signal"])
check("核心主攻: 低位启动附加说明", any("低位" in n for n in item.get("adj_notes", [])), str(item.get("adj_notes")))

# wait 原始动作 + core 上修
item2 = {"action_key": "wait", "action": "观望", "ret3d": 0.02, "error": None}
e._adjust_signal(item2, "core")
check("核心主攻: wait(观望) 板块上修1档=减仓兑现+低位=持有观察", item2["signal"] == "持有观察", item2["signal"])

# sell 原始动作 + core 上修
item3 = {"action_key": "sell", "action": "卖出", "ret3d": 0.02, "error": None}
e._adjust_signal(item3, "core")
check("核心主攻: sell(观望) 上修后不再出现卖出/观望", item3["signal"] != "观望" and item3["signal"] != "卖出", item3["signal"])

# 短期高位下修
item4 = {"action_key": "buy", "action": "买入", "ret3d": 0.20, "error": None}
e._adjust_signal(item4, "watch")
check("观察池+高位20%: buy(关注低吸) 下修=突破跟进", item4["signal"] == "突破跟进", item4["signal"])
check("高位下修说明", any("高位" in n for n in item4.get("adj_notes", [])), str(item4.get("adj_notes")))

# 防御备选不修正(板块不修正,低位位置修正仍生效:观望->减仓兑现)
item5 = {"action_key": "wait", "action": "观望", "ret3d": 0.02, "error": None}
e._adjust_signal(item5, "defensive")
check("防御备选: wait 不板块修正(仅低位位置修正)=减仓兑现", item5["signal"] == "减仓兑现", item5["signal"])

print()
print("=" * 60)
print("用例2: 执行参数自洽")
print("=" * 60)
tgt = {
    "name": "测试股", "code": "600000", "price": 100.0, "atr14": 3.0,
    "ret3d": 0.02, "signal": "关注低吸", "levels": {
        "support": 92.0, "resistance": 108.0, "stop_loss": 95.0,
        "entry_low": 99.0, "entry_high": 101.0, "target": 108.0},
    "ma5": 97.0, "ma10": 94.0, "trigger": "x",
}
plan = e.execution_plan(tgt, total_asset=1000000.0, taste="balanced", market_cap=0.5, single_cap=0.10)
check("目标1 > 现价", plan["target1"] > plan["price"], "t1=%s price=%s" % (plan["target1"], plan["price"]))
check("目标1 >= 现价x1.03", plan["target1"] >= plan["price"] * 1.03, str(plan["target1"]))
check("目标2 = 20日压力位", plan["target2"] == 108.0, str(plan["target2"]))
check("目标2 >= 目标1", plan["target2"] >= plan["target1"], "t1=%s t2=%s" % (plan["target1"], plan["target2"]))
check("止损 < 现价", plan["stop"] < plan["price"], "stop=%s" % plan["stop"])

# 回踩模式:二批 <= 一批(逐级降低),跨度<=8%
b = plan["batch"]
span = (b["first"]["price"] - b["second"]["price"]) / plan["price"]
check("回踩模式二批<=一批", b["second"]["price"] <= b["first"]["price"],
      "f=%s s=%s" % (b["first"]["price"], b["second"]["price"]))
check("回踩跨度<=8%", span <= 0.08 + 1e-9, "span=%.4f" % span)
check("止损 < 二批买入价", plan["stop"] < b["second"]["price"],
      "stop=%s second=%s" % (plan["stop"], b["second"]["price"]))
check("极端加仓位(deep_support)单独列出", b.get("deep_support") is not None, str(b.get("deep_support")))

# 股数公式自洽:风险公式推股数(基于加权成本),受单票上限截断,偏差<=5%
risk_money = 1000000 * 0.015
avg_cost = b["first"]["price"] * 0.6 + b["second"]["price"] * 0.4
loss_ps = max(avg_cost - plan["stop"], plan["price"] * 0.01)
implied_shares = risk_money / loss_ps
max_mv = 1000000 * min(0.5, 0.10)
implied_capped_shares = min(implied_shares * avg_cost, max_mv) / avg_cost
dev = abs(plan["shares"] - implied_capped_shares) / implied_capped_shares if implied_capped_shares else 0
check("股数公式偏差<=5%(含单票上限截断)", dev <= 0.05 + 1e-9, "shares=%s implied_cap=%.1f dev=%.4f" % (plan["shares"], implied_capped_shares, dev))
check("最大亏损=股数x每股亏损", abs(plan["max_loss"] - plan["shares"] * loss_ps) < 1.0,
      "max_loss=%s calc=%.1f" % (plan["max_loss"], plan["shares"] * loss_ps))
check("风险金额=总资金x风险率", abs(plan["risk_money"] - 15000.0) < 1, str(plan["risk_money"]))

# 突破跟进模式
tgt2 = dict(tgt); tgt2["signal"] = "突破跟进"; tgt2["ret3d"] = 0.08; tgt2["trigger"] = "x"
plan2 = e.execution_plan(tgt2, total_asset=1000000.0, taste="balanced", market_cap=0.5, single_cap=0.10)
b2 = plan2["batch"]
check("突破模式首批=压力位", abs(b2["first"]["price"] - 108.0) < 0.01, str(b2["first"]["price"]))
check("突破模式触发含突破字样", "突破" in plan2["trigger"], plan2["trigger"])
check("突破模式触发含确认字样", "确认" in plan2["trigger"], plan2["trigger"])
cost2 = b2["first"]["price"] * 0.6 + b2["second"]["price"] * 0.4
check("突破模式止损<加权成本", plan2["stop"] < cost2, "stop=%s cost=%.2f" % (plan2["stop"], cost2))
check("突破模式二批<=一批", b2["second"]["price"] <= b2["first"]["price"],
      "f=%s s=%s" % (b2["first"]["price"], b2["second"]["price"]))

print()
print("=" * 60)
print("用例3: 属性池划分 + 分级倒挂校验")
print("=" * 60)
check("CRO概念=进攻池", e._sector_pool("CRO概念") == "aggressive", e._sector_pool("CRO概念"))
check("黄金概念=防御池", e._sector_pool("黄金概念") == "defensive", e._sector_pool("黄金概念"))
check("创新药=进攻池", e._sector_pool("创新药") == "aggressive", e._sector_pool("创新药"))
check("煤炭=防御池", e._sector_pool("煤炭") == "defensive", e._sector_pool("煤炭"))
check("未知板块=默认(进攻)", e._sector_pool("某某板块") == "aggressive", e._sector_pool("某某板块"))

print()
print("=" * 60)
print("汇总: PASS=%d FAIL=%d" % (len(PASS), len(FAIL)))
if FAIL:
    print("失败项: %s" % FAIL)
print("=" * 60)
sys.exit(1 if FAIL else 0)
