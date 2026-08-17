"""中证A500历史成分导入脚本:一键生成标准格式成分文件。

用法:
    python app/ml/import_a500.py            # 抓取当前成分作基线期 + 已有文件保留
    python app/ml/import_a500.py --reset     # 删除旧文件,仅从当前成分重建

标准输出格式(a500_history_components.json):
{
  "periods": [
    {"start": "2026-08-14", "end": null, "codes": ["000001", ...]},   # 当前有效期(新→旧)
    {"start": "2025-12-13", "end": "2026-06-12", "codes": [...]},     # 历史调样期(按需补充)
    ...
  ],
  "meta": {"source": "index_stock_cons_csindex(000510)", "created": "..."}
}

幸存者偏差说明:
- 每次中证A500定期调整都会生成新的一期(旧的冻结为历史期),训练时按交易日
  动态匹配"当日有效成分",调出股票只用其有效期内样本,绝不回溯。
- 当前脚本自动抓取「最新一期成分」作为 periods[0];历史调样期可通过
  `--periods-json path` 传入补充(标准格式列表),或手动编辑追加。
"""
import argparse
import datetime as dt
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from app import config  # noqa: E402

A500_PATH = os.path.join(config.DATA_DIR, "a500_history_components.json")
INDEX_CODE = "000510"   # 中证A500 指数代码(中证指数官网)


def fetch_current_constituents() -> list:
    """抓取中证A500最新一期成分(中证指数官网,akshare)。"""
    import akshare as ak
    df = ak.index_stock_cons_csindex(symbol=INDEX_CODE)
    # 列:指数代码/指数名称/成分券代码/成分券名称...
    col = None
    for c in df.columns:
        if "成分券代码" in str(c):
            col = c
            break
    if col is None:
        raise RuntimeError(f"未找到成分代码列: {list(df.columns)}")
    codes = [str(c).zfill(6) for c in df[col].tolist()]
    return sorted(set(codes))


def build_periods(extra_periods: list, created: str, current_start: str) -> list:
    """合并当前期 + 可选历史期,校验区间不重叠且按 start 降序。

    current_start:当前成分期生效起点。无历史调样时,设为训练数据可追溯起点
    (约 config.HIST_DAYS 交易日前),使当前 500 只成分覆盖全部训练历史。
    历史调样期补充后,旧成分自然按所在期匹配样本。
    """
    today = dt.date.today().isoformat()
    current = {"start": current_start, "end": None, "codes": None}
    periods = []
    for p in extra_periods or []:
        periods.append({
            "start": str(p["start"]), "end": str(p.get("end") or "") or None,
            "codes": [str(c).zfill(6) for c in (p.get("codes") or [])],
        })
    periods.append(current)  # 当前期放最后,处理时按 start 降序
    periods.sort(key=lambda x: x["start"], reverse=True)
    # 若历史期 end 与下一期 start 重叠,自动纠正(保证连续无重叠)
    for i in range(len(periods) - 1):
        nxt_start = periods[i]["start"]
        if periods[i]["end"] is not None and periods[i]["end"] >= nxt_start:
            # end 不得大于等于下一期 start,回退一日
            d = dt.date.fromisoformat(nxt_start) - dt.timedelta(days=1)
            periods[i]["end"] = d.isoformat()
    return periods


def default_current_start() -> str:
    """当前成分期默认起点:训练数据可追溯起点(约 HIST_DAYS 交易日前)。

    按自然日推算(周末≈交易日):HIST_DAYS 交易日 ≈ HIST_DAYS*7/5 自然日。
    """
    n = int(getattr(config, "HIST_DAYS", 600))
    d = dt.date.today() - dt.timedelta(days=int(n * 7 / 5) + 30)
    return d.isoformat()


def main():
    ap = argparse.ArgumentParser(description="导入中证A500历史成分")
    ap.add_argument("--reset", action="store_true", help="删除旧文件重建(仅当前成分)")
    ap.add_argument("--periods-json", default=None,
                    help="历史调样期 JSON 文件路径(可选),标准格式 periods 列表")
    args = ap.parse_args()

    if args.reset:
        if os.path.exists(A500_PATH):
            os.remove(A500_PATH)
            print(f"[导入] 已删除旧文件: {A500_PATH}")

    old = {}
    if os.path.exists(A500_PATH):
        try:
            with open(A500_PATH, encoding="utf-8") as f:
                old = json.load(f)
        except (OSError, ValueError):
            old = {}

    extra = list(old.get("periods") or [])
    if args.periods_json:
        with open(args.periods_json, encoding="utf-8") as f:
            extra += json.load(f)

    print("[导入] 抓取中证A500当前成分...")
    codes = fetch_current_constituents()
    print(f"[导入] 当前成分 {len(codes)} 只")

    created = dt.datetime.now().isoformat(timespec="seconds")
    periods = build_periods(extra, created, default_current_start())
    # 当前期 codes 由抓取结果填充
    for p in periods:
        if p["codes"] is None:
            p["codes"] = codes

    doc = {"periods": periods,
           "meta": {"source": f"index_stock_cons_csindex({INDEX_CODE})",
                    "created": created}}
    os.makedirs(os.path.dirname(A500_PATH), exist_ok=True)
    with open(A500_PATH, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1)
    print(f"[导入] 已写入 {A500_PATH}: {len(periods)} 期成分")
    for p in periods:
        print(f"  - {p['start']} ~ {p.get('end') or '至今'}: {len(p['codes'])} 只")


if __name__ == "__main__":
    main()
