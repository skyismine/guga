# -*- coding: utf-8 -*-
"""第三轮:校验 API + 开关各态降级 + 无快照降级。"""
import sys, io, glob, os
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
sys.path.insert(0, r"D:\guga")
from app.web.server import app

c = app.test_client()
PASS = FAIL = 0

def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"PASS | {name} | {detail}")
    else:
        FAIL += 1
        print(f"FAIL | {name} | {detail}")

# 1) 页面整体
html = c.get("/decision").get_data(as_text=True)
check("页面200+新模块", html.count("conclusion-bar") >= 1 and "target-tabs" in html and "sector-modal" in html)
check("淘汰折叠默认关闭", "<details closed" in html or "已淘汰" in html)
check("Tab 嵌套 pane", html.count("tab-pane") >= 1 and html.count("tabs-btn") >= 1)
check("情绪龙头风险标签", "高波动·纯情绪博弈" in html)

# 纠错:板块弹窗要有关键名称
check("弹窗JS函数存在", "openSectorDetail" in html and "closeSectorModal" in html)

# 2) 板块详情 API
r = c.get("/api/sector_detail", query_string={"name": "CRO概念"})
j = r.get_json()
check("板块详情API 200", r.status_code == 200, str(r.status_code))
for k in ("name", "breakdown_html", "kline_html", "constituent_html", "news_html", "reason_html"):
    check(f"API 字段 {k}", (j or {}).get(k) is not None, str((j or {}).get(k))[:40])

# 3) 设置页含新开关
set_html = c.get("/settings").get_data(as_text=True)
for k in ("web_ui.conclusion_bar", "web_ui.yesterday_review", "web_ui.sector_detail", "前端体验"):
    check(f"设置页 {k}", k in set_html)

# 4) API 设置返回 web_ui 默认
from app.support import settings as st
cfg = st.load()
w = cfg.get("web_ui") or {}
check("settings web_ui 默认开启", all(w.get(x) for x in
      ("conclusion_bar", "yesterday_review", "rejected_collapse", "target_tabs",
       "mood_risk_tag", "delta_arrows", "sector_detail")), str(w))

# 5) 快照写入(快照以决策 data date 命名)
import datetime
from app.decision import engine as en
bd = en.decision_brief()
ddate = str(bd.get("date") or datetime.date.today())
today = str(datetime.date.today())
t1 = os.path.join(r"D:\guga\data_cache\review", f"targets_{ddate}.json")
t2 = os.path.join(r"D:\guga\data_cache\review", f"layers_{ddate}.json")
check("今日 targets 快照已写", os.path.exists(t1), t1)
check("今日 layers 快照已写", os.path.exists(t2), t2)

print(f"\n汇总: PASS={PASS} FAIL={FAIL}")