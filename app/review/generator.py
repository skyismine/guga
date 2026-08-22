"""A股每日复盘 · 文本生成:数据驱动的十大模块深度复盘(参照人工复盘模板)。

每个部分输出结构化 items(文本行 / 小节标题 / 数据表格),页面按序交错渲染,提升可读性。
   📋 30秒速览(大盘定性/核心主线/操作要点/核心风险)
   一、大盘综述(周期定位:量能/情绪/风格)
   二、板块轮动拆解(结构分析:核心主线/异动脉冲/退潮回落 + 驱动属性标签)
   三、主线三层分级研判(核心/发酵/观察 + 梯队/资金验证/生命周期/演进追踪/格局强度)
   四、明日重点观察标的池(主线龙头观察池 + 有承接超跌标的池)
   五、核心事件深度解读(要闻表 + 央视联播 + 解读)
   六、资金面与情绪面交叉验证(资金流向/市场情绪/背离检测/5日趋势)
   七、当日决策效果验证(主线/标的/模型三档验证)
   八、盘面核心结论(结论总纲 / 趋势 / 主线 / 风险 / 操作)
   九、明日交易策略(主线分层策略 / 超跌策略 / 仓位与风控红线)

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


def _drv_tag(f) -> str:
    """板块驱动属性标签: 领涨股接近涨停(情绪驱动) vs 中军趋势驱动。

    设计取舍: 以领涨股涨幅近似判别情绪/趋势属性(免去逐股市值/筹码采集);
    涨停级领涨 → 小票情绪驱动,否则 → 中军趋势驱动。
    """
    lp = f.get("leader_pct") or 0
    return "小票情绪驱动" if lp >= 9.5 else "中军趋势驱动"


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

    # ---- P1 周期定位(量能 / 情绪 / 风格) ----
    items.append(_head("周期定位(量能 / 情绪 / 风格)"))
    if len(valid_amt) >= 5:
        cur_amt = valid_amt[-1]
        hi, lo = max(valid_amt), min(valid_amt)
        pctile = sum(1 for v in valid_amt if v <= cur_amt) / len(valid_amt)
        vol_tag = ("放量(高位)" if cur_amt >= hi * 0.95
                   else ("放量" if pctile >= 0.8 and cur_amt > lo * 1.15
                         else ("缩量" if pctile <= 0.25 and cur_amt < hi * 0.85 else "平量")))
        items.append(_t(f"**量能周期**:近{len(valid_amt)}日成交分位 **{pctile:.0%}**"
                        f"(区间 {lo:.0f}~{hi:.0f} 亿),当前 **{vol_tag}**。"))
    fg = None
    try:
        from app.features.market_features import market_snapshot, fear_greed_label
        fg = (market_snapshot().get("market") or {}).get("market_fear_greed")
    except Exception:  # noqa: BLE001
        fg = None
    if fg is not None:
        emo = ("过热" if fg >= 75 else ("高温" if fg >= 60 else ("中性" if fg >= 40
                                                                 else ("低温" if fg >= 25 else "寒冷"))))
        items.append(_t(f"**情绪周期**:恐贪指数 **{fg:.0f}**(0-100 分位),处于 **{emo}** 区,{fear_greed_label(fg)}。"))
    elif lu_n is not None:
        emo = "高温" if lu_n >= 60 else ("中性" if lu_n >= 30 else "低温")
        items.append(_t(f"**情绪周期**:涨停 **{lu_n:.0f}** 家,处于 **{emo}** 区。"))
    style_tag = None
    try:
        from app.support.mainline import market_style_bias
        style_tag = (market_style_bias() or {}).get("tag")
    except Exception:  # noqa: BLE001
        style_tag = None
    if style_tag:
        items.append(_t(f"**风格定位**:决策系统风格标签 **{style_tag}**。"))
    elif big and small:
        items.append(_t(f"**风格定位**:今日大小盘价差 {(s - b) * 100:+.2f}pp,"
                        + ("资金偏好小盘弹性。" if s > b else "资金偏向大盘防守。")))
    try:
        from app.review import archive
        prev_tags = [(r.get("metrics") or {}).get("style_tag")
                     for r in archive.load_days(4)[:-1]]
        prev_tags = [t for t in prev_tags if t]
        if prev_tags and style_tag:
            same = sum(1 for t in prev_tags if t == style_tag)
            items.append(_t(f"**风格延续性**:前 {len(prev_tags)} 个交易日标签"
                            f"({'、'.join(prev_tags[-3:])}),今日{'延续' if same == len(prev_tags) else '切换'}。"))
    except Exception:  # noqa: BLE001
        pass
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

    # ---- P1 板块结构分类(核心主线 / 异动脉冲 / 退潮回落)+ 驱动属性标签 ----
    items.append(_head("板块结构分类(核心主线 / 异动脉冲 / 退潮回落)"))
    core_s = sorted([f for f in up if f["net_yi"] > 0],
                    key=lambda x: x["net_yi"], reverse=True)[:3]
    pulse = [f for f in up if f["net_yi"] < -0.5][:3]
    decay = sorted([f for f in flows if f["pct_chg"] < 0 and f["net_yi"] < 0],
                   key=lambda x: x["net_yi"])[:3]
    struct_rows = []
    for f in core_s:
        struct_rows.append([f["industry"], "核心主线(资金共振)", _cell(f"{f['pct_chg']:+.2f}%", "up"),
                            _cell(f"{f['net_yi']:+.2f}亿", "up"), _drv_tag(f)])
    for f in pulse:
        struct_rows.append([f["industry"], "异动脉冲(涨但资金流出)", _cell(f"{f['pct_chg']:+.2f}%", "up"),
                            _cell(f"{f['net_yi']:+.2f}亿", "down"), "持续性待验证"])
    for f in decay:
        struct_rows.append([f["industry"], "退潮回落(跌+资金流出)", _cell(f"{f['pct_chg']:+.2f}%", "down"),
                            _cell(f"{f['net_yi']:+.2f}亿", "down"), "回避/观察"])
    items.append(_table("", ["板块", "结构分类", "涨跌幅", "主力净流入", "驱动属性"], struct_rows))

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

    # ---- P1 多维度交叉验证(背离检测 + 5日趋势) ----
    items.append(_head("3. 多维度交叉验证(背离 / 趋势)"))
    adv, dec = act.get("advance", 0), act.get("decline", 0)
    idx_avg = _avg(d.get("indices", []), "pct_chg")
    if idx_avg and (adv + dec) > 0:
        ratio = adv / (adv + dec)
        if idx_avg > 0.003 and ratio < 0.45:
            items.append(_t("**背离信号**:指数上涨但下跌家数占优,指数与个股宽度背离,"
                            "权重护盘而赚钱效应弱,追涨胜率低。"))
        elif idx_avg < -0.003 and ratio > 0.55:
            items.append(_t("**背离信号**:指数下跌但多数个股上涨,存在结构修复迹象,需放量确认。"))
        else:
            items.append(_t("指数与涨跌家数方向一致,无显著背离。"))
    mf = d.get("market_fund", {})
    cur_main = (mf["rows"][0].get("net_yi") if mf.get("ok") and mf.get("rows") else None)
    if idx_avg and cur_main is not None and idx_avg > 0.003 and cur_main < -30:
        items.append(_t("**背离信号**:指数上涨但大盘主力资金净流出,量价背离,反弹持续性存疑。"))
    if len(md) >= 5:
        last5_main = [r.get("main_yi") for r in md[-5:] if r.get("main_yi") is not None]
        if len(last5_main) >= 3:
            up_days = sum(1 for v in last5_main if v > 0)
            if up_days >= 4:
                items.append(_t("近 5 日大盘主力资金**持续净流入**,资金趋势持续性强,回踩即机会。"))
            elif up_days <= 1 and last5_main[-1] < 0:
                items.append(_t("近 5 日大盘主力资金多日净流出,当日资金行为或为**单日脉冲**,持续性待验证。"))
            else:
                items.append(_t("近 5 日大盘主力资金流入流出互现,趋势不明确,以结构性机会为主。"))
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


def _market_ctx(d: Dict) -> Dict:
    """大盘维度 ctx(评级/量能/情绪/风格),供 30秒速览。复用决策引擎 market_permit,口径一致。"""
    ctx = {}
    try:
        from app.decision.engine import market_permit
        p = market_permit()
        ctx["grade"] = p.get("grade")
        vr = p.get("vol_ratio")
        ctx["vol_tag"] = ("放量" if vr >= 1.05 else ("缩量" if vr <= 0.95 else "平量")) if vr else None
        fg = p.get("fear_greed")
        ctx["fear_greed"] = fg
        ctx["qualify"] = (f"大盘评级 {p.get('grade')}"
                          + (f"、恐贪 {fg:.0f} 分({p.get('fear_greed_label')})" if fg is not None else ""))
    except Exception:  # noqa: BLE001
        ctx["qualify"] = "大盘数据暂缺"
    try:
        from app.support.mainline import market_style_bias
        ctx["style_tag"] = (market_style_bias() or {}).get("tag") or "均衡"
    except Exception:  # noqa: BLE001
        ctx["style_tag"] = "均衡"
    lu_n = (d.get("activity") or {}).get("real_limit_up",
                                         (d.get("activity") or {}).get("limit_up", 0)) or 0
    fg = ctx.get("fear_greed")
    if lu_n >= 60 or (fg is not None and fg >= 70):
        ctx["emotion_tag"] = "高温"
    elif lu_n >= 30 or (fg is not None and fg >= 45):
        ctx["emotion_tag"] = "中性"
    else:
        ctx["emotion_tag"] = "低温"
    return ctx


def generate_review(d: Dict) -> Dict:
    """由采集数据生成完整复盘(十大模块结构化 items + 全局速览表 + 30秒速览 ctx)。

    模块顺序(与页面渲染一致,Markdown 兼容):
      30秒速览 → 大盘综述(周期定位) → 板块轮动拆解(结构分析) → 主线三层分级研判
      → 明日重点观察标的池 → 核心事件解读 → 资金面与情绪面交叉验证
      → 当日决策效果验证 → 盘面核心结论 → 明日交易策略
    """
    overview = _index_overview(d)
    sector = _sector_rotation(d)
    from app.review.layers import layer_review, layer_summary
    from app.review.watch_pool import watch_pool_review
    from app.review.verify import verify_review
    from app.review.strategy_today import strategy_review
    from app.review.snapshot import build_snapshot
    layers_items = layer_review(d)
    watch_items = watch_pool_review(d)
    events = _events(d)
    capital = _capital_sentiment(d)
    verify_items = verify_review(d)
    conclusion = _conclusion(d)
    strategy_items = strategy_review(d)

    # 30秒速览 ctx(汇总各模块结论,置于报告最前)
    mctx = _market_ctx(d)
    lsum = layer_summary(d)
    core_names = "、".join(f"**{c}**" for c in lsum["core"]) or "暂无明确主线"
    pattern_txt = {"单主线抱团": "单主线高度聚焦", "多主线轮动": "多主线轮动并进",
                   "无明确主线": "无明确主线"}.get(lsum["pattern"], "")
    pos_txt, risk_txt = "见「明日交易策略」分区操作", "退潮主线不接力、无承接超跌不抄底"
    for it in strategy_items:
        if it.get("t") and "建议次日总仓位区间" in it["t"]:
            pos_txt = it["t"].split("建议次日总仓位区间 ")[-1].split("。")[0]
    for it in layers_items:
        if it.get("t") and "核心风险点" in it["t"]:
            risk_txt = it["t"].split(":", 1)[-1].strip()[:60]
    ctx = {
        "market": mctx,
        "core_names": core_names,
        "strength": lsum["strength"],
        "pattern": pattern_txt,
        "action": pos_txt,
        "risk": risk_txt,
        "headline": {},
    }
    snapshot_items = build_snapshot(ctx)

    sections = {
        "snapshot": {"title": "📋 30秒速览", "items": snapshot_items},
        "index_overview": {"title": "一、大盘综述(周期定位)", "items": overview},
        "sector_rotation": {"title": "二、板块轮动拆解(结构分析)", "items": sector},
        "layers": {"title": "三、主线三层分级研判", "items": layers_items},
        "watch_pool": {"title": "四、明日重点观察标的池", "items": watch_items},
        "events": {"title": "五、核心事件深度解读", "items": events},
        "capital_sentiment": {"title": "六、资金面与情绪面交叉验证", "items": capital},
        "verify": {"title": "七、当日决策效果验证", "items": verify_items},
        "conclusion": {"title": "八、盘面核心结论", "items": conclusion},
        "strategy": {"title": "九、明日交易策略", "items": strategy_items},
    }
    for sec in sections.values():
        sec["lines"] = _items_to_lines(sec["items"])

    return {
        "date": d.get("date"),
        "sections": sections,
        "tables": _tables(d),
        "snapshot_ctx": ctx,
        "generated_at": None,
    }
