"""模块5 风控与仓位校验:强制仓位风控,避免情绪化操作。

规则(默认值见 settings,可配置):
- 单只标的仓位 <= 总资金 single_pct(10%);
- 单一板块/赛道仓位 <= total 的 sector_pct(30%);
- 总仓位上限按市场恐贪档位动态调整;
- 浮亏超 loss_reduce_threshold 的标的,单次加仓不得超过原有仓位的 add_increase_cap。
"""
import csv
import os

from app import config
from app.data.fetcher import get_daily_history, get_spot_quotes
from app.features.concept_features import get_concepts, main_concept_sw
from app.features.market_features import market_snapshot
from app.support import settings as _st


def load_portfolio(path: str = None) -> list:
    """读取本地持仓 CSV。列:code, qty, cost, category(核心/波段/观察)。

    容错: 优先 utf-8-sig,失败自动回退 gbk(部分编辑器以 GBK/ANSI 保存中文 CSV);
    两种编码都失败返回空列表并告警(调用方不应据此覆盖原文件)。
    """
    path = path or _st.load().get("portfolio_path")
    if not os.path.exists(path):
        return []
    for enc in ("utf-8-sig", "gbk"):
        try:
            return _parse_portfolio(path, enc)
        except UnicodeDecodeError as e:
            print(f"[portfolio] {enc} 解码失败: {e}")
        except (OSError, ValueError) as e:
            print(f"[portfolio] 持仓读取失败({enc}): {e}")
            return []
    return []


def _parse_portfolio(path: str, enc: str) -> list:
    out = []
    with open(path, encoding=enc, newline="") as f:
        rows = csv.DictReader(f)
        for r in rows:
            code = str(r.get("code") or r.get("股票代码") or "").strip().zfill(6)
            if not code.isdigit():
                continue
            qty = float(r.get("qty") or r.get("持仓数量") or 0)
            cost = float(r.get("cost") or r.get("成本价") or 0)
            cat = (r.get("category") or r.get("持仓分类") or "观察").strip() or "观察"
            out.append({"code": code, "qty": qty, "cost": cost, "category": cat})
    return out


def save_portfolio(positions: list, path: str = None) -> str:
    path = path or _st.load().get("portfolio_path")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    # 覆盖前保留上一版备份(防止误覆盖/编码损坏导致数据丢失)
    if os.path.exists(path):
        try:
            os.replace(path, path + ".bak")
        except OSError:
            pass
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["code", "qty", "cost", "category"])
        for p in positions:
            w.writerow([p["code"], p["qty"], p["cost"], p.get("category", "观察")])
    return path


def quotes_of(codes) -> dict:
    """批量实时行情;缺失回退日线收盘价。"""
    codes = [str(c).zfill(6) for c in codes]
    out = {}
    try:
        out = get_spot_quotes(codes)
    except Exception as e:  # noqa: BLE001
        print(f"[risk] 实时行情失败: {e}")
    for c in codes:
        if c not in out or not out[c].get("price"):
            try:
                df = get_daily_history(c, days=5, adjust="qfq")
                out[c] = {"price": float(df["close"].iloc[-1]), "amount": 0.0,
                          "pct_chg": 0.0}
            except Exception:  # noqa: BLE001
                out[c] = {"price": 0.0, "amount": 0.0, "pct_chg": 0.0}
    return out


def _market_mood_cap(rules: dict, fg: float) -> float:
    table = rules["position_by_mood"]
    if fg is None:
        return rules["max_total_pct"]
    from app.features.market_features import fear_greed_label
    lab = fear_greed_label(fg)
    return table.get({
        "极度贪婪": "extreme_greed", "贪婪": "greed", "中性": "neutral",
        "恐慌": "fear", "极度恐惧": "extreme_fear",
    }.get(lab, "neutral"), rules["max_total_pct"])


def _sector_of(code):
    mc = main_concept_sw(code)
    return mc or (get_concepts(code) or [None])[0]


def _phase_risk() -> dict:
    """分阶段仓位矩阵(全局阶段唯一判定, 复用 engine.phase_cfg)。异常时回退原有规则兜底。"""
    try:
        from app.decision.engine import phase_cfg
        p = phase_cfg()
        return {"cap": p["cap"], "single_cap": p["single_cap"], "add_cap": p["add_cap"],
                "phase": p["phase"], "label": p["label"], "keynote": p["keynote"]}
    except Exception:  # noqa: BLE001
        rules = _st.load().get("risk", {}) or {}
        return {"cap": rules.get("max_total_pct", 0.5), "single_cap": rules.get("single_pct", 0.10),
                "add_cap": rules.get("add_increase_cap", 0.3), "phase": "main",
                "label": "主升发酵期", "keynote": "顺势加仓,持有为主"}


def validate(positions: list, total_asset: float = None,
             prices: dict = None, fg: float = None) -> dict:
    """校验持仓是否违反仓位规则(分阶段矩阵)。返回 {ok, rating, total_market_value,
    total_pct, single_violations, sector_violations, add_violations, tips}。"""
    cfg = _st.load()
    rules = cfg["risk"]
    ph = _phase_risk()
    positions = positions or []
    codes = [p["code"] for p in positions]
    prices = prices if prices is not None else quotes_of(codes)
    mv = [p["qty"] * float(prices.get(p["code"], {}).get("price", 0)) for p in positions]
    total_mv = sum(mv)
    total_asset = total_asset or total_mv or 1.0
    total_pct = total_mv / total_asset

    single, sector = [], {}
    add = []
    for p, v in zip(positions, mv):
        pct = v / total_asset if total_asset else 0.0
        code = p["code"]
        if pct > ph["single_cap"] + 1e-6:
            single.append({"code": code, "pct": round(pct, 4),
                           "limit": ph["single_cap"]})
        sec = _sector_of(code)
        if sec:
            sector.setdefault(sec, {"pct": 0.0, "codes": []})
            sector[sec]["pct"] += pct
            sector[sec]["codes"].append(code)
        # 加仓限制:浮亏超阈值
        loss = (float(prices.get(code, {}).get("price", 0)) - p["cost"]) / p["cost"] if p["cost"] else 0.0
        if loss <= -rules["loss_reduce_threshold"]:
            add.append({"code": code, "loss_pct": round(loss, 4),
                        "add_cap_ratio": rules["add_increase_cap"]})
    sec_v = [{"sector": k, **v, "limit": rules["sector_pct"]}
             for k, v in sector.items() if v["pct"] > rules["sector_pct"] + 1e-6]

    cap = ph["cap"]   # 分阶段总仓位上限(全系统天花板)
    total_ok = total_pct <= cap + 1e-6
    n_viol = len(single) + len(sec_v) + (0 if total_ok else 1) + len(add)
    rating = "低" if n_viol == 0 else ("中" if n_viol == 1 else "高")
    tips = []
    if not total_ok:
        tips.append(f"总仓位 {total_pct:.1%} 超过当前阶段({ph['label']})上限 {cap:.0%},建议降仓至 {cap:.0%} 以内")
    for s in single:
        tips.append(f"{s['code']} 仓位 {s['pct']:.1%} 超过单只上限 {s['limit']:.0%}")
    for s in sec_v:
        tips.append(f"{s['sector']} 合计仓位 {s['pct']:.1%} 超过板块上限 {s['limit']:.0%}")
    for a in add:
        tips.append(f"{a['code']} 浮亏 {a['loss_pct']:.1%},单次加仓不得超过现有仓位的 {a['add_cap_ratio']:.0%}")
    return {
        "ok": n_viol == 0,
        "rating": rating,
        "violations": n_viol,
        "total_market_value": round(total_mv, 2),
        "total_asset": round(total_asset, 2),
        "total_pct": round(total_pct, 4),
        "max_total_pct": cap,
        "single_violations": single,
        "sector_violations": sec_v,
        "add_violations": add,
        "tips": tips,
    }


def max_position(code: str, total_asset: float, price: float = None,
                 category: str = None) -> dict:
    """新开仓建议:返回该标的可买最大仓位(市值、股数、占资金比)。"""
    cfg = _st.load()
    rules = cfg["risk"]
    price = price or quotes_of([code]).get(code, {}).get("price", 0.0)
    if not price:
        return {"ok": False, "reason": "无法获取最新价"}
    cap_share = _phase_risk()["single_cap"]
    mv = total_asset * cap_share
    qty = int(mv / price // config.LOT_SIZE) * config.LOT_SIZE
    return {
        "ok": True,
        "code": code,
        "price": round(price, 2),
        "max_market_value": round(mv, 2),
        "max_shares": qty,
        "cost": round(qty * price, 2),
        "pct_of_asset": cap_share,
        "note": f"单只上限 {cap_share:.0%},对应可买 {qty} 股({round(qty*price,0):.0f} 元)",
    }


def position_rating(positions: list, total_asset: float = None) -> dict:
    """持仓整体风险评级(含市场情绪档位)。"""
    cfg = _st.load()
    try:
        snap = market_snapshot()
        fg = (snap.get("market") or {}).get("market_fear_greed")
    except Exception:  # noqa: BLE001
        fg = None
    res = validate(positions, total_asset=total_asset, fg=fg)
    res["fear_greed"] = fg
    return res
