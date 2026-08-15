"""模块1 主线板块识别与龙头/中军/ETF 匹配 + 超跌强承接池。

打分维度(权重可配置,默认合计 100):
- 资金面 40:拆为 5日资金强度 + 单日资金强度,按准入板块净流入率排名线性递减打分;
  权重随市场评级动态切换(A级 5日20%+单日80%, C/D级 5日70%+单日30%, 默认 40%/60%)
- 趋势强度 30:板块涨跌幅 + 涨停家数
- 情绪共振 20:市场恐贪指数档位
- 消息催化 10:财联社电报标题命中概念关键词

准入规则:剔除 5日主力资金累计净流出的板块,以及 5日资金净流入但股价累计涨幅≤0 的量价背离板块,
淘汰板块附带 reject_reason。

输出:核心主线(Top N)/ 补涨支线(Top M)/ 观察,并匹配三类标的:
情绪龙头 / 中军(成交额最大) / 对应 ETF,附带预测概率、操作信号、支撑压力位。
"""
import os
import json
import time
import datetime as dt
import pandas as pd

from app import config
from app.data.fetcher import get_spot_quotes
from app.features.concept_features import _load_map
from app.features.market_features import market_snapshot, fear_greed_label
from app.ml.predictor import Predictor
from app.support import settings as _st
from app.support.portfolio import _one

_last_scores = {"date": None, "items": {}}
_grade_cache = {"date": None, "grade": "B"}


# ---------------------------------------------------------------- 数据源(带缓存)
def _today() -> str:
    return dt.date.today().strftime("%Y-%m-%d")


def _with_timeout(fn, timeout: float, default, name="task"):
    """在子线程中执行网络调用并限时;超时返回默认值(线程随后静默结束)。"""
    import threading
    box = {"v": default, "err": None}

    def _run():
        try:
            box["v"] = fn()
        except Exception as e:  # noqa: BLE001
            box["err"] = str(e)
    th = threading.Thread(target=_run, daemon=True, name=name)
    th.start()
    th.join(timeout)
    if box["err"]:
        print(f"[mainline] {name} 失败: {box['err']}")
    return box["v"]


def _batch_spot(codes: list, size: int = 80) -> dict:
    """分批获取实时行情(降级路径,替代全A快照)。"""
    from app.data.fetcher import get_spot_quotes
    out = {}
    for i in range(0, len(codes), size):
        try:
            out.update(get_spot_quotes(codes[i:i + size]))
        except Exception:  # noqa: BLE001
            continue
    return {c: {"name": q.get("name", ""), "price": q["price"],
                "pct_chg": q["pct_chg"], "amount": q.get("amount", 0.0),
                "turnover": 0.0, "total_mv": None, "float_mv": None}
           for c, q in out.items() if q and q.get("price")}


def _try_trade_date() -> str:
    try:
        import akshare as ak
        cal = ak.tool_trade_date_hist_sina()
        today = dt.date.today()
        traded = pd.to_datetime(cal["trade_date"]).dt.date
        traded = traded[traded <= today]
        return str(traded.iloc[-1])
    except Exception:  # noqa: BLE001
        return _today()


def _a_spot_map(refresh=False) -> dict:
    """全 A 快照:{code: {...}}。东财接口限流时自动降级新浪分页拉取;再失败返回空。"""
    path = os.path.join(config.DATA_DIR, f"spot_{_today()}.json")
    if not refresh and os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            pass

    def _fetch_em():
        import akshare as ak
        df = ak.stock_zh_a_spot_em()
        out = {}
        for _, r in df.iterrows():
            try:
                code = str(r.iloc[1]).zfill(6)
                out[code] = {
                    "name": str(r.iloc[2]),
                    "price": float(r.iloc[3] or 0),
                    "pct_chg": float(r.iloc[4] or 0),
                    "amount": float(r.iloc[7] or 0),
                    "turnover": float(r.iloc[15] or 0),
                    "total_mv": float(r.iloc[17] or 0) / 1e8,
                    "float_mv": float(r.iloc[18] or 0) / 1e8,
                }
            except (IndexError, TypeError, ValueError):
                continue
        return out

    def _fetch_sina():
        import requests
        base = ("https://vip.stock.finance.sina.com.cn/quotes_service/"
                "api/json_v2.php/Market_Center.getHQNodeData")
        out = {}
        for page in range(1, 81):
            try:
                r = requests.get(base, params={
                    "page": page, "num": 80, "sort": "symbol", "asc": 1,
                    "node": "hs_a", "symbol": "", "_s_r_a": page}, timeout=8)
                r.encoding = "utf-8"
                data = r.json()
            except Exception:  # noqa: BLE001
                break
            if not data:
                break
            for d in data:
                try:
                    code = str(d.get("code", "")).zfill(6)
                    out[code] = {
                        "name": str(d.get("name", "")),
                        "price": float(d.get("trade") or 0),
                        "pct_chg": float(d.get("changepercent") or 0),
                        "amount": float(d.get("amount") or 0),
                        "turnover": float(d.get("turnoverratio") or 0),
                        "total_mv": None, "float_mv": None,
                    }
                except (TypeError, ValueError):
                    continue
            time.sleep(0.08)
        return out

    out = _with_timeout(_fetch_em, 30, {}, name="a_spot")
    if not out:
        out = _with_timeout(_fetch_sina, 50, {}, name="a_spot_sina")
    if out:
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(out, f, ensure_ascii=False)
        except OSError:
            pass
    return out


def _zt_pool(date=None, refresh=False) -> list:
    """当日涨停池个股:[{code, name, pct, reason, boards, float_yi}]。

    boards=连板数(炸板重来按当前连板), float_yi=流通市值(亿)。
    旧缓存文件缺字段时按 boards=1 / float_yi=0 兜底,不阻塞读取。
    """
    date = date or _try_trade_date()
    path = os.path.join(config.DATA_DIR, f"zt_{date}.json")
    if not refresh and os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            pass
    import akshare as ak

    def _fetch():
        df = ak.stock_zt_pool_em(date=date.replace("-", ""))
        out = []
        cols = {str(c): i for i, c in enumerate(df.columns)}
        idx_code = cols.get("代码", 1)
        idx_name = cols.get("名称", 2)
        idx_pct = cols.get("涨跌幅", 3)
        idx_reason = cols.get("涨停统计", cols.get("所属行业", -1))
        idx_board = cols.get("连板数", -1)
        idx_float = cols.get("流通市值", -1)
        for _, r in df.iterrows():
            try:
                reason = str(r.iloc[idx_reason] or "") if idx_reason >= 0 else ""
                board = int(float(r.iloc[idx_board] or 0)) if idx_board >= 0 else 1
                float_yi = float(r.iloc[idx_float] or 0) / 1e8 if idx_float >= 0 else 0.0
                out.append({"code": str(r.iloc[idx_code]).zfill(6), "name": str(r.iloc[idx_name]),
                            "pct": float(r.iloc[idx_pct] or 0), "reason": reason,
                            "boards": board, "float_yi": round(float_yi, 2)})
            except (IndexError, TypeError, ValueError):
                continue
        return out

    out = _with_timeout(_fetch, 20, [], name="zt_pool")
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False)
    except OSError:
        pass
    return out


def _news_mentions() -> dict:
    """财联社电报中命中概念名的次数(当日缓存)。

    容错设计:
    - 财联社电报超时/失败时,降级用东财要闻(stock_info_global_em)统计;
    - 两级来源均失败才返回空 dict;
    - 失败不落盘空缓存,次日/下次调用自动重试;
    - 失败原因打印日志,便于与"真无相关新闻"区分。
    """
    path = os.path.join(config.DATA_DIR, f"news_{_today()}.json")
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                cached = json.load(f)
            if cached:
                return cached
        except (OSError, ValueError):
            pass

    def _count_cls():
        import akshare as ak
        df = ak.stock_info_global_cls(symbol="全部")
        text = ""
        for _, r in df.iterrows():
            text += " " + str(r.iloc[1]) + " " + str(r.iloc[2])
        return _count_mentions(text)

    def _count_em():
        import akshare as ak
        df = ak.stock_info_global_em()
        text = ""
        for _, r in df.iterrows():
            title = str(r.get("标题", ""))
            summary = str(r.get("摘要", ""))
            text += " " + title + " " + summary
        return _count_mentions(text)

    def _count_mentions(text: str) -> dict:
        out = {}
        for name in _all_concepts():
            n = text.count(_concept_kw(name))
            if n > 0:
                out[name] = n
        return out

    out = _with_timeout(_count_cls, 20, None, name="news_cls")
    if out is None:
        print("[mainline] 财联社电报超时/失败,降级东财要闻统计 news_mentions")
        out = _with_timeout(_count_em, 20, None, name="news_em")
    if out is None:
        print("[mainline] 东财要闻亦失败,news_mentions 为空(未落盘,下次自动重试)")
        return {}
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False)
    except OSError:
        pass
    return out


def _concept_kw(name: str) -> str:
    kw = name.replace("概念", "").replace("板块", "").strip()
    return kw or name


def _all_concepts() -> list:
    seen = set()
    for code, cs in _load_map().items():
        for c in cs:
            if c not in seen:
                seen.add(c)
    return list(seen)


_ALL_CONCEPTS = None
_ALL_CONCEPTS_MTIME = None


def _concepts_cached() -> list:
    global _ALL_CONCEPTS, _ALL_CONCEPTS_MTIME
    from app.features.concept_features import map_mtime
    mtime = map_mtime()
    if _ALL_CONCEPTS is None or mtime != _ALL_CONCEPTS_MTIME:
        _ALL_CONCEPTS = _all_concepts()
        _ALL_CONCEPTS_MTIME = mtime
    return _ALL_CONCEPTS


_cons_cache = {}
_cons_cache_mtime = None


def _concept_cons(name: str, allow_net: bool = True) -> list:
    """板块成分股(多策略):concept_map 精确 → 双向子串 → 东财概念成分接口兜底。

    概念映射按日重抓,缓存按 map_mtime 失效,映射变动后自动用新成分。
    """
    global _cons_cache, _cons_cache_mtime
    from app.features.concept_features import map_mtime
    mtime = map_mtime()
    if mtime != _cons_cache_mtime:
        _cons_cache.clear()
        _cons_cache_mtime = mtime
    if name in _cons_cache:
        return _cons_cache[name]
    codes = _concept_stocks(name)
    if not codes:
        for c in _concepts_cached():
            if name in c or c in name:
                codes += _concept_stocks(c)
        codes = list(dict.fromkeys(codes))
    if not codes and allow_net:
        def _fetch():
            import akshare as ak
            df = ak.stock_board_concept_cons_em(symbol=name)
            return [str(r.iloc[1]).zfill(6) for _, r in df.iterrows()
                    if str(r.iloc[1]).strip().isdigit()]
        codes = _with_timeout(_fetch, 12, [], name=f"cons:{name[:8]}")
    _cons_cache[name] = codes
    return codes


def _name_to_code(spot: dict) -> dict:
    out = {}
    for code, s in spot.items():
        if s.get("name"):
            out.setdefault(s["name"], code)
    return out


def _concept_stocks(name: str) -> list:
    return [c for c, cs in _load_map().items() if name in cs]


def _etf_map(refresh=False) -> dict:
    """全 ETF 快照:{name: {code, price, amount(万)}}。"""
    path = os.path.join(config.DATA_DIR, f"etf_{_today()}.json")
    if not refresh and os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            pass
    def _fetch():
        import akshare as ak
        df = ak.fund_etf_spot_em()
        out = {}
        for _, r in df.iterrows():
            try:
                name = str(r.iloc[1]); code = str(r.iloc[0]).zfill(6)
                out[name] = {"code": code, "price": float(r.iloc[3] or 0),
                             "amount_wan": float(r.iloc[7] or 0) / 1e4}
            except (IndexError, TypeError, ValueError):
                continue
        return out

    out = _with_timeout(_fetch, 30, {}, name="etf_spot")
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False)
    except OSError:
        pass
    return out


_ETF_ALIAS = {
    "人工智能": ["人工智能", "AI"], "AI": ["人工智能", "AI"],
    "算力": ["算力", "计算机"], "半导体": ["半导体", "芯片"],
    "芯片": ["半导体", "芯片"], "华为": ["芯片", "半导体"],
    "证券": ["证券", "非银"], "银行": ["银行"], "军工": ["军工", "国防"],
    "机器人": ["机器人"], "光伏": ["光伏"], "新能源": ["新能源", "电池"],
    "锂电池": ["电池", "新能源"], "储能": ["储能", "电池"],
    "白酒": ["白酒"], "医药": ["医药", "创新药"], "创新药": ["创新药", "医药"],
    "CRO": ["创新药", "CRO", "医药"],
    "黄金": ["黄金"], "有色": ["有色金属", "有色"], "煤炭": ["煤炭"],
    "房地产": ["房地产", "地产"], "国企改革": ["国企", "中特估"],
    "中特估": ["央企", "国企"], "红利": ["红利"], "消费": ["消费"],
    "农业": ["农业", "农林牧渔"], "汽车": ["汽车", "智能汽车"],
    "游戏": ["游戏", "传媒"], "传媒": ["传媒"], "数字经济": ["计算机", "数字"],
    "5G": ["5G", "通信"], "通信": ["通信"], "电力": ["电力", "公用"],
}


def _match_etf(name: str) -> dict | None:
    cfg = _st.load()
    min_wan = cfg.get("etf_min_amount", 5000.0)
    kws = _ETF_ALIAS.get(_concept_kw(name)) or [_concept_kw(name)]
    etfs = _etf_map()
    best = None
    for kw in kws:
        for en, e in etfs.items():
            if kw and kw.lower() not in en.lower():
                continue
            if e["amount_wan"] < min_wan:
                continue
            if best is None or e["amount_wan"] > best["amount_wan"]:
                best = {**e, "name": en}
    if best:
        best["matched_kw"] = kws
    return best


# ---------------------------------------------------------------- 打分
def _minmax(vals: list) -> dict:
    lo, hi = min(vals), max(vals)
    rng = hi - lo
    return {v: (v - lo) / rng if rng else 0.0 for v in vals}


# ---------------------------------------------------------------- 第五轮:扩展因子
def _extend_cfg() -> dict:
    """扩展因子配置(读取 settings.decision.mainline.extend_factor,缺省返回空)。"""
    ml = (_st.load().get("decision", {}).get("mainline", {}) or {})
    if not ml.get("enable_extend_factor", False):
        return {}
    return ml.get("extend_factor") or {}


def _zt_of_sector(name: str, zt: list, cons: list) -> list:
    """板块内涨停个股列表(code 命中成分)。"""
    cons_set = set(cons)
    return [z for z in zt if z.get("code") in cons_set]


def _sector_ladder(name: str, zt: list, cons: list, lcfg: dict) -> dict:
    """板块连板梯队:统计最高连板/各板位/断层/中军,输出 ladder_score(0~1) 与 tag。

    ladder_score = base(最高连板) + tier_bonus(板位覆盖) + zhongjun_bonus(中军涨停)
                   - gap_penalty(断层), 截断到 [0,1]。
    无连板时基础分很低但不为 0,允许趋势类机构赛道低分存续(禁止一票否决)。
    """
    stocks = _zt_of_sector(name, zt, cons)
    boards = [z.get("boards", 1) for z in stocks]
    max_b = max(boards) if boards else 0
    tiers = sorted({min(b, 4) for b in boards}) if boards else []
    zhongjun = any((z.get("float_yi") or 0) >= float(lcfg.get("zhongjun_float_yi", 100.0))
                   for z in stocks)
    gap = False
    if max_b >= int(lcfg.get("gap_from_board", 3)):
        # 有高位板但缺中间某板位 => 断层(如 3板却无 2板)
        gap = any(t not in tiers for t in range(1, max_b))
    base_map = lcfg.get("base_board") or {0: 0.10, 1: 0.50, 2: 0.75, 3: 0.88, 4: 1.00}
    base = float(base_map.get(min(max_b, 4), 0.10))
    tier = float(lcfg.get("tier_bonus", 0.06)) * max(0, len(tiers) - 1)
    zj = float(lcfg.get("zhongjun_bonus", 0.12)) if zhongjun else 0.0
    gap_pen = float(lcfg.get("gap_penalty", 0.10)) if gap else 0.0
    score = max(0.0, min(1.0, base + tier + zj - gap_pen))
    return {"score": round(score, 3), "max_board": max_b, "tiers": tiers,
            "gap": gap, "zhongjun": zhongjun,
            "tag": _ladder_tag(max_b, gap, zhongjun)}


def _ladder_tag(max_b: int, gap: bool, zhongjun: bool) -> str:
    parts = []
    if max_b <= 0:
        parts.append("无连板")
    else:
        parts.append(f"{max_b}板梯队")
    if gap:
        parts.append("断层")
    if zhongjun:
        parts.append("有中军")
    return "·".join(parts)


def _sector_size_bias(name: str, zt: list, cons: list, lcfg: dict) -> int:
    """板块市值风格:按板块内涨停股流通市值中位数判定 -1大盘 / 0均衡 / 1小盘。
    无涨停数据时返回 0(不参与风格偏转排序)。"""
    stocks = _zt_of_sector(name, zt, cons)
    if not stocks:
        return 0
    fv = sorted((z.get("float_yi") or 0) for z in stocks)
    med = fv[len(fv) // 2]
    th = float(lcfg.get("zhongjun_float_yi", 100.0))
    if med >= th:
        return -1
    if med <= th * 0.5:
        return 1
    return 0


_style_cache = {"date": None, "data": None}


def market_style_bias(refresh: bool = False) -> dict:
    """全局大小盘风格偏转(沪深300 vs 中证2000 相对动量)。

    返回 {bias, tag, mom, rel_score, date}:bias∈{-1,0,1} = 大盘/均衡/小盘。
    数据源:小盘指数优先中证2000(东财 932000);东财被限流/新浪无该指数时,
    依次退化为中证1000(sh000852)/中证500(sh000905)作小盘代理(输出 small_symbol 标注),
    大盘指数为沪深300(sh000300)。全部不可用时降级为 0(均衡),不影响打分。
    """
    global _style_cache
    today = _today()
    if not refresh and _style_cache["date"] == today and _style_cache["data"]:
        return _style_cache["data"]
    out = {"bias": 0, "tag": "均衡", "mom": {}, "rel_score": 0.0, "date": today,
           "big_symbol": "", "small_symbol": ""}
    try:
        import akshare as ak
        scfg = _extend_cfg().get("style") or {}
        if not scfg.get("enabled", True):
            _style_cache.update(date=today, data=out)
            return out
        days = scfg.get("mom_days") or [10, 20]
        weights = scfg.get("mom_weight") or [0.5] * len(days)

        def _em_close(symbol: str):
            df = ak.index_zh_a_hist(symbol=symbol, period="daily")
            return pd.to_numeric(df["收盘"], errors="coerce").dropna()

        def _sina_close(symbol: str):
            from app.data.market import get_index_history
            return get_index_history(symbol)["close"]

        def _close_chain(em: tuple, sinas: tuple):
            for s in em:
                try:
                    return _em_close(s), s
                except Exception:  # noqa: BLE001
                    continue
            for s in sinas:
                try:
                    return _sina_close(s), s
                except Exception:  # noqa: BLE001
                    continue
            return None, None

        big, bsym = _close_chain(("000300",), ("sh000300",))
        small, ssym = _close_chain(("932000",),
                                   ("sh932000", "sz932000", "sh000852", "sh000905"))
        need = max(days) + 1
        if big is None or small is None or len(big) < need or len(small) < need:
            _style_cache.update(date=today, data=out)
            return out
        out["big_symbol"], out["small_symbol"] = bsym, ssym
        rel = {}
        for nd, wt in zip(days, weights):
            r_big = big.iloc[-1] / big.iloc[-1 - nd] - 1
            r_small = small.iloc[-1] / small.iloc[-1 - nd] - 1
            rel[f"{nd}d"] = round(float(r_small - r_big), 4)   # 正=小盘强
        score = sum(float(wt) * rel[f"{nd}d"] for nd, wt in zip(days, weights))
        thresh = float(scfg.get("bias_thresh", 0.02))
        if score >= thresh:
            out["bias"], out["tag"] = 1, "小盘风格"
        elif score <= -thresh:
            out["bias"], out["tag"] = -1, "大盘风格"
        else:
            out["bias"], out["tag"] = 0, "均衡"
        out["mom"], out["rel_score"] = rel, round(score, 4)
    except Exception as e:  # noqa: BLE001
        print(f"[mainline] market_style_bias 降级为均衡: {e}")
    _style_cache.update(date=today, data=out)
    return out


def _market_grade() -> str:
    """市场评级(A/B/C/D),按日缓存,驱动资金面动态权重。"""
    if _grade_cache["date"] == _today() and _grade_cache["grade"]:
        return _grade_cache["grade"]
    grade = "B"
    try:
        from app.decision.engine import market_permit
        grade = market_permit().get("grade", "B")
    except Exception:  # noqa: BLE001
        pass
    _grade_cache["date"] = _today()
    _grade_cache["grade"] = grade
    return grade


def _capital_split(grade: str) -> tuple:
    """资金面拆分(5日, 单日)权重。A级 5日20%/单日80%, C/D级 5日70%/单日30%, 默认(含B级) 5日40%/单日60%。
    开关 mainline_dynamic_weight 关闭时固定为 5日40%/单日60%。"""
    if not _st.load().get("mainline_dynamic_weight", True):
        return 0.40, 0.60
    return {"A": (0.20, 0.80), "C": (0.70, 0.30), "D": (0.70, 0.30)}.get(grade, (0.40, 0.60))


def sector_scores(use_cache=True, flows=None, flows_5d=None,
                  rank_delta_thresh=None, weaken_news_on_no_5d_money=False,
                  news_weak_ratio=0.25) -> list:
    """概念板块打分,返回按分数降序列表(含 level)。

    新增准入规则 + 资金面拆分:
    - 准入:剔除 5日主力资金累计净流出、以及 5日资金净流入但股价累计涨幅≤0 的量价背离板块;
    - 资金面(原 40 分)拆为 5日资金强度 + 单日资金强度,按准入板块的净流入率排名,第 1 名得满分、线性递减;
    - 动态权重:市场评级 A 级切换为 5日20%+单日80%,C/D 级切换为 5日70%+单日30%,其余默认 5日40%+单日60%。
    淘汰板块附 reject_reason,保留在返回列表尾部并标记 level='rejected'。

    防抖稳定器可选注入参数(均为追加式,不影响原逻辑;默认 None/False 时行为与原版完全一致):
    - flows: 单日资金流数据列表,为 None 时内部实时抓取(原逻辑);由稳定器注入「窗口平滑」后的单日资金
    - flows_5d: 5日资金流数据(列表或 {industry: row} 字典),为 None 时内部实时抓取;5日表稳定器不参与平滑
    - rank_delta_thresh: 排名打分阻尼阈值;相邻板块净流入率差 < 此值视为同档位、不做阶梯扣分,防微小抖动造成排名跳变
    - weaken_news_on_no_5d_money: 无5日资金净流入(net_5d_yi<=0)时,新闻催化满分降为低档位 news_weak_ratio,防消息脉冲
    """
    from app.review.data import collect_sector_flow, collect_sector_flow_5d
    w = _st.load().get("score_weights", {})
    fcfg = _st.load().get("decision", {}).get("fund", {})
    flows = collect_sector_flow() if flows is None else flows
    if not flows:
        return []
    if flows_5d is None:
        flows_5d = {f["industry"]: f for f in collect_sector_flow_5d()}
    elif not isinstance(flows_5d, dict):
        flows_5d = {f["industry"]: f for f in flows_5d}

    grade = _market_grade()
    w_5d, w_1d = _capital_split(grade)

    # ---- 准入过滤(5日资金) + 合并 5日累计数据(可配置开关)
    admission_enabled = fcfg.get("admission_enabled", True)
    admitted, rejected = [], []
    for f in flows:
        f5 = flows_5d.get(f["industry"])
        if f5 is None:
            rejected.append({**f, "level": "rejected", "fund_status": "数据缺失",
                             "reject_reason": "5日资金数据缺失,无法判定准入"})
            continue
        row = {**f, **{k: f5[k] for k in ("pct_5d", "net_5d_yi", "inflow_5d_yi", "outflow_5d_yi")}}
        if admission_enabled:
            net_5d_min = fcfg.get("admission_net_5d_min", 0.0)
            if row["net_5d_yi"] <= net_5d_min:
                rejected.append({**row, "level": "rejected", "fund_status": "流出",
                                 "reject_reason": f"5日主力资金累计净流出 {row['net_5d_yi']:.1f} 亿(准入剔除)"})
                continue
            pct_5d_min = fcfg.get("admission_min_pct_5d", 0.0)
            if row["pct_5d"] <= pct_5d_min:
                rejected.append({**row, "level": "rejected", "fund_status": "背离",
                                 "reject_reason": f"5日资金净流入但累计涨幅 {row['pct_5d']:+.2f}%,量价背离(准入剔除)"})
                continue
        admitted.append(row)
    if not admitted:
        _last_scores["date"] = _today()
        _last_scores["items"] = {}
        return rejected

    # ---- 资金面:净流入率 + 排名(仅准入板块参与)
    use_net_rate = fcfg.get("use_net_rate", True)
    for f in admitted:
        f["rate_5d"] = (f["net_5d_yi"] / (f["inflow_5d_yi"] + f["outflow_5d_yi"])
                        if (f["inflow_5d_yi"] + f["outflow_5d_yi"]) else 0.0)
        f["rate_1d"] = (f["net_yi"] / (f["inflow_yi"] + f["outflow_yi"])
                        if (f["inflow_yi"] + f["outflow_yi"]) else 0.0)
    if use_net_rate:
        order_5d = sorted(admitted, key=lambda x: x["rate_5d"], reverse=True)
        order_1d = sorted(admitted, key=lambda x: x["rate_1d"], reverse=True)
    else:
        order_5d = sorted(admitted, key=lambda x: x["net_5d_yi"], reverse=True)
        order_1d = sorted(admitted, key=lambda x: x["net_yi"], reverse=True)
    n = len(admitted)
    cap_total = w.get("capital", 40)
    sustain_th = fcfg.get("status_thresholds", {}).get("sustain", 0.0)

    # 排名打分阻尼:相邻板块净流入率差 < rank_delta_thresh 视为同档位(取前一档名次),
    # 不做阶梯扣分,防止盘中微小抖动造成名次互换、分数跳变。
    def _eff_rank(order, key):
        out, prev_val, prev_rank = {}, None, None
        for i, f in enumerate(order, 1):
            v = f.get(key)
            if (rank_delta_thresh and prev_val is not None and v is not None
                    and (prev_val - v) < rank_delta_thresh):
                out[f["industry"]] = prev_rank
            else:
                out[f["industry"]] = i
                prev_rank = i
            prev_val = v
        return out

    if rank_delta_thresh:
        rank5 = _eff_rank(order_5d, "rate_5d" if use_net_rate else "net_5d_yi")
        rank1 = _eff_rank(order_1d, "rate_1d" if use_net_rate else "net_yi")
    else:
        rank5 = {f["industry"]: i + 1 for i, f in enumerate(order_5d)}
        rank1 = {f["industry"]: i + 1 for i, f in enumerate(order_1d)}
    for f in admitted:
        r5 = rank5.get(f["industry"], order_5d.index(f) + 1)
        r1 = rank1.get(f["industry"], order_1d.index(f) + 1)
        f["fund_rank_5d"] = r5
        f["fund_rank_1d"] = r1
        f["fund_score_5d"] = round(cap_total * w_5d * (n - r5 + 1) / n, 2)
        f["fund_score_1d"] = round(cap_total * w_1d * (n - r1 + 1) / n, 2)
        f["fund_score"] = round(f["fund_score_5d"] + f["fund_score_1d"], 2)
        # 5日资金状态:持续流入(5日+单日均净流入) / 流入转弱(5日净流入但当日流出) / 流出(5日净流出)
        # 注:准入过滤已剔除 5日净流出与背离板块,故此处仅"持续流入/流入转弱"两类;背离保留在淘汰列表 fund_status
        f["fund_status"] = ("持续流入" if f["rate_1d"] > sustain_th else "流入转弱"
                            if f["rate_5d"] > sustain_th else "流出")

    fg = 50.0
    try:
        fg = float((market_snapshot() or {}).get("market", {}).get("market_fear_greed") or 50)
    except Exception:  # noqa: BLE001
        pass
    zt = _zt_pool()
    zt_set = {z["code"] for z in zt}
    zt_cnt = {f["industry"]: len(set(_concept_cons(f["industry"], allow_net=False)) & zt_set)
              for f in admitted}
    news = _news_mentions()

    # ---- 第五轮:扩展因子(连板梯队 + 板块市值风格) ----
    ext_cfg = _extend_cfg()
    lcfg = ext_cfg.get("ladder") or {}
    ladder_on = bool(lcfg.get("enabled", True) and ext_cfg)
    cons_cache = {f["industry"]: _concept_cons(f["industry"], allow_net=False) for f in admitted}
    ladder_map = {}
    size_map = {}
    if ladder_on:
        for f in admitted:
            name = f["industry"]
            ladder_map[name] = _sector_ladder(name, zt, cons_cache[name], lcfg)
            size_map[name] = _sector_size_bias(name, zt, cons_cache[name], lcfg)

    names = [f["industry"] for f in admitted]
    pcts = [f["pct_chg"] for f in admitted]
    pct_map = _minmax(pcts)

    rows = []
    for f in admitted:
        name = f["industry"]
        pct = pct_map[f["pct_chg"]]
        z = zt_cnt.get(name, 0)
        zt_norm = min(z, 8) / 8.0 if zt else pct
        if ladder_on:
            # trend = 当日涨跌幅归一*0.6 + 涨停家数*0.2 + ladder_score*0.2
            pct_w = float(lcfg.get("pct_w", 0.6))
            zt_w = float(lcfg.get("zt_w", 0.2))
            ld_w = float(lcfg.get("ladder_w", 0.2))
            ladder_score = ladder_map[name]["score"]
            trend = pct * pct_w + zt_norm * zt_w + ladder_score * ld_w
        else:
            trend = pct * 0.8 + 0.2 * zt_norm
        direction = 1.0 if f["pct_chg"] >= 0 else 0.5
        senti = (fg / 100.0) * direction
        news_s = 1.0 if news.get(name) else 0.0
        if (weaken_news_on_no_5d_money and news.get(name)
                and f.get("net_5d_yi", 0) <= 0):
            # 消息脉冲削弱:无5日资金净流入时,单日利好消息不配满分(防一日游行情抬升分数)
            news_s = news_weak_ratio
        score = (f["fund_score"] + w.get("trend", 30) * trend
                 + w.get("sentiment", 20) * senti + w.get("news", 10) * news_s)
        row = {**f, "score": round(score, 2), "zt_count": z, "news_hits": news.get(name, 0),
               "breakdown": {
                   "fund": f["fund_score"],
                   "fund_5d": f.get("fund_score_5d"),
                   "fund_1d": f.get("fund_score_1d"),
                   "trend": round(w.get("trend", 30) * trend, 2),
                   "sentiment": round(w.get("sentiment", 20) * senti, 2),
                   "news": round(w.get("news", 10) * news_s, 2),
               }}
        if ladder_on:
            row["ladder_score"] = ladder_map[name]["score"]
            row["ladder_tag"] = ladder_map[name]["tag"]
            row["ladder_detail"] = {k: ladder_map[name][k] for k in ("max_board", "tiers", "gap", "zhongjun")}
            row["size_bias"] = size_map[name]
        rows.append(row)
    rows.sort(key=lambda x: x["score"], reverse=True)
    _mark_levels(rows)
    rows.extend(rejected)
    _last_scores["date"] = _today()
    _last_scores["items"] = {r["industry"]: r["level"] for r in rows}
    return rows


def _mark_levels(rows: list) -> None:
    cfg = _st.load()
    top_n = cfg.get("mainline_top_n", 2)
    branch_n = cfg.get("mainline_branch_top_n", 5)
    for i, r in enumerate(rows):
        r["rank"] = i + 1
        r["level"] = "core" if i < top_n else ("branch" if i < branch_n else "watch")


def get_concepts_of(code: str) -> list:
    from app.features.concept_features import get_concepts
    return get_concepts(code)


def sector_level(name: str) -> str:
    if _last_scores["date"] != _today() or not _last_scores["items"]:
        try:
            sector_scores(use_cache=True)
        except Exception:  # noqa: BLE001
            return "watch"
    return _last_scores["items"].get(name, "watch")


def mainline_summary() -> dict:
    """Web 展示:主线列表 + 三类标的 + 超跌池 + 准入淘汰板块。"""
    scores = sector_scores(use_cache=True)
    items = []
    for r in scores[: max(_st.load().get("mainline_branch_top_n", 5), 5)]:
        items.append({
            "rank": r["rank"], "name": r["industry"], "level": r["level"],
            "score": r["score"], "pct_chg": r["pct_chg"], "net_yi": r["net_yi"],
            "zt_count": r["zt_count"], "leader": r.get("leader", ""),
            "news_hits": r["news_hits"], "fund_score": r.get("fund_score"),
            "fund_rank_1d": r.get("fund_rank_1d"), "fund_status": r.get("fund_status"),
            "rate_1d": r.get("rate_1d"), "rate_5d": r.get("rate_5d"),
            "fund_rank_5d": r.get("fund_rank_5d"),
            "targets": match_targets(r["industry"]),
        })
    rejected = [{"name": r["industry"], "pct_chg": r["pct_chg"], "net_yi": r["net_yi"],
                  "fund_status": r.get("fund_status"), "reason": r["reject_reason"]}
                 for r in scores if r.get("level") == "rejected"]
    try:
        fg = float((market_snapshot() or {}).get("market", {}).get("market_fear_greed") or 50)
    except Exception:  # noqa: BLE001
        fg = None
    return {
        "date": _today(),
        "fear_greed": fg,
        "fear_greed_label": fear_greed_label(fg) if fg is not None else None,
        "top_n": _st.load().get("mainline_top_n", 2),
        "market_grade": _grade_cache.get("grade", "B"),
        "items": items,
        "rejected": rejected,
        "oversold": oversold_pool(),
    }


# ---------------------------------------------------------------- 三类标的
def _filter_spot(spot: dict, stocks: list) -> list:
    cfg = _st.load()
    bad = cfg.get("leader_exclude", ["ST", "退"])
    min_mv = cfg.get("leader_min_market_cap", 20.0)
    out = []
    for c in stocks:
        s = spot.get(c)
        if not s or not s["price"]:
            continue
        if any(b in s["name"].upper() for b in bad):
            continue
        if s["float_mv"] and s["float_mv"] < min_mv:
            continue
        out.append({"code": c, **s})
    return out


def _match_stocks(name: str, spot: dict) -> list:
    """返回板块候选成分股(过滤 ST/市值)。spot 为空时走实时行情降级。"""
    cfg = _st.load()
    bad = cfg.get("leader_exclude", ["ST", "退"])
    stocks = _filter_spot(spot, _concept_cons(name)) if spot else []
    if not stocks:
        codes = _concept_cons(name)
        if not codes:
            return []
        stocks = [{"code": c, **v} for c, v in _batch_spot(codes).items() if v.get("price")]
        stocks = [s for s in stocks if not any(b in s["name"].upper() for b in bad)]
    return stocks


def match_targets(name: str, top_n: int = 3) -> dict:
    """匹配某板块:情绪龙头 / 中军 / ETF,附预测与支撑压力。"""
    spot = _a_spot_map()
    stocks = _match_stocks(name, spot)
    if not stocks:
        return {"name": name, "error": "无成分股数据(成分源暂不可用)"}

    emo = max(stocks, key=lambda s: s["pct_chg"])   # 情绪龙头:涨幅最大
    mid = max(stocks, key=lambda s: s["amount"])    # 中军:成交额最大
    etf = _match_etf(name)

    predictor = Predictor()
    quotes = get_spot_quotes([emo["code"], mid["code"]]) if len(stocks) >= 2 else {}
    out = []
    for tag, t in (("龙头", emo), ("中军", mid)):
        item = {"role": tag, "code": t["code"], "name": t["name"],
                "price": round(t["price"], 2), "pct_chg": t["pct_chg"],
                "amount_yi": round(t["amount"] / 1e8, 2),
                "float_mv": t["float_mv"]}
        try:
            _, pred, adv = _one(t["code"], predictor, quotes, None, _st.load())
            item.update({
                "p_up": round(pred["p_up"], 4), "direction": pred["direction_cn"],
                "action": adv["action_cn"], "levels": adv["levels"],
                "reasons": adv["reasons"][:3],
            })
        except Exception as e:  # noqa: BLE001
            item["error"] = str(e)
        out.append(item)
    if etf:
        out.append({"role": "ETF", "name": etf["name"], "code": etf["code"],
                    "price": round(etf["price"], 3), "amount_wan": round(etf["amount_wan"], 0),
                    "matched_kw": ",".join(etf["matched_kw"])})
    return {"name": name, "stocks": out, "count": len(stocks)}


# ---------------------------------------------------------------- 超跌强承接池
def _atr14(high, low, close):
    h, l, c = high.to_numpy(), low.to_numpy(), close.to_numpy()
    tr = []
    for i in range(1, len(c)):
        tr.append(max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])))
    if len(tr) < 14:
        return float("nan")
    return float(pd.Series(tr).tail(14).mean())


def oversold_pool(top_n: int = None, use_cache: bool = True) -> list:
    """超跌强承接池:近30日累计跌超阈值 + 当日放量站上5日线 + ATR 排除妖股波动。"""
    cfg = _st.load()
    top_n = top_n or cfg.get("oversold_pool_size", 15)
    ov = cfg.get("oversold", {})
    concept_codes = []
    try:
        top = sector_scores(use_cache=use_cache)[: max(_st.load().get("mainline_branch_top_n", 5), 5)]
        for r in top:
            concept_codes += _concept_cons(r["industry"])
    except Exception:  # noqa: BLE001
        pass
    zt_codes = [z["code"] for z in _zt_pool()]
    candidates, seen = [], set()
    for c in list(concept_codes) + zt_codes + list(config.TRAIN_STOCK_CODES):
        if c not in seen:
            seen.add(c)
            candidates.append(c)
    candidates = candidates[:1200]
    spot = _a_spot_map()

    hit = []
    for code in candidates:
        try:
            from app.data.fetcher import get_daily_history
            df = get_daily_history(code, days=70, adjust="qfq")
            if len(df) < 31:
                continue
            close = df["close"]
            ret30 = close.iloc[-1] / close.iloc[-31] - 1
            if ret30 > -ov.get("drop_30d", 0.30):
                continue
            vol = df["volume"]
            vol_ratio = vol.iloc[-1] / float(vol.iloc[-6:-1].mean()) if vol.iloc[-6:-1].mean() else 0
            if vol_ratio < ov.get("vol_ratio", 1.5):
                continue
            if close.iloc[-1] <= float(close.iloc[-5:].mean()):
                continue
            if close.iloc[-1] <= close.iloc[-2]:
                continue
            atr_pct = _atr14(df["high"], df["low"], close) / close.iloc[-1]
            if atr_pct > ov.get("max_atr_pct", 0.07):
                continue
            s = spot.get(code)
            name = (s or {}).get("name", code)
            if any(b in name.upper() for b in cfg.get("leader_exclude", ["ST", "退"])):
                continue
            pct_chg = (s or {}).get("pct_chg") if s else float(close.iloc[-1] / close.iloc[-2] - 1)
            hit.append({
                "code": code, "name": name,
                "price": round(float(close.iloc[-1]), 2),
                "pct_chg": round(float(pct_chg or 0), 4),
                "ret30": round(ret30, 4),
                "vol_ratio": round(vol_ratio, 2), "atr_pct": round(atr_pct, 4),
                "amount_yi": round((s or {}).get("amount", 0) / 1e8, 2) if s else None,
                "float_mv": (s or {}).get("float_mv") if s else None,
                "score": round(-ret30 * 100 + vol_ratio * 5 + max(pct_chg or 0, 0), 1),
            })
        except Exception:  # noqa: BLE001
            continue
    hit.sort(key=lambda x: (x["pct_chg"], x["score"]), reverse=True)
    hit = hit[: top_n * 2]

    predictor = Predictor()
    quotes = get_spot_quotes([h["code"] for h in hit]) if hit else {}
    out = []
    for h in hit[:top_n]:
        r = dict(h)
        try:
            _, pred, adv = _one(h["code"], predictor, quotes, None, cfg)
            r.update({"p_up": round(pred["p_up"], 4), "direction": pred["direction_cn"],
                      "action": adv["action_cn"], "levels": adv["levels"]})
        except Exception as e:  # noqa: BLE001
            r["error"] = str(e)
        out.append(r)
    return out


if __name__ == "__main__":
    import json
    print(json.dumps(mainline_summary(), ensure_ascii=False, indent=2)[:3000])
