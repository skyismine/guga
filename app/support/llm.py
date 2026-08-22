"""模块7 大模型文案:把每日复盘的(结构化)结果自动生成专业策略文案与理由。

接入 OpenAI 兼容的 Chat Completions 接口(base_url/api_key/model 可在系统设置配置),
将采集到的结构化数据(大盘/板块/持仓/风控/决策)整理为紧凑上下文,
由模型产出专业措辞的策略解读、持仓观点与明日操作理由。

设计原则:
- 全链路容错:配置缺失 / 网络失败 / 超时 / 返回异常均不抛给上层,
  由 generate() 以"策略文案"部分降级为规则话术,不影响报告主体。
- 数据脱敏:仅发送汇总指标与股票名称/代码,不发送持仓金额等敏感细节的原始值
  (金额类字段做单位换算后仅保留汇总)。
- 可观测:失败原因写回返回结构的 reason 字段,页面可提示。
"""
import json
import time

import requests

from app.support import settings as _st


def _cfg() -> dict:
    return _st.load().get("llm") or {}


def enabled() -> bool:
    c = _cfg()
    return bool(c.get("enable") and c.get("api_key") and c.get("base_url") and c.get("model"))


def _req(prompt: str, system: str = None) -> str:
    c = _cfg()
    url = c["base_url"].rstrip("/") + "/chat/completions"
    payload = {
        "model": c.get("model", "gpt-4o-mini"),
        "messages": [
            {"role": "system", "content": system or _SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.4,
        "max_tokens": int(c.get("max_tokens", 1500)),
        "stream": False,
    }
    try:
        r = requests.post(url, json=payload,
                          headers={"Authorization": f"Bearer {c['api_key']}"},
                          timeout=float(c.get("timeout", 60)))
    except requests.exceptions.RequestException as e:  # noqa: BLE001
        raise RuntimeError(f"请求失败: {e}")
    if r.status_code != 200:
        raise RuntimeError(f"接口返回 {r.status_code}: {r.text[:200]}")
    data = r.json()
    try:
        return data["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, TypeError) as e:  # noqa: BLE001
        raise RuntimeError(f"返回格式异常: {e}")


_SYSTEM_PROMPT = (
    "你是一位严谨专业的 A 股量化策略分析师。用户会提供当日的市场结构化数据,"
    "包含近 3 日趋势、决策引擎输出(大盘评级/恐贪/主线分级/决策验证)与盘面采集摘要。"
    "请你撰写一份专业、深度的中文盘后复盘文案——重点是**趋势对比、归因分析与风险预警**,而非简单润色。"
    "结构固定为:\n"
    "1. 【盘面综述】:指数、量能、涨跌结构总体描述,**必须原样给出恐贪指数整数与大盘评级字母**"
    "(如「恐贪 43、评级 C」),不得省略或改写;\n"
    "2. 【板块轮动】:资金与涨幅共振的主线、分歧兑现方向、资金聚焦度;\n"
    "3. 【趋势与归因】:对比近 3 个交易日走势,说明今日量能/资金/情绪变化的因果归因,"
    "明确区分「持续趋势」与「单日脉冲」;\n"
    "4. 【事件与政策解读】:当日要闻对市场与板块的影响;\n"
    "5. 【资金与情绪】:主力资金、市场情绪、连板与涨停结构;\n"
    "6. 【核心结论与操作思路】:基于主线分层(核心/发酵/观察)与明日观察标的池的后市研判与操作基调;\n"
    "7. 【风险预警】:明确的 2-4 条风险点,至少覆盖「退潮主线不接力」「无承接超跌不抄底」等纪律类风险。\n"
    "硬性要求(逐条自检):\n"
    "- **必须原样引用数据中给出的关键标量**:恐贪指数整数、大盘评级字母、两市成交额、涨停家数,"
    "任何一条缺失都视为不合规;\n"
    "- 每一条观点都必须引用给出的具体数值/板块,严禁编造数据;\n"
    "- 措辞专业、不带情绪化喊单,明确区分事实与判断;\n"
    "- 正文 600-900 字,纯文字为主,可用少量加粗强调;\n"
    "- 结尾固定附加一句免责声明:本内容由量化系统自动生成,仅供研究参考,不构成投资建议。"
)


def _compact_data(d: dict) -> dict:
    """把 review 采集数据压缩为 LLM 友好摘要(仅汇总指标)。"""
    act = d.get("activity") or {}
    md = d.get("market_daily") or []
    out = {
        "date": d.get("date"),
        "指数": [
            {"名": i.get("name"), "涨跌": round(i.get("pct_chg", 0) * 100, 2)}
            for i in (d.get("indices") or []) if i.get("pct_chg") is not None
        ],
        "涨跌家数": {"涨": act.get("advance"), "跌": act.get("decline"),
                    "涨停": act.get("real_limit_up", act.get("limit_up")),
                    "跌停": act.get("limit_down")},
    }
    if md:
        cur = md[-1]
        out["量能资金"] = {
            "两市成交(亿)": cur.get("amount_yi"),
            "主力净流入(亿)": cur.get("main_yi"),
            "近10日资金净流入天数": sum(1 for r in md if (r.get("main_yi") or 0) > 0),
        }
    flows = d.get("sector_flow") or []
    if flows:
        top_in = sorted([f for f in flows if f["net_yi"] > 0], key=lambda x: x["net_yi"], reverse=True)[:5]
        top_out = sorted([f for f in flows if f["net_yi"] < 0], key=lambda x: x["net_yi"])[:3]
        out["板块净流入Top5"] = [{"板块": f["industry"], "涨幅%": round(f["pct_chg"], 2),
                                  "净流入亿": f["net_yi"], "领涨": f["leader"]} for f in top_in]
        out["板块净流出Top3"] = [{"板块": f["industry"], "涨幅%": round(f["pct_chg"], 2),
                                  "净流出亿": abs(f["net_yi"])} for f in top_out]
    lu = d.get("limit_up") or {}
    if lu.get("ok"):
        out["涨停池"] = {"家数": lu.get("total"), "最高连板": lu.get("max_lian"),
                        "炸板": lu.get("zhadan_total"), "封板资金亿": lu.get("total_money_yi")}
    ev = d.get("events") or {}
    if ev.get("hot"):
        out["要闻关键词"] = [n.get("title", "")[:40] for n in ev["hot"][:5]]
    return out


_MIN_CONTENT_LEN = 300   # 复盘文案最小有效长度:低于此值视为推理预算耗尽/截断,触发重试


def generate_strategy(review_data: dict = None, extra: dict = None) -> dict:
    """由复盘采集数据 + 可选附加结论生成深度复盘文案。

    返回 {"ok": bool, "text": str, "reason": str|None}:
      ok=True  已生成文案(text)
      ok=False 未启用/失败,reason 为原因(调用方应降级为规则话术)。

    重试策略: 推理类模型(deepseek 系)偶发把全部补全预算耗在 reasoning_content 上,
    导致 content 为空/过短(实测 85~1786 字随机)。对「内容过短」做最多 2 次重试,
    请求级异常仍一次即弃(避免放大网络故障成本)。
    """
    if not enabled():
        return {"ok": False, "text": "", "reason": "大模型未启用"}
    if not review_data:
        return {"ok": False, "text": "", "reason": "缺少复盘数据"}
    prompt_lines = ["以下为今日量化系统采集的市场结构化数据(JSON):", ""]
    prompt_lines.append(json.dumps(_compact_data(review_data), ensure_ascii=False, indent=1))
    if extra:
        prompt_lines.append("")
        prompt_lines.append("附加决策与持仓摘要:")
        prompt_lines.append(json.dumps(extra, ensure_ascii=False, indent=1))
    prompt_lines.append("")
    prompt_lines.append("请基于以上数据撰写今日深度复盘文案。")
    prompt = "\n".join(prompt_lines)

    last_reason = "模型返回内容过短(推理预算耗尽或输出被截断),建议调大 max_tokens"
    for attempt in range(3):
        try:
            text = _req(prompt)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "text": "", "reason": f"生成失败: {e}"}
        if len((text or "").strip()) >= _MIN_CONTENT_LEN:
            return {"ok": True, "text": text, "reason": None}
    return {"ok": False, "text": "", "reason": last_reason}


def generate_strategy_cached(review_data: dict = None, extra: dict = None, ttl: int = 1800) -> dict:
    """带内存缓存的文案生成(同一日内重复调用不重复计费)。"""
    key = str((review_data or {}).get("date"))
    now = time.time()
    hit = _CACHE.get(key)
    if hit and now - hit["t"] < ttl:
        return hit["res"]
    res = generate_strategy(review_data, extra)
    _CACHE[key] = {"t": now, "res": res}
    return res


_CACHE = {}


if __name__ == "__main__":
    from app.review import collect_review
    d = collect_review(use_cache=True)
    out = generate_strategy(d)
    print(json.dumps({"ok": out["ok"], "reason": out["reason"], "text": out["text"][:400]},
                     ensure_ascii=False, indent=1))
