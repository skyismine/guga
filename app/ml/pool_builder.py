"""训练池构建模块:统一输出训练标的列表与样本权重。

核心设计:
- 核心底仓 = 中证A500 历史逐期成分池(`a500_history_components.json`),每期成分记录
  起止日期,按交易日动态匹配"当日有效成分股",严禁用最新一期成分回溯全量历史,
  从根源消除幸存者偏差(调入/调出历史完整保留,调出股票仅用其有效期内样本)。
- 场景补充标的(`extra_train_stocks.json`):情绪弹性(连板/主线强势中小市值) +
  风险样本(连续跌停) + 大盘龙头(沪深300核心龙头),自动去重,总量约 650-700 只。
- 样本加权:
  - 类别平衡加权:按训练集三分类占比反推类别权重,缓解震荡类多数类偏向;
  - 主线标的加权:曾属 core 主线板块的标的权重 * mainline_boost_weight(默认 1.2)。

对外仅输出:
  - build_training_pool() -> {"codes": [...], "categories": {code: scene}, ...}
  - sample_weights(train_df, categories, mainline_codes) -> np.ndarray 样本权重
"""
import datetime as dt
import json
import os
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from app import config

# ---------------------------------------------------------------- 默认配置(内置默认值)
# 数据文件路径(相对项目根)
A500_PATH = os.path.join(config.DATA_DIR, "a500_history_components.json")
EXTRA_PATH = os.path.join(config.DATA_DIR, "extra_train_stocks.json")
POOL_CACHE_PATH = os.path.join(config.DATA_DIR, "gbm_train_pool.json")

MIN_LIST_DAYS = 120            # 上市不满该天数视为次新股,剔除
MAX_MISSING_RATIO = 0.20       # 数据缺失率超过该阈值剔除
EMOTIONAL_COUNT = 100          # 情绪弹性补充标的默认数量
RISK_COUNT = 50                # 风险样本补充标的默认数量
LARGE_CAP_COUNT = 30           # 大盘龙头补充标的默认数量
MAINLINE_BOOST_WEIGHT = 1.2    # core 主线标的样本权重

# 沪深A股代码前缀(排除 B股/退市/三板)
_LIQUID_PREFIXES = ("60", "00", "30", "68")


def _clean_name(name: str) -> str:
    return str(name or "").replace("*", "").replace(" ", "").upper()


def _is_ok_code(code: str) -> bool:
    code = str(code).zfill(6)
    return code.startswith(_LIQUID_PREFIXES)


def _is_ok_name(name: str) -> bool:
    n = _clean_name(name)
    if n.startswith("ST") or n.startswith("*ST") or "退" in n:
        return False
    if n in ("", "None", "NAN"):
        return False
    return True


# ---------------------------------------------------------------- 数据文件读写
def _read_json(path: str, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def load_a500_history() -> dict:
    """加载中证A500历史成分文件。

    标准格式:
    {
      "periods": [
        {"start": "2025-06-13", "end": "2025-12-12", "codes": ["000001", ...]},
        {"start": "2024-06-14", "end": "2025-06-12", "codes": [...]},
        ...
      ],          # 由新到旧排列;end 缺失表示至今
      "meta": {...}
    }
    若文件缺失,返回空 periods(训练池退化为仅补充标的,并打印警告)。
    """
    raw = _read_json(A500_PATH, {})
    periods = raw.get("periods") or []
    out = []
    for p in periods:
        start = str(p.get("start") or "")
        end = str(p.get("end") or "") or None
        codes = [str(c).zfill(6) for c in (p.get("codes") or [])]
        if not start or not codes:
            continue
        out.append({"start": start, "end": end, "codes": codes})
    return {"periods": out, "meta": raw.get("meta") or {}}


def load_extra_stocks() -> Dict[str, List[str]]:
    """加载场景补充标的。

    标准格式:
    {
      "emotional": ["300xxx", ...],   # 情绪弹性(连板/主线强势中小市值,非ST)
      "risk":       ["600xxx", ...],   # 风险样本(连续跌停,非ST)
      "large_cap":  ["601xxx", ...]    # 大盘龙头(沪深300行业龙头,非A500成分)
    }
    """
    raw = _read_json(EXTRA_PATH, {})
    return {k: [str(c).zfill(6) for c in (raw.get(k) or [])]
            for k in ("emotional", "risk", "large_cap")}


# ---------------------------------------------------------------- 幸存者偏差消除
def a500_active_periods(code: str, periods: List[dict]) -> List[Tuple[str, Optional[str]]]:
    """返回该股票在中证A500中的有效成分区间列表(可多段,因调入调出)。

    核心:幸存者偏差处理。对每期成分,仅当股票在本期名录内才有该期有效区间;
    调出后不再有后续区间,因此样本不会用"新成分身份"回溯其调出后的历史。
    """
    code = str(code).zfill(6)
    ranges = []
    for p in periods:
        if code in set(p["codes"]):
            ranges.append((p["start"], p.get("end")))
    return ranges


def filter_df_by_active_periods(df: pd.DataFrame, code: str,
                                periods: List[dict]) -> pd.DataFrame:
    """把个股日线裁剪到"在 A500 成分内"的有效交易日(幸存者偏差消除)。

    无任何有效区间(不在 A500 历史成分)的标的(即补充标的/龙头)返回原样。
    """
    ranges = a500_active_periods(code, periods)
    if not ranges:
        return df
    if df is None or df.empty:
        return df
    masks = []
    idx = pd.DatetimeIndex(df.index)
    for start, end in ranges:
        m = idx >= pd.Timestamp(start)
        if end:
            m &= idx <= pd.Timestamp(end)
        masks.append(m)
    keep = masks[0]
    for m in masks[1:]:
        keep = keep | m
    return df[keep]


# ---------------------------------------------------------------- 过滤
def _filter_candidates(codes: List[str], names: Dict[str, str],
                       hist_len: Dict[str, int]) -> List[str]:
    """统一过滤:ST/退市/非A股前缀 + 数据不足/缺失率超标。"""
    out = []
    for c in codes:
        c = str(c).zfill(6)
        if not _is_ok_code(c):
            continue
        nm = names.get(c, "")
        if nm and not _is_ok_name(nm):
            continue
        if c in hist_len and hist_len[c] < MIN_LIST_DAYS:
            continue
        out.append(c)
    return out


def build_training_pool(verbose: bool = True) -> dict:
    """构建最终训练池。

    合并 A500 历史成分(全期去重) + 三类补充标的,自动去重、过滤,
    输出 {"codes": [...], "categories": {code: "core_a500"/"emotional"/...},
          "a500_periods": periods, "stats": {...}}。
    注意:A500 核心池按"去重后的全集"输出(训练时再由各标的有效区间裁剪样本),
    因此 stock_count.core_a500 为去重后 A500 标的数,而非每期 500。
    """
    a500 = load_a500_history()
    extra = load_extra_stocks()
    periods = a500["periods"]

    # A500 历史成分全集(去重)
    a500_codes: List[str] = []
    for p in periods:
        for c in p["codes"]:
            if c not in a500_codes:
                a500_codes.append(c)
    a500_codes = _filter_candidates(a500_codes, {}, {})

    # 三类补充标的(按配置截取数量,自动去重且不与核心池重复)
    categories: Dict[str, str] = {}
    order = ("emotional", "risk", "large_cap")
    counts = {"emotional": EMOTIONAL_COUNT, "risk": RISK_COUNT,
              "large_cap": LARGE_CAP_COUNT}
    extra_total = 0
    for scene in order:
        picked = []
        for c in extra.get(scene) or []:
            c = str(c).zfill(6)
            if c in a500_codes or c in categories:
                continue
            picked.append(c)
        picked = picked[: counts[scene]]
        for c in picked:
            categories[c] = scene
        extra_total += len(picked)

    codes = list(a500_codes) + list(categories.keys())
    # categories 里核心池单独标记(补充标的已标记)
    cat = {"core_a500": "core_a500"}
    for c in codes:
        if c not in categories:
            categories[c] = "core_a500"

    stats = {
        "core_a500": len(a500_codes),
        "emotional": sum(1 for c in categories.values() if c == "emotional"),
        "risk": sum(1 for c in categories.values() if c == "risk"),
        "large_cap": sum(1 for c in categories.values() if c == "large_cap"),
        "extra_total": extra_total,
        "total": len(codes),
        "a500_periods": len(periods),
    }
    result = {"codes": codes, "categories": categories,
              "a500_periods": periods, "stats": stats}
    if verbose:
        print(f"[训练池] A500核心 {stats['core_a500']} + 补充 {stats['extra_total']} "
              f"(情绪{stats['emotional']}/风险{stats['risk']}/龙头{stats['large_cap']})"
              f" = {stats['total']} 只, A500调样 {stats['a500_periods']} 期")
    try:
        with open(POOL_CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
    except OSError:
        pass
    return result


def load_training_pool() -> dict:
    """加载缓存训练池;缺失时重新构建。"""
    cached = _read_json(POOL_CACHE_PATH, None)
    if cached and cached.get("codes"):
        return cached
    return build_training_pool(verbose=False)


# ---------------------------------------------------------------- 样本加权
def mainline_codes_from_snapshots() -> set:
    """从历史主线快照解析"曾属 core 主线"的标的代码(用于主线标的加权)。

    数据源:data_cache/review/targets_*.json(每日主线→标的映射,core 角色的标的)。
    快照缺失时返回空集(权重退化为纯类别平衡,不阻塞训练)。
    """
    codes: set = set()
    import glob
    pattern = os.path.join(config.DATA_DIR, "review", "targets_*.json")
    for path in sorted(glob.glob(pattern)):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            continue
        for s in data.get("sectors") or []:
            role = str(s.get("role", "")).lower()
            if role in ("core", "核心", "core_mainline"):
                c = str(s.get("code") or "").zfill(6)
                if c and _is_ok_code(c):
                    codes.add(c)
    return codes


def mainline_codes_from_concept(verbose: bool = True) -> set:
    """从主线系统当前核心板块的成分解析核心标的(备用源,概念映射本地缓存)。

    当 targets_*.json 无历史 core 记录时,用「最近一个交易日主线核心板块」的成分股
    作为主线标的近似(概念成分映射 concept_map.json 按日更新)。失败返回空集。
    """
    codes: set = set()
    try:
        from app.support.mainline import _last_scores
        # 尝试读取最近核心板块
        last = _last_scores.get("items") or {}
        core_names = [n for n, it in last.items()
                      if (it or {}).get("level") in ("core",)]
        if not core_names:
            from app.support.mainline_stabilizer import get_output
            stable = (get_output() or {}).get("stable") or {}
            core = stable.get("core") or {}
            if core:
                core_names = [core["name"]]
        for nm in core_names:
            from app.support.mainline import _concept_cons
            for c in _concept_cons(nm, allow_net=False):
                if _is_ok_code(c):
                    codes.add(c)
    except Exception as e:  # noqa: BLE001
        if verbose:
            print(f"[训练池] 主线标的解析失败,退化为纯类别平衡: {e}")
    return codes


def mainline_codes() -> set:
    """主线标的集合:优先历史快照,其次当前主线概念成分。"""
    codes = mainline_codes_from_snapshots()
    if codes:
        return codes
    return mainline_codes_from_concept()


def class_weights(data: pd.DataFrame, label_col: str = "label") -> Dict[int, float]:
    """类别平衡权重:权重大小与该类样本占比成反比(缓解多数类偏向)。

    w_c = 1 / (占比_c * K),K=类别数,使各类加权占比均等。
    """
    vc = data[label_col].value_counts()
    total = int(vc.sum())
    n_cls = int(vc.shape[0])
    if total == 0 or n_cls == 0:
        return {}
    return {int(c): float(total / (n_cls * vc[c])) for c in vc.index}


def sample_weights(train_df: pd.DataFrame, mainline_codes_: Optional[set] = None,
                   label_col: str = "label", code_col: str = "code") -> np.ndarray:
    """计算训练样本权重向量(与 train_df 行对齐)。

    权重 = 类别平衡权重 × (主线标的 ? MAINLINE_BOOST_WEIGHT : 1.0)
    所有权重视标量基准归一(均值=1),避免改变学习率/正则的绝对尺度。
    """
    cw = class_weights(train_df, label_col)
    if not cw:
        return np.ones(len(train_df))
    w = np.array([cw[int(l)] for l in train_df[label_col].values], dtype=float)
    mline = mainline_codes_ or set()
    if mline and code_col in train_df.columns:
        boost = (train_df[code_col].isin(mline)).values.astype(float)
        w = w * (1.0 + (MAINLINE_BOOST_WEIGHT - 1.0) * boost)
    w = w / (w.mean() + 1e-9)
    return w


# ---------------------------------------------------------------- 入口
def pool_report() -> dict:
    """训练池摘要(供训练报告/页面展示)。"""
    pool = load_training_pool()
    return pool.get("stats") or {}


if __name__ == "__main__":
    r = build_training_pool()
    print(f"\n[训练池] 共 {r['stats']['total']} 只")
    from collections import Counter
    print(Counter(r["categories"].values()))
