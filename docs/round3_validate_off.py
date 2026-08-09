# -*- coding: utf-8 -*-
"""第三轮:开关关闭降级测试(不改核心逻辑,仅验证展示开关)。"""
import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
sys.path.insert(0, r"D:\guga")
import app.web.server as srv

c = srv.app.test_client()
PASS = FAIL = 0

def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"PASS | {name} | {detail}")
    else:
        FAIL += 1
        print(f"FAIL | {name} | {detail}")

# 全部关闭
import app.web.server as m
orig = m._web_ui_flag
m._web_ui_flag = lambda k: False
try:
    html = c.get("/decision").get_data(as_text=True)
finally:
    m._web_ui_flag = orig
check("关闭后无结论卡", "conclusion-bar" not in html)
check("关闭后无Tab", "data-tab" not in html and 'id="tab-' not in html)
check("关闭后无弹窗", "sector-modal" not in html and "openSectorDetail" not in html)
check("关闭后无风险标签", "高波动·纯情绪博弈" not in html)
check("关闭后无复盘/淘汰折叠", "昨日信号复盘" not in html)
print(f"\n汇总: PASS={PASS} FAIL={FAIL}")