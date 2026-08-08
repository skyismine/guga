"""A股每日复盘 · 文本生成:数据驱动的六大部分深度复盘(参照人工复盘模板)。

每个部分输出结构化 items(文本行 / 小节标题 / 数据表格),页面按序交错渲染,提升可读性。
  一、大盘核心综述(主题 / 指数表格 / 成交额 / 涨跌结构)
  二、板块轮动深度拆解(领涨主线表 / 分歧表 / 领跌表 / 轮动规律)
  三、核心事件深度解读(要闻表 + 央视联播 + 解读)
  四、资金面与情绪面分析(资金流向表 / 市场情绪)
  五、盘面核心结论(结论总纲 / 趋势 / 主线 / 风险 / 操作)
  六、操作策略参考(短线 / 中线 / 核心风险)

行文本中 `**加粗**` 用于标记强调,页面渲染为加粗;文本统一使用中文冒号。
"""
from typing import Dict, List


def _pct(x) -> str:
    return f"{x * 100:+.2f}%"


def _fmt_float(x, nd=2) -> str:
    return f"{x:.{nd}f}" if x is not None else "-"


def _avg(rows, key):
    vals = [r[key] for r in rows if r.get(key) is not None]
    return sum(vals) / len(vals) if vals else 0.0


def _cell(v, c: str = "") -> Dict:
    return {"v": v, "c": c}


def _head(title: str) -> Dict:
    return {"head": title}


def _t(text: str) -> Dict:
    return {"t": text}


def _table(title: str, cols: List[str], rows: List[list]) -> Dict:
    return {"table": {"title": title, "cols": cols, "rows": rows}}


_INDEX_FEATURE = {
    "上证指数": "权重蓝筹托底",
    "深证成指": "成长股联动走强",
    "创业板指": "题材成长活跃",
    "沪深300": "大盘蓝筹稳中有升",
    "上证50": "权重价值占优",
    "中证500": "中盘股跟随上行",
    "中证1000": "中小盘题材活跃",
    "科创50": "科技成长领跑",
}


# ================================================================ 一、大盘核心综述
def _index_overview(d: Dict) -> List[Dict]:
    idx = d.get("indices", [])
    act = d.get("activity", {})
    md = d.get("market_daily", [])
    if not idx:
        return [_t("当日大盘指数数据暂缺。")]
    adv, dec = act.get("advance", 0), act.get("decline", 0)
    lu_n = act.get("real_limit_up", act.get("limit_up", 0))
    ld_n = act.get("limit_down", 0)
    avg = _avg(idx, "pct_chg")
    up_n = sum(1 for i in idx if i["pct_chg"] > 0)
    down_n = len(idx) - up_n
    lead_up = max(idx, key=lambda i: i["pct_chg"])
    lead_down = min(idx, key=lambda i: i["pct_chg"])

    cur = md[-1] if md else {}
    amt_today = cur.get("amount_yi")
    amt_prev = md[-2].get("amount_yi") if len(md) > 1 else None
    valid_amt = [r["amount_yi"] for r in md if r.get("amount_yi") is not None]
    amt_avg10 = sum(valid_amt) / len(valid_amt) if valid_amt else None

    ratio = adv / (adv + dec) if (adv + dec) else 0
    effect = ("赚钱效应良好" if ratio > 0.55 and lu_n >= 50
              else ("结构分化明显、赚钱效应一般" if ratio > 0.45 else "赚钱效应偏弱"))
    width_state = ("全面普涨" if ratio > 0.6 else
                   ("涨多跌少" if ratio > 0.5 else ("涨跌对半" if ratio > 0.45 else "跌多涨少")))
    state = "普涨" if up_n >= len(idx) * 0.75 else ("涨多跌少" if up_n > down_n else "涨跌互现")

    items = []
    theme = ("指数强势上行、个股结构性分化" if (up_n >= len(idx) * 0.75 and ratio < 0.55)
             else ("放量普涨、赚钱效应扩散" if ratio > 0.6 else "指数与个股走势分化"))
    items.append(_t(f"今日 A 股呈现“{theme}”格局:主要指数{state},"
                    f"平均涨跌 **{_pct(avg)}**,{len(idx)} 个指数 {up_n} 涨 {down_n} 跌;"
                    f"领涨 **{lead_up['name']}**({_fmt_float(lead_up['close'])} 点,{_pct(lead_up['pct_chg'])}),"
                    f"相对弱势 **{lead_down['name']}**({_fmt_float(lead_down['close'])} 点,{_pct(lead_down['pct_chg'])})."))

    # 指数表格
    rows = []
    for i in sorted(idx, key=lambda x: x["pct_chg"], reverse=True):
        feat = _INDEX_FEATURE.get(i["name"], "指数跟随大盘")
        if i is lead_up:
            feat = "领涨主要指数"
        if i is lead_down:
            feat = "相对弱势、拖累指数"
        rows.append([i["name"], f"{_fmt_float(i['close'])}",
                     _cell(f"{_pct(i['pct_chg'])}", "up" if i["pct_chg"] >= 0 else "down"), feat])
    items.append(_head("核心指数表现"))
    items.append(_table("", ["指数", "收盘点位", "当日涨跌幅", "核心特征"], rows))

    # 风格结构
    big = {i["name"]: i["pct_chg"] for i in idx if i["name"] in ("上证50", "沪深300")}
    small = {i["name"]: i["pct_chg"] for i in idx if i["name"] in ("中证500", "中证1000")}
    if big and small:
        b = _avg([{"pct_chg": v} for v in big.values()], "pct_chg")
        s = _avg([{"pct_chg": v} for v in small.values()], "pct_chg")
        if s > b + 0.003:
            items.append(_t(f"风格上**小盘强于大盘**(中证1000/中证500 {_pct(s)} vs 沪深300/上证50 {_pct(b)}),"
                            "资金偏好弹性成长,题材活跃度高。"))
        elif b > s + 0.003:
            items.append(_t(f"风格上**大盘权重占优**(沪深300/上证50 {_pct(b)} vs 中证1000/中证500 {_pct(s)}),"
                            "资金偏向蓝筹防守。"))
        else:
            items.append(_t("大小盘风格基本均衡,无明显切换。"))

    # 成交额 / 量能
    if amt_today is not None:
        amt_wan = amt_today / 10000
        if amt_prev:
            diff = amt_today - amt_prev
            if diff > 0:
                items.append(_t(f"全天两市总成交约 **{amt_wan:.2f} 万亿元**,较前一交易日**放量 "
                                f"{abs(diff):.0f} 亿元**,量能重回扩张区间。"))
            else:
                items.append(_t(f"全天两市总成交约 **{amt_wan:.2f} 万亿元**,较前一交易日缩量 "
                                f"{abs(diff):.0f} 亿元,量能小幅收敛。"))
        elif amt_avg10:
            items.append(_t(f"全天两市总成交约 **{amt_wan:.2f} 万亿元**(近10日均值 {amt_avg10 / 10000:.2f} 万亿),"
                            + ("量能明显高于均值、交投活跃。" if amt_today > amt_avg10 else "量能低于均值、交投偏淡。")))

    # 涨跌结构 / 宽度
    if (adv + dec) > 0:
        items.append(_t(f"涨跌结构上,全市场 **{adv:.0f} 只上涨**、**{dec:.0f} 只下跌**,涨跌家数{width_state}、"
                        f"并非全面普涨;涨停 **{lu_n:.0f} 家** / 跌停 **{ld_n:.0f} 家**,{effect}。"
                        "上涨集中于权重龙头与细分赛道,多数个股跟涨力度有限,"
                        + ("市场分化程度较高。" if ratio < 0.55 else "市场广度良好。")))
    return items


# ================================================================ 二、板块轮动深度拆解
def _sector_rotation(d: Dict) -> List[Dict]:
    flows = d.get("sector_flow", [])
    if not flows:
        return [_t("板块资金流数据暂缺。")]
    up = sorted([f for f in flows if f["pct_chg"] > 0], key=lambda x: x["pct_chg"], reverse=True)
    down = sorted([f for f in flows if f["pct_chg"] < 0], key=lambda x: x["pct_chg"])
    by_net = sorted(flows, key=lambda x: x["net_yi"], reverse=True)

    items = []
    # 1) 领涨主线(涨幅 + 资金共振)
    mains = sorted([f for f in up[:15] if f["net_yi"] > 0],
                   key=lambda x: x["net_yi"], reverse=True)[:3]
    if not mains:
        mains = up[:3]
    items.append(_head("领涨主线(资金与涨幅共振)"))
    mrows = [[f["industry"], _cell(f"{f['pct_chg']:+.2f}%", "up"),
              _cell(f"{f['net_yi']:+.2f} 亿", "up"), f"{f['leader']} {f['leader_pct']:+.2f}%"]
             for f in mains]
    items.append(_table("", ["板块", "涨跌幅", "主力净流入", "领涨股"], mrows))
    main_names = {f["industry"] for f in mains}
    spread = [f for f in up[:10] if f["industry"] not in main_names]
    if spread:
        items.append(_t("扩散分支:" + "、".join(f["industry"] for f in spread[:4])
                        + " 同步跟涨,形成板块级资金合力,主线由单一板块向同链条扩散。"))

    # 2) 分歧 / 兑现主线(涨幅高但资金净流出)
    diverge = [f for f in up[:10] if f["net_yi"] < -0.5]
    if diverge:
        items.append(_head("分歧 / 兑现主线(涨但资金流出,风险信号)"))
        drows = [[f["industry"], _cell(f"{f['pct_chg']:+.2f}%", "up"),
                  _cell(f"{f['net_yi']:+.2f} 亿", "down"), "冲高回落 / 获利兑现压力"] for f in diverge[:3]]
        items.append(_table("", ["板块", "涨跌幅", "主力净流出", "风险提示"], drows))

    # 3) 领跌方向
    if down:
        items.append(_head("领跌方向(资金流出)"))
        down_rows = [[f["industry"], _cell(f"{f['pct_chg']:+.2f}%", "down"),
                      _cell(f"{f['net_yi']:+.2f} 亿", "down")] for f in down[:4]]
        items.append(_table("", ["板块", "涨跌幅", "主力净流出"], down_rows))

    # 4) 资金聚焦度
    if by_net:
        top = by_net[0]
        inflow_all = sum(f["net_yi"] for f in by_net if f["net_yi"] > 0)
        top_share = (top["net_yi"] / inflow_all) if inflow_all else 0
        items.append(_t(f"全市场概念板块主力净流入合计 **{inflow_all:+.2f} 亿**,居首 **{top['industry']}**"
                        f"({top['net_yi']:+.2f} 亿,占比 {top_share:.0%}),资金聚焦度"
                        + ("较高、主线集中。" if top_share > 0.15 else "分散、多线并进。")))

    # 5) 轮动核心规律
    items.append(_head("板块轮动核心规律"))
    if diverge:
        items.append(_t("**高低切换加速**:资金从涨幅居前但净流出的高位板块流出,流向位置更低、资金净流入的板块,"
                        "市场风险偏好仍在,但对高位标的追高意愿显著下降。"))
    else:
        items.append(_t("**主线聚焦**:资金向净流入居前的板块集中,赚钱效应呈结构性特征。"))
    items.append(_t("**指数强、个股分化**:指数普涨而涨跌家数接近,上涨由权重与龙头拉动,"
                    "多数个股跟涨乏力,选股难度加大。"))
    md = d.get("market_daily", [])
    if md:
        cur = md[-1]
        amt_avg10 = _avg(md, "amount_yi")
        if cur.get("amount_yi") and amt_avg10 and cur["amount_yi"] > amt_avg10:
            items.append(_t("**放量分歧不等于趋势反转**:成交额高于近10日均值说明流动性充裕,"
                            "板块分歧只是内部结构调整,并非系统性风险释放。"))

    # 6) 涨停池行业分布(情绪主线)
    lu = d.get("limit_up", {})
    inds = lu.get("industries", {})
    if inds:
        topi = list(inds.items())[:4]
        items.append(_t("短线情绪主线:涨停股集中于 "
                        + "、".join(f"**{k}**({v}家)" for k, v in topi) + "。"))
    return items


# ================================================================ 三、核心事件深度解读
_POLICY_KW = {"央行", "国务院", "证监会", "国常会", "政策", "降准", "降息", "LPR", "MLF",
              "注册制", "印花税", "两融", "退市", "并购", "重组", "IPO", "改革", "开放"}
_EXTERNAL_KW = {"美联储", "联储", "欧央行", "关税", "北向", "外资", "美元", "美债",
                "通胀", "就业", "加息", "利率", "海外"}
_TECH_KW = {"半导体", "芯片", "AI", "人工智能", "科技", "光模块", "算力", "机器人"}
_FUND_KW = {"业绩", "订单", "涨价", "减产", "招标", "业绩预告", "中报", "扩产", "产能"}
_SECTOR_KW = {"出口", "光伏", "锂电", "汽车", "地产", "消费", "医药", "新能源", "军工", "有色", "稀土"}


def _event_reading(kws) -> str:
    k = set(kws)
    parts = []
    if k & _POLICY_KW:
        parts.append("属**政策面**信号,影响风险偏好与对应板块估值")
    if k & _EXTERNAL_KW:
        parts.append("属**外部因素**,情绪扰动大于实质影响,关注外资与汇率反应")
    if k & _TECH_KW:
        parts.append("属**产业催化**,利好科技成长链条,关注次日板块资金验证")
    if k & _FUND_KW:
        parts.append("属**基本面验证**,关注业绩兑现与个股持续性")
    if k & _SECTOR_KW:
        parts.append("属**行业景气**信号,关注板块资金共振与龙头表现")
    return ("解读:" + ("、".join(parts) if parts else "市场关注度提升")
            + "。事件驱动需结合次日竞价与板块资金验证,谨防利好兑现高开低走。")


def _events(d: Dict) -> List[Dict]:
    ev = d.get("events", {})
    items = []
    hot = ev.get("hot", []) or []
    if hot:
        items.append(_head("当日财经要闻(市场关联度排序)"))
        erows = [[n["title"], _event_reading(n.get("keywords", []))] for n in hot[:5]]
        items.append(_table("", ["要闻", "解读"], erows))
    cctv = ev.get("cctv", []) or []
    if cctv:
        items.append(_head("央视《新闻联播》要点"))
        for c in cctv[:3]:
            items.append(_t(f"· {c['title']}"))
        items.append(_t("联播内容反映政策与宏观定调方向,相关领域往往获得增量政策预期。"))
    if not hot and not cctv:
        items.append(_t("暂无当日驱动事件数据,建议结合盘后公告与政策动态补充判断。"))
    else:
        items.append(_t("解读:当日事件与指数/板块共振方向一致者,次日延续概率更高;"
                        "若事件方向与盘面背离,则需以盘面资金为准。"))
    return items


# ================================================================ 四、资金面与情绪面分析
def _capital_sentiment(d: Dict) -> List[Dict]:
    items = []
    flows = d.get("sector_flow", [])
    md = d.get("market_daily", [])

    items.append(_head("1. 资金流向"))
    if flows:
        by_net = sorted(flows, key=lambda x: x["net_yi"], reverse=True)
        top_in = [f for f in by_net if f["net_yi"] > 0][:5]
        top_out = [f for f in reversed(by_net) if f["net_yi"] < 0][:5]
        if top_in:
            items.append(_table("主力净流入居前", ["板块", "净流入(亿)", "涨跌幅"],
                                [[f["industry"], _cell(f"{f['net_yi']:+.2f}", "up"),
                                  _cell(f"{f['pct_chg']:+.2f}%", "up" if f["pct_chg"] >= 0 else "down")]
                                 for f in top_in]))
        if top_out:
            items.append(_table("主力净流出居前", ["板块", "净流出(亿)", "涨跌幅"],
                                [[f["industry"], _cell(f"{f['net_yi']:+.2f}", "down"),
                                  _cell(f"{f['pct_chg']:+.2f}%", "up" if f["pct_chg"] >= 0 else "down")]
                                 for f in top_out]))
        pos = sum(1 for f in flows if f["net_yi"] > 0)
        items.append(_t(f"概念板块中 **{pos}/{len(flows)}** 获主力净流入,"
                        + ("资金面整体偏暖。" if pos >= len(flows) * 0.6 else "资金面整体偏谨慎。")))

    north = d.get("north", {})
    if north:
        if north.get("north_available") and abs(north.get("north_total_yi", 0)) >= 0.01:
            t = north.get("north_total_yi", 0)
            blocks = "、".join(f"{b['block']} {b['net_yi']:+.2f}亿" for b in north.get("north_blocks", [])) or "暂无"
            items.append(_t(f"北向资金当日{('净流入' if t > 0 else '净流出')} **{_fmt_float(abs(t))} 亿元**({blocks}),"
                            + ("外资情绪积极。" if t > 30 else ("外资谨慎观望。" if abs(t) < 15 else "外资态度中性。"))))
        else:
            south = north.get("south_total_yi", 0)
            items.append(_t("北向资金单日实时净买入自 2024 年起停止披露(交易所口径),当日数据不可得;"
                            f"港股通(南向)当日净买入 **{south:+.2f} 亿**,可作外资/南向偏好参考。"))

    mf = d.get("market_fund", {})
    if mf.get("ok") and mf.get("rows"):
        main = mf["rows"][0]
        items.append(_t(f"大盘主力资金 {main['name']} 净流入 **{main['net_yi']:+.2f} 亿**"
                        f"(占成交 {main.get('net_pct', 0):+.2f}%)。"))

    if md:
        valid_main = [r["main_yi"] for r in md if r.get("main_yi") is not None]
        valid_amt = [r["amount_yi"] for r in md if r.get("amount_yi") is not None]
        cur = md[-1]
        if valid_main:
            up_days = sum(1 for v in valid_main if v > 0)
            total = sum(valid_main)
            items.append(_t(f"近10个交易日大盘主力资金 **{up_days}/{len(valid_main)} 日净流入**,"
                            f"累计净流入 **{total:+.0f} 亿**,"
                            + ("资金面近10日整体偏暖。" if up_days >= 6 else "资金面近10日整体偏谨慎。")))
        if valid_amt:
            avg_amt = sum(valid_amt) / len(valid_amt)
            items.append(_t(f"两市日均成交额 {avg_amt:.0f} 亿,今日 **{cur.get('amount_yi') or 0:.0f} 亿**,"
                            + ("量能明显放大、交投活跃、流动性充裕。" if (cur.get('amount_yi') or 0) > avg_amt * 1.1
                               else ("量能温和。" if (cur.get('amount_yi') or 0) > avg_amt * 0.9
                                     else "量能有所萎缩、观望情绪浓。"))))

    items.append(_head("2. 市场情绪"))
    act = d.get("activity", {})
    lu = d.get("limit_up", {})
    lu_n = act.get("real_limit_up", act.get("limit_up", 0))
    ld_n = act.get("limit_down", 0)
    items.append(_t(f"全市场涨停 **{lu_n:.0f} 家**、跌停 **{ld_n:.0f} 家**,"
                    + ("市场情绪仍处多头区间、赚钱效应充足" if lu_n >= 60
                       else ("情绪中性、结构性机会为主" if lu_n >= 30 else "情绪偏弱、观望为主"))
                    + ";情绪主要集中于低位修复赛道,高位科技股追高意愿下降,资金倾向于“避高就低”。"))
    if lu.get("ok"):
        items.append(_t(f"涨停池共 {lu['total']} 家,最高连板 **{lu['max_lian']} 板**,"
                        f"炸板 {lu['zhadan_total']} 次,封板资金合计 {lu['total_money_yi']:.1f} 亿,"
                        + ("连板高度与封板资金同步走强,短线情绪处于亢奋区,但需警惕退潮。" if lu["max_lian"] >= 5
                           else "连板高度一般,情绪处于中性区间。")))
    diverge = [f for f in flows if f["pct_chg"] > 0 and f["net_yi"] < -0.5] if flows else []
    if diverge:
        items.append(_t("高位风险:涨幅居前但主力净流出的板块 "
                        + "、".join(f"**{f['industry']}**" for f in diverge[:3])
                        + " 存在兑现压力,谨防高开回落与局部踩踏。"))
    else:
        items.append(_t("高位风险信号较少,板块涨跌与资金方向总体一致,承接力尚可。"))
    return items


# ================================================================ 五、盘面核心结论
def _conclusion(d: Dict) -> List[Dict]:
    idx = d.get("indices", [])
    act = d.get("activity", {})
    md = d.get("market_daily", [])
    flows = d.get("sector_flow", [])
    lu_n = act.get("real_limit_up", act.get("limit_up", 0))
    ld_n = act.get("limit_down", 0)
    adv, dec = act.get("advance", 0), act.get("decline", 0)
    _north = d.get("north", {})
    north = _north.get("north_total_yi", 0) if _north.get("north_available") else 0

    score = 0
    avg_pct = _avg(idx, "pct_chg") if idx else 0
    if avg_pct > 0.01:
        score += 2
    elif avg_pct < -0.01:
        score -= 2
    ratio = adv / (adv + dec) if (adv + dec) else 0.5
    if ratio > 0.55:
        score += 1
    elif ratio < 0.45:
        score -= 1
    if lu_n >= 60:
        score += 1
    elif ld_n >= 30:
        score -= 1
    if north > 20:
        score += 1
    elif north < -20:
        score -= 1
    if md and len(md) > 1:
        cur, prev = md[-1], md[-2]
        if cur.get("amount_yi") and prev.get("amount_yi") and cur["amount_yi"] > prev["amount_yi"]:
            score += 1

    if score >= 4:
        base = "盘面**偏强**,指数、量能、宽度与资金面共振向上,主线清晰,短线可维持进攻思路"
    elif score >= 2:
        base = "盘面**中性偏强**,结构性机会为主,指数未现系统性风险,关注主线持续性"
    elif score <= -2:
        base = "盘面**偏弱**,赚钱效应收缩,防守优先,耐心等待情绪企稳"
    else:
        base = "盘面**多空拉锯**、方向不明,宜控制仓位、轻仓试探"

    reso = sorted([f for f in flows if f["pct_chg"] > 0 and f["net_yi"] > 0],
                  key=lambda x: x["net_yi"], reverse=True)
    focus = "、".join(f"**{f['industry']}**" for f in reso[:3]) if reso else "暂无明确主线"
    diverge = [f for f in flows if f["pct_chg"] > 0 and f["net_yi"] < -0.5] if flows else []
    diverge_names = "、".join(f"**{f['industry']}**" for f in diverge[:2]) if diverge else "无"

    items = [_head("结论总纲")]
    items.append(_t(f"{base},但内部分化加剧,行情从单边普涨进入结构轮动阶段。"))

    trend = ("指数放量收阳、量能充足,上行趋势未破" if avg_pct > 0.005
             else ("指数缩量回踩,趋势偏弱" if avg_pct < -0.005 else "指数高位震荡、方向待选择"))
    items.append(_t(f"**趋势判断**:{trend}"
                    + ("。进入震荡分化阶段。" if 0.45 <= ratio <= 0.55 else "。")))
    items.append(_t(f"**主线判断**:资金共振主线为 {focus};资金向低位修复/中上游确定性方向迁移,"
                    "低位补涨成为当前核心偏好。"))
    if diverge:
        items.append(_t(f"**风险预警**:{diverge_names} 等板块涨幅居前但主力净流出,高位兑现压力显现,"
                        + ("是盘面主要风险点,谨防传导至同链条。" if score >= 0 else "需防范资金流出扩散。")))
    items.append(_t("**操作基调**:低吸不追高,聚焦低位确定性;"
                    + ("短线可维持进攻、控制仓位 6-7 成。" if score >= 3
                       else ("中性仓位、以结构性波段为主。" if score >= 0 else "防守为主、轻仓试错。"))))
    return items


# ================================================================ 六、操作策略参考
def _strategy(d: Dict, conclusion_text: str) -> List[Dict]:
    flows = d.get("sector_flow", [])
    lu = d.get("limit_up", {})
    lu_n = d.get("activity", {}).get("real_limit_up", d.get("activity", {}).get("limit_up", 0))

    reso = sorted([f for f in flows if f["pct_chg"] > 0 and f["net_yi"] > 0],
                  key=lambda x: x["net_yi"], reverse=True)
    focus = "、".join(f"**{f['industry']}**" for f in reso[:3]) if reso else "暂无明确主线"
    diverge = [f for f in flows if f["pct_chg"] > 0 and f["net_yi"] < -0.5] if flows else []
    avoid = "、".join(f"**{f['industry']}**" for f in diverge[:3]) if diverge else "**短期涨幅过大的高位情绪标的**"
    weak = sorted(flows, key=lambda x: x["pct_chg"])[:2] if flows else []

    if "偏强" in conclusion_text or "进攻" in conclusion_text:
        pos = "6-7 成"
    elif "偏弱" in conclusion_text or "防守" in conclusion_text:
        pos = "3-4 成"
    else:
        pos = "4-5 成"

    items = [_head("1. 短线交易思路")]
    items.append(_t(f"· 不追高连续大涨的题材小票,情绪退潮时回撤速度极快;仓位控制:总仓位 **{pos}**,"
                    "聚焦核心赛道,避免频繁追涨杀跌。"))
    items.append(_t(f"· 重点跟踪 {focus} 主线的企稳/回踩机会:若龙头缩量回踩关键支撑后获资金承接,"
                    "可低吸同链条低位分支,否则以观望为主。"))
    items.append(_t(f"· 回避方向:{avoid};高位股谨防核按钮与量化踩踏。"))

    items.append(_head("2. 中线持仓思路"))
    if reso:
        items.append(_t(f"· 持仓结构优化:核心仓位配置资金共振主线 {focus},作为进攻主力;"
                        + (f"可少量配置领跌后超跌的防御方向(**{'、'.join(f['industry'] for f in weak)}**)作为对冲。"
                           if weak else "可少量配置低估值防御品种作为对冲。")))
    items.append(_t("· 操作节奏:利用盘中分歧调仓,不盲目杀跌;高位涨幅大的标的可分批兑现部分仓位,"
                    "切换至仍有修复空间的低位分支与超跌方向。"))
    if lu.get("max_lian", 0) >= 5:
        items.append(_t("· 情绪跟踪:连板高度较高,短线情绪可能见顶退潮,追高风险加大,注意高位股退潮节奏。"))

    items.append(_head("3. 核心风险提示"))
    risks = []
    if lu.get("max_lian", 0) >= 5:
        risks.append("连板高度较高,情绪退潮引发高位股补跌")
    if diverge:
        risks.append("高位获利盘集中兑现,带动成长股整体调整")
    risks.append("8 月进入中报业绩披露期,部分标的业绩不及预期引发个股风险")
    risks.append("海外市场波动加剧、外资流向反复影响市场情绪")
    rrows = [[f"风险{i}", rk] for i, rk in enumerate(risks[:4], 1)]
    items.append(_table("", ["序号", "风险点"], rrows))
    items.append(_t("风控纪律:以上均为模型与数据复盘参考,**不构成投资建议**;严格执行止损,控制单票仓位。"))
    return items


# ================================================================ 核心数据表(全局速览)
def _tables(d: Dict) -> Dict:
    idx = d.get("indices", [])
    act = d.get("activity", {})
    flows = d.get("sector_flow", [])
    lu = d.get("limit_up", {})
    north = d.get("north", {})
    by_net = sorted(flows, key=lambda x: x["net_yi"], reverse=True)
    return {
        "activity": {
            "advance": act.get("advance"), "decline": act.get("decline"),
            "limit_up": act.get("real_limit_up", act.get("limit_up")),
            "limit_down": act.get("limit_down"),
            "flat": act.get("flat"),
            "activity_pct": act.get("activity_pct"),
        },
        "sector_in_top": [{"industry": f["industry"], "pct_chg": f["pct_chg"],
                           "net_yi": f["net_yi"], "leader": f["leader"]}
                          for f in by_net[:10]],
        "limit_up": {
            **{k: v for k, v in lu.items() if k != "date"},
            "industries": {f["industry"]: f["net_yi"] for f in by_net[:8]},
            "ind_label": "涨停概念方向" if by_net else "涨停行业分布",
        },
        "north": north,
        "market_daily": d.get("market_daily", []),
    }


# ================================================================ 入口
def _items_to_lines(items: List[Dict]) -> List[str]:
    return [i["t"] if "t" in i else i["head"] for i in items if ("t" in i or "head" in i)]


def generate_review(d: Dict) -> Dict:
    """由采集数据生成完整复盘(六大部分结构化 items + 全局速览表)。"""
    overview = _index_overview(d)
    sector = _sector_rotation(d)
    events = _events(d)
    capital = _capital_sentiment(d)
    conclusion = _conclusion(d)
    conclusion_text = conclusion[-1].get("t", "") if conclusion else ""
    strategy = _strategy(d, conclusion_text)

    sections = {
        "index_overview": {"title": "一、大盘核心综述", "items": overview},
        "sector_rotation": {"title": "二、板块轮动深度拆解", "items": sector},
        "events": {"title": "三、核心事件深度解读", "items": events},
        "capital_sentiment": {"title": "四、资金面与情绪面分析", "items": capital},
        "conclusion": {"title": "五、盘面核心结论", "items": conclusion},
        "strategy": {"title": "六、操作策略参考", "items": strategy},
    }
    for sec in sections.values():
        sec["lines"] = _items_to_lines(sec["items"])

    return {
        "date": d.get("date"),
        "sections": sections,
        "tables": _tables(d),
        "generated_at": None,
    }
