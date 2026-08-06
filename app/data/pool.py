"""训练池构建:覆盖不同行业/市值/风格的样本股。

从申万一级行业成分(`index_component_sw`)按"纳入权重"(≈ 流通市值权重)分层抽样:
- 每行业取权重最大的若干只(大盘蓝筹);
- 再取权重中位段的若干只(中盘/成长);
覆盖 31 个申万一级行业 + 大/中市值,天然包含价值与成长风格,显著提升模型泛化能力。

生成的股票池与"个股 -> 申万行业"映射本地缓存,训练/回测/信号统一使用。
"""
import json
import os
import time

import pandas as pd

from app import config
from app.features.industry_features import (SW_CODE_TO_NAME, _load_map,
                                            _save_map, _map_cache,
                                            STATIC_STOCK_INDUSTRY)
from app.config import _STATIC_TRAIN_CODES  # 静态基准池,避免自引用膨胀

_POOL_PATH = os.path.join(config.DATA_DIR, "train_pool.json")
_POOL_MIN_PER_INDUSTRY = 4   # 每行业大/中盘各取几只
_MAX_TOTAL = 130             # 池总量上限(留 2/3/688 覆盖科创板)
_LIQUID_PREFIXES = ("60", "00", "30", "68")  # 沪深主板/中小/创业/科创


def _clean_name(name: str) -> str:
    return str(name).replace("*", "").replace(" ", "").upper()


def _filter_ok(name: str, code: str) -> bool:
    n = _clean_name(name)
    if n.startswith("ST") or n.startswith("退") or "退" in n:
        return False
    return code.startswith(_LIQUID_PREFIXES)


def _industry_stocks(sw_code: str) -> pd.DataFrame:
    """某申万一级行业的成分股(代码/名称/权重)。"""
    import akshare as ak
    df = ak.index_component_sw(symbol=sw_code)
    df = df.rename(columns={df.columns[1]: "code", df.columns[2]: "name",
                            df.columns[3]: "weight"})
    df["code"] = df["code"].astype(str).str.zfill(6)
    return df


def _filtered_df(sw_code: str) -> pd.DataFrame:
    """某申万一级行业过滤后的成分(排除 ST/退市/非 A 股前缀)。"""
    df = _industry_stocks(sw_code)
    return df[df.apply(lambda r: _filter_ok(r["name"], r["code"]), axis=1)]


def _sample_from_df(df: pd.DataFrame, per: int) -> list:
    """每行业抽样:per//2 只大盘(权重最大) + per//2 只中盘(权重中位段)。"""
    if df.empty:
        return []
    s = df.sort_values("weight", ascending=False).reset_index(drop=True)
    n = len(s)
    large = list(s["code"].head(max(1, per // 2)))
    mid = []
    if per // 2 > 0 and n > per // 2 + 1:
        lo, hi = int(n * 0.40), int(n * 0.85)
        mid_slice = s.iloc[lo:hi]["code"].tolist()
        step = max(1, len(mid_slice) // (per // 2))
        mid = mid_slice[::step][: per // 2]
    return large + mid


def _sample_industry(sw_code: str, per: int) -> list:
    """每行业抽样:per//2 只大盘(权重最大) + per//2 只中盘(权重中位段)。"""
    return _sample_from_df(_filtered_df(sw_code), per)


def build_train_pool(per: int = None, max_total: int = None, use_cache: bool = True):
    """构建并缓存训练池。返回 {"codes": [...], "industry": {code: sw_code}}。"""
    per = per or _POOL_MIN_PER_INDUSTRY
    max_total = max_total or _MAX_TOTAL
    if use_cache and os.path.exists(_POOL_PATH):
        if time.time() - os.path.getmtime(_POOL_PATH) <= config.CACHE_TTL_SECONDS:
            try:
                with open(_POOL_PATH, encoding="utf-8") as f:
                    return json.load(f)
            except (OSError, ValueError):
                pass

    codes, industry = [], {}
    _load_map()
    for sw_code, ind_name in SW_CODE_TO_NAME.items():
        try:
            df = _filtered_df(sw_code)
            # 全量成分写入反查映射(任意 A 股代码即可本地命中行业,无需网络解析)
            for c in df["code"]:
                _map_cache.setdefault(c, sw_code)
            picked = _sample_from_df(df, per)
            print(f"  [池] {ind_name}({sw_code}): 成分 {len(df)} / 抽样 {len(picked)} 只")
        except Exception as e:  # noqa: BLE001
            print(f"  [池] {ind_name}({sw_code}) 失败: {e}")
            continue
        for c in picked:
            if c not in codes:
                codes.append(c)
                industry[c] = sw_code
                _map_cache[c] = sw_code

    codes = codes[:max_total]
    # 保留静态基准池(保证与历史实验可比的股票也在内),行业映射用静态表兜底
    for c in _STATIC_TRAIN_CODES:
        if c not in codes:
            codes.append(c)
            industry[c] = STATIC_STOCK_INDUSTRY.get(c)
    result = {"codes": codes, "industry": {c: industry.get(c) or STATIC_STOCK_INDUSTRY.get(c)
                                           for c in codes}}
    os.makedirs(os.path.dirname(_POOL_PATH), exist_ok=True)
    with open(_POOL_PATH, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    # 把完整行业映射写回缓存(覆盖被动态解析失败污染的 None 条目)
    for c, sw in result["industry"].items():
        if sw:
            _map_cache[c] = sw
    _save_map()
    return result


def load_train_pool() -> dict:
    """加载缓存的训练池;缺失或不足 100 只时返回空(调用方回退静态列表)。"""
    if not os.path.exists(_POOL_PATH):
        return {}
    try:
        with open(_POOL_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    if not data.get("codes") or len(data["codes"]) < 100:
        return {}
    return data


def expanded_train_codes() -> list:
    """训练代码:优先加载扩充池,否则回退静态列表。"""
    data = load_train_pool()
    return data.get("codes") or list(config.TRAIN_STOCK_CODES)


if __name__ == "__main__":
    r = build_train_pool()
    codes = r["codes"]
    inds = {}
    for c, sw in r["industry"].items():
        inds.setdefault(sw, []).append(c)
    print(f"\n[训练池] 共 {len(codes)} 只,覆盖 {len(inds)} 个申万一级行业")
    for sw, cs in sorted(inds.items(), key=lambda kv: (kv[0] is not None, kv[0] or "")):
        print(f"  {SW_CODE_TO_NAME.get(sw, sw or '未知'):<8} {len(cs)} 只: {','.join(cs[:3])}...")
