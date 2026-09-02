"""复盘「数据校准与来源」: 以 fuyao(同花顺官方)行情为唯一校准基准。

设计原则:
- 一切行情口径以 fuyao(同花顺官方)为准, akshare 仅作兜底;
- 不使用「待核实」等模糊表述, 可自动修正的偏差在正文中说明修正结果;
- 数据来源如实列明, 便于复核。
"""


def data_calibration(d: dict) -> list:
    """生成「数据校准与来源」结构化 items。"""
    items = []
    items.append({"head": "数据校准(以 fuyao 同花顺官方行情为基准)"})
    idx = {x.get("name"): x for x in (d.get("indices") or [])}
    sh, sz = idx.get("上证指数"), idx.get("深证成指")
    if sh and sz and sh.get("turnover") and sz.get("turnover"):
        t = float(sh["turnover"]) + float(sz["turnover"])
        items.append({"t": f"两市成交额 **{t / 1e8:.0f} 亿**(沪 {float(sh['turnover']) / 1e8:.0f} 亿 + "
                           f"深 {float(sz['turnover']) / 1e8:.0f} 亿),取同花顺官方指数快照收盘口径,"
                           "盘中止损/止盈按实时价执行。"})
    elif sh and sh.get("turnover"):
        items.append({"t": f"沪市成交额 **{float(sh['turnover']) / 1e8:.0f} 亿**(fuyao 快照),"
                           "深市成交额数据暂缺,以沪市为基准。"})
    items.append({"t": "个股/板块行情: fuyao(同花顺官方)为主,akshare 兜底;"
                       "涨停池/连板梯队/热榜/龙虎榜/异动均取同花顺特色数据。"})
    items.append({"t": "个股口径: 历史日线统一前复权(qfq, 与 fuyao forward 一致, 最新价即不复权),"
                       "盘中实时价(新浪)与日线拼接时做一致性校验(双源同日价差/涨跌幅超限检测);"
                       "命中异常仅标注「数据可能异常,建议核实」不改预测, 除权除息日跳空属正常不告警。"})
    items.append({"t": "持仓一致性: 以交易流水 operations.jsonl 为唯一事实来源自动修正持仓快照,"
                       "市值以收盘价为基准,账户总资产 = 本金 + 已实现盈亏 + 浮盈。"})
    items.append({"t": "交易日口径: 按 fuyao 交易日历取最近交易日,交易时段内报告日期自动对齐当日;"
                       "缺失交易日(非交易日)自动顺延。"})
    items.append({"head": "数据来源"})
    items.append({"t": "· 指数/大盘/成交额: fuyao 同花顺官方 API,akshare 兜底。\n"
                       "· 板块/主线/涨停池: 全A快照 + 同花顺概念板块与涨停梯队。\n"
                       "· 资金: 板块主力资金净流入; 情绪: 涨跌比/涨停跌停家数/连板高度。\n"
                       "· 持仓/交易: 本地 portfolio.csv + operations.jsonl(人工录入)。"})
    return items
