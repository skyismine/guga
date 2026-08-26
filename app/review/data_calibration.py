"""P2 数据校准与来源标注: 核心指数收盘多源交叉校验,输出校准结论。

- 用 fuyao 指数快照(官方通道) vs 本系统 collect_indices(新浪实时/日线) 比对
  上证/深成/创业板收盘与涨跌幅,偏差 > 0.3% 时标注待核实,否则判定已校准;
- 涨跌停/成交额来源标注,供报告「数据来源」节使用。
- 全程容错: fuyao 不可用时判定"未交叉核实(单源)"。
"""
from app.review import operations


def _col(v, c: str = "") -> dict:
    return {"v": v, "c": c}


def _fuyao_index_snapshot() -> dict:
    """上证/深成/创业板指 收盘与涨跌幅(fuyao 官方通道,单次批量)。"""
    out = {}
    try:
        from app.data.fuyao import enabled as _fy_enabled
        from app.data.fuyao import _get
        if not _fy_enabled():
            return out
        data = _get("/api/a-share-index/prices/snapshot",
                    {"thscodes": "000001.SH,399001.SZ,399006.SZ"}, ttl=600) or {}
        for it in (data.get("item") or []):
            out[it.get("ticker")] = {"close": it.get("last_price"),
                                     "pct": it.get("price_change_ratio_pct")}
    except Exception as e:  # noqa: BLE001
        print(f"[calibration] fuyao 指数快照失败: {e}")
    return out


_INDEX_SYMBOL = {"上证指数": "000001", "深证成指": "399001", "创业板指": "399006"}


def data_calibration(d: dict) -> list:
    """生成「数据校准」items: 核心指数多源比对结论 + 来源清单。"""
    items = []
    fy = _fuyao_index_snapshot()
    indices = {i["name"]: i for i in d.get("indices", [])}
    rows = []
    if fy:
        for name, code in _INDEX_SYMBOL.items():
            f = fy.get(code)
            loc = indices.get(name) or {}
            if not f or not loc:
                continue
            d_close = abs((f.get("close") or 0) - (loc.get("close") or 0))
            d_pct = abs((f.get("pct") or 0) - (loc.get("pct_chg", 0) * 100 or 0))
            ok = d_close / (loc.get("close") or 1) < 0.003 and d_pct < 0.3
            rows.append([name, f"{loc.get('close') or '-':.2f}", f"{loc.get('pct_chg', 0) * 100:+.2f}%",
                         _col("已校准" if ok else "偏差待核实", "up" if ok else "down"),
                         _cell_note(d_close, d_pct)])
        items.append({"head": "核心指数收盘多源校准"})
        if rows:
            items.append({"table": {"title": "", "cols": ["指数", "收盘", "涨跌幅", "判定", "双源偏差"],
                                    "rows": rows}})
        else:
            items.append({"t": "指数校准数据暂缺。"})
    else:
        items.append({"head": "核心指数收盘多源校准"})
        items.append({"t": "未做多源交叉核实(单源口径,fuyao 指数快照不可用)。"})

    # 来源标注
    act = d.get("activity") or {}
    sources = [
        "行情快照/涨跌家数: 东财全A实时 + 新浪分页",
        "概念资金流/涨停池: 同花顺(akshare) + fuyao 官方兜底",
        "指数收盘: 新浪实时/日线 + fuyao 指数快照交叉校准",
        "要闻/联播: 东财快讯 + 央视联播",
    ]
    if act:
        sources.append(f"涨跌停: 涨停 {act.get('limit_up', '-')} / 跌停 {act.get('limit_down', '-')}(东财涨停池)")
    items.append({"head": "数据来源"})
    for s in sources:
        items.append({"t": f"· {s}"})
    return items


def _cell_note(d_close: float, d_pct: float) -> str:
    return f"收盘差 {d_close:.2f} / 涨跌差 {d_pct:.2f}pp"
