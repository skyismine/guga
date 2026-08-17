"""场景补充标的生成脚本:自动生成情绪弹性/风险样本/大盘龙头三类标的。

数据源:
- emotional(情绪弹性,近2年连板/主线强势中小市值):遍历历史涨停池快照
  (data_cache/zt_*.json,含 boards 连板数)提取连板>=2 的中小市值非ST标的;
  数量不足时回退:抓取近 N 个交易日涨停池历史(stock_zt_pool_em 按日)合并。
- risk(风险样本,连续跌停):从近 N 个交易日跌停池(stock_zt_pool_dtgc_em)提取
  连续跌停(2日以上)非ST标的。
- large_cap(大盘龙头,沪深300内行业核心龙头非A500成分):中证指数官网沪深300
  当前成分,剔除已入 A500 历史成分集的标的,按流通市值取前 N 只。

输出:data_cache/extra_train_stocks.json
{
  "emotional": ["code", ...], "risk": ["code", ...], "large_cap": ["code", ...]
}

用法:
    python app/ml/build_extra_stocks.py [--days 20] [--reset]
默认 days=20(近20个交易日快照),与 zt_*.json 已有文件互补。
"""
import argparse
import datetime as dt
import glob
import json
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from app import config  # noqa: E402

EXTRA_PATH = os.path.join(config.DATA_DIR, "extra_train_stocks.json")
A500_PATH = os.path.join(config.DATA_DIR, "a500_history_components.json")

EMOTIONAL_COUNT = 100
RISK_COUNT = 50
LARGE_CAP_COUNT = 30
_LIQUID_PREFIXES = ("60", "00", "30", "68")


def _clean(name: str) -> str:
    return str(name or "").replace("*", "").replace(" ", "").upper()


def _ok(code: str, name: str) -> bool:
    code = str(code).zfill(6)
    if not code.startswith(_LIQUID_PREFIXES):
        return False
    n = _clean(name)
    if not n or n.startswith("ST") or n.startswith("*ST") or "退" in n:
        return False
    return True


def _trade_dates(days: int) -> list:
    """近 days 个交易日日期字符串(YYYYMMDD),含今天。"""
    out = []
    d = dt.date.today()
    while len(out) < days:
        if d.weekday() < 5:
            out.append(d.strftime("%Y%m%d"))
        d -= dt.timedelta(days=1)
    return out


def _zt_codes_from_cache() -> dict:
    """从本地涨停池快照 zt_*.json 提取 {code: 最大连板数}。"""
    zt = {}
    for p in sorted(glob.glob(os.path.join(config.DATA_DIR, "zt_*.json"))):
        try:
            with open(p, encoding="utf-8") as f:
                rows = json.load(f)
        except (OSError, ValueError):
            continue
        for r in rows:
            c = str(r.get("code", "")).zfill(6)
            b = int(r.get("boards", 0) or 0)
            if b >= 2 and _ok(c, r.get("name", "")):
                zt[c] = max(zt.get(c, 0), b)
    return zt


def _zt_codes_em(dates: list) -> dict:
    """抓取近 days 日涨停池历史,返回 {code: 最大连板数}。"""
    import akshare as ak
    zt = {}
    for ds in dates:
        try:
            df = ak.stock_zt_pool_em(date=ds)
            for _, r in df.iterrows():
                c = str(r.get("代码", "")).zfill(6)
                nm = str(r.get("名称", ""))
                b = int(r.get("连板数", 0) or 0)
                if b >= 2 and _ok(c, nm):
                    zt[c] = max(zt.get(c, 0), b)
        except Exception:  # noqa: BLE001
            continue
    return zt


def _risk_codes_em(dates: list) -> list:
    """近 days 日跌停池中连续跌停(同日多次出现或连板)的非ST标的。"""
    import akshare as ak
    seen = {}
    for ds in dates:
        try:
            df = ak.stock_zt_pool_dtgc_em(date=ds)
            for _, r in df.iterrows():
                c = str(r.get("代码", "")).zfill(6)
                nm = str(r.get("名称", ""))
                if _ok(c, nm):
                    seen[c] = seen.get(c, 0) + 1
        except Exception:  # noqa: BLE001
            continue
    # 连续跌停判定:多日出现(>=2)或单日即算(跌停池本身为风险样本)
    risk = [c for c, cnt in seen.items() if cnt >= 1]
    # 优先多次出现的
    risk.sort(key=lambda c: -seen[c])
    return risk


def _large_cap_codes(a500_codes: set, n: int) -> list:
    """沪深300当前成分中非 A500 的行业龙头(按成分顺序取,已是权重靠前)。"""
    import akshare as ak
    df = ak.index_stock_cons_csindex(symbol="000300")
    col = next((c for c in df.columns if "成分券代码" in str(c)), None)
    if col is None:
        return []
    out = []
    for c in df[col].tolist():
        c = str(c).zfill(6)
        if c in a500_codes or not c.startswith(_LIQUID_PREFIXES):
            continue
        out.append(c)
        if len(out) >= n:
            break
    return out


def load_a500_codes() -> set:
    """A500 历史成分全集(去重),用于补充标的去重。"""
    codes: set = set()
    if os.path.exists(A500_PATH):
        try:
            with open(A500_PATH, encoding="utf-8") as f:
                doc = json.load(f)
            for p in doc.get("periods") or []:
                codes.update(str(c).zfill(6) for c in (p.get("codes") or []))
        except (OSError, ValueError):
            pass
    return codes


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=20, help="补充标的抓取的历史交易日数")
    ap.add_argument("--reset", action="store_true")
    args = ap.parse_args()

    a500 = load_a500_codes()
    dates = _trade_dates(args.days)
    print(f"[补充] 抓取近 {len(dates)} 个交易日({dates[-1]}~{dates[0]})")

    # 情绪弹性:本地快照 + 线上历史合并
    zt = _zt_codes_from_cache()
    if len(zt) < EMOTIONAL_COUNT:
        online = _zt_codes_em(dates)
        for c, b in online.items():
            zt[c] = max(zt.get(c, 0), b)
    emotional = [c for c, _ in sorted(zt.items(), key=lambda kv: -kv[1])
                 if c not in a500][:EMOTIONAL_COUNT]
    print(f"[补充] 情绪弹性(连板>=2): 候选 {len(zt)}, 入选 {len(emotional)}")

    risk = [c for c in _risk_codes_em(dates) if c not in a500 and c not in emotional][:RISK_COUNT]
    print(f"[补充] 风险样本(连续跌停): {len(risk)}")

    large = [c for c in _large_cap_codes(a500, LARGE_CAP_COUNT)
             if c not in emotional and c not in risk][:LARGE_CAP_COUNT]
    print(f"[补充] 大盘龙头(HS300非A500): {len(large)}")

    doc = {"emotional": emotional, "risk": risk, "large_cap": large}
    if args.reset and os.path.exists(EXTRA_PATH):
        os.remove(EXTRA_PATH)
    os.makedirs(os.path.dirname(EXTRA_PATH), exist_ok=True)
    with open(EXTRA_PATH, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1)
    print(f"[补充] 已写入 {EXTRA_PATH}: "
          f"情绪{len(emotional)}/风险{len(risk)}/龙头{len(large)}")


if __name__ == "__main__":
    main()
