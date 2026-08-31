"""A股每日复盘 · 文本生成:数据驱动的十一大模块深度复盘(参照人工复盘模板)。

每个部分输出结构化 items(文本行 / 小节标题 / 数据表格),页面按序交错渲染,提升可读性。
   📋 30秒速览(大盘定性/核心主线/仓位上限/核心风险)
   一、大盘综述(周期定位:量能/情绪/风格)
   二、板块轮动拆解(结构分析:核心主线/异动脉冲/退潮回落 + 驱动属性标签)
   三、主线三层分级研判(核心/发酵/观察 + 梯队/资金验证/生命周期/演进追踪/格局强度)
   四、明日重点观察标的池(三档梯队主线龙头 + 有承接超跌标的池)
   五、资金面与情绪面交叉验证(资金流向/市场情绪/背离检测/5日趋势)
   六、核心事件深度解读(要闻→对应板块/主线层级/差异化解读/共振判断)
   七、同花顺特色数据(连板梯队/热榜/龙虎榜/异动)
   八、当日决策效果验证(主线/标的/模型三档验证 + 持续性/降级联动)
   九、持仓与交易体系(账户/合规/逐仓方案/纪律)
   十、明日交易策略与开仓计划(主线分层策略 / 超跌策略 / 仓位与风控红线)
   十一、数据校准与来源(fuyao 基准 / 持仓一致性 / 来源列明)

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
            trend = "整体偏暖" if total > 0 else ("整体偏谨慎" if total < 0 else "中性")
            items.append(_t(f"近{len(valid_main)}个交易日大盘主力资金 **{up_days}/{len(valid_main)} 日净流入**,"
                            f"累计净流入 **{total:+.0f} 亿**,资金面{trend}。"))
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


# ---------------------------------------------------------------- 核心事件深度解读(去模板化)
def _event_note(kws) -> str:
    """差异化事件解读(按类型给明确方向, 删除万能套话)。"""
    k = set(kws)
    if k & {"央行", "降准", "降息", "LPR", "MLF", "货币政策"}:
        return "政策面:关注流动性与利率传导,利好成长与高股息"
    if k & {"证监会", "注册制", "两融", "退市", "印花税", "改革"}:
        return "政策面:资本市场制度信号,影响风险偏好与券商"
    if k & {"半导体", "芯片", "AI", "人工智能", "算力", "光模块", "机器人"}:
        return "产业催化:科技成长链条,关注次日板块资金验证"
    if k & {"业绩", "中报", "订单", "涨价", "招标", "扩产"}:
        return "基本面:关注业绩兑现与个股持续性"
    if k & {"美联储", "关税", "美元", "美债", "加息", "海外"}:
        return "外围:传导至外资与汇率,情绪扰动为主"
    if k & {"光伏", "锂电", "汽车", "地产", "消费", "医药", "新能源", "军工"}:
        return "行业景气:关注板块资金共振与龙头表现"
    return "市场关注度提升,结合次日竞价与板块资金验证"


def _events_v2(d: Dict) -> List[Dict]:
    """核心事件深度解读(升级版): 每条要闻标注对应板块/主线层级, 差异化解读, 共振判断。"""
    ev = d.get("events", {})
    hot = ev.get("hot", []) or []
    cctv = ev.get("cctv", []) or []
    items = []
    try:
        from app.review.layers import _tier_by_score
        from app.support import mainline as _ml
        rows = _ml.sector_scores(use_cache=True) or []
        level_map = {r["industry"]: _tier_by_score(r.get("score", 0))
                     for r in rows if r.get("level") != "rejected"}
    except Exception:  # noqa: BLE001
        level_map = {}
    if hot:
        items.append(_head("当日财经要闻(含对应板块/主线层级)"))
        erows = []
        for n in hot[:6]:
            title = n["title"]
            kws = n.get("keywords", []) or []
            # 对应板块: 用概念名关键词匹配要闻标题
            matched = [nm for nm in level_map
                       if any(k and k in title for k in [nm.replace("概念", "")])][:2]
            tier = ""
            if matched:
                _tier = level_map.get(matched[0], "")
                tier = {"core": "核心", "branch": "发酵", "watch": "观察"}.get(_tier, _tier)
            # 共振判断: 事件方向与当日板块资金方向
            reso = "待盘面资金验证"
            if matched:
                flows = {f["industry"]: f for f in d.get("sector_flow", [])}
                fl = flows.get(matched[0]) or {}
                if fl.get("pct_chg") is not None and fl["pct_chg"] > 0 and fl.get("net_yi", 0) >= 0:
                    reso = "共振(板块资金流入),次日延续概率高"
                elif fl.get("pct_chg") is not None and fl["pct_chg"] <= 0:
                    reso = "背离(以盘面资金为准)"
            erows.append([title[:26], "、".join(matched) or "-", tier or "-",
                          _event_note(kws), reso])
        items.append(_table("", ["要闻", "对应板块", "主线层级", "解读", "共振判断"], erows))
    if cctv:
        items.append(_head("央视《新闻联播》要点(市场化筛选)"))
        econ = [c for c in cctv if any(k in c["title"] for k in
                ("产业", "经济", "政策", "科技", "改革", "金融", "市场", "企业", "新能源", "消费"))][:4]
        if econ:
            for c in econ:
                items.append(_t(f"· {c['title']}"))
            items.append(_t("政策定调: 联播聚焦产业/经济/政策方向,相关赛道关注增量预期。"))
        else:
            items.append(_t("联播以政务/外交为主,无显著产业经济信号,政策面影响有限。"))
    if not hot and not cctv:
        items.append(_t("暂无当日驱动事件数据,结合盘后公告与政策动态补充判断。"))
    return items


def _tables(d: Dict) -> Dict:
    """页面顶部全局速览表数据(活动/板块/涨停/北向/市场日度)。"""
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


def _uniq_rows(rows: List[Dict], key: str) -> List[Dict]:
    """按字段去重(保留首个), 兜底缓存数据中偶发的重复板块行。"""
    seen, out = set(), []
    for r in rows:
        k = r.get(key)
        if k in seen:
            continue
        seen.add(k)
        out.append(r)
    return out


def generate_review(d: Dict) -> Dict:
    """由采集数据生成完整复盘(十一大模块结构化 items + 全局速览表 + 30秒速览 ctx)。

    模块顺序(与页面渲染一致,Markdown 兼容):
      30秒速览 → 大盘综述(周期定位) → 板块轮动拆解(结构分析) → 主线三层分级研判(含情绪锚点)
      → 明日重点观察标的池(三档梯队) → 资金面与情绪面交叉验证
      → 核心事件深度解读 → 同花顺特色数据(连板梯队/热榜/龙虎榜/异动) → 当日决策效果验证
      → 持仓与交易体系(账户/合规/逐仓方案/纪律) → 明日交易策略与开仓计划 → 数据校准与来源
    """
    # 数据归一: 板块资金/榜单去重(缓存数据可能含重复板块名),保证各模块口径一致
    d = dict(d)
    d["sector_flow"] = _uniq_rows(d.get("sector_flow") or [], "industry")
    d["sector_flow_5d"] = _uniq_rows(d.get("sector_flow_5d") or [], "industry")
    overview = _index_overview(d)
    sector = _sector_rotation(d)
    from app.review.layers import layer_review, layer_summary
    from app.review.watch_pool import watch_pool_review
    from app.review.verify import verify_review
    from app.review.strategy_today import strategy_review
    from app.review.snapshot import build_snapshot
    from app.review.special_data import special_data_review
    from app.review.positions import positions_review
    from app.review.data_calibration import data_calibration
    layers_items = layer_review(d)
    watch_items = watch_pool_review(d)
    capital = _capital_sentiment(d)
    events_items = _events_v2(d)
    ths_items = special_data_review(d)
    verify_items = verify_review(d)
    positions_items = positions_review(d)
    strategy_items = strategy_review(d)
    calibration_items = data_calibration(d)


    # 30秒速览 ctx(汇总各模块结论,置于报告最前)
    mctx = _market_ctx(d)
    lsum = layer_summary(d)
    core_names = "、".join(f"**{c}**" for c in lsum["core"]) or "暂无明确主线"
    risk_txt = "退潮主线不接力、无承接超跌不抄底"
    for it in layers_items:
        if it.get("t") and "核心风险点" in it["t"]:
            risk_txt = it["t"].split(":", 1)[-1].strip()[:60]
    _cap_txt = "阶段仓位上限见明日策略"
    try:
        from app.decision.engine import phase_cfg
        _cap_txt = f"{phase_cfg().get('cap', 0) * 100:.0f}%(当前阶段 {phase_cfg().get('label', '')})"
    except Exception:  # noqa: BLE001
        pass
    ctx = {
        "market": mctx,
        "core_names": core_names,
        "strength": lsum["strength"],
        "position_cap": _cap_txt,
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
        "capital_sentiment": {"title": "五、资金面与情绪面交叉验证", "items": capital},
        "events": {"title": "六、核心事件深度解读", "items": events_items},
        "ths_special": {"title": "七、同花顺特色数据(连板梯队/热榜/龙虎榜/异动)", "items": ths_items},
        "verify": {"title": "八、当日决策效果验证", "items": verify_items},
        "positions": {"title": "九、持仓与交易体系(账户/合规/逐仓方案/纪律)", "items": positions_items},
        "strategy": {"title": "十、明日交易策略与开仓计划", "items": strategy_items},
        "calibration": {"title": "十一、数据校准与来源", "items": calibration_items},
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
