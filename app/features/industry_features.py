"""申万行业映射工具(训练池分层抽样用)+ 概念特征装配(re-export)。

特征层数据源已由申万行业指数切换为同花顺概念指数(见 concept_features),
本模块仅保留供训练池构建使用的申万映射工具,并把对外特征装配接口
prepare_features / attach_industry_features 转发到概念实现,调用方无感。
"""
import json
import os

from app import config

# 申万一级行业名称 <-> 代码(固定集合,训练池分层抽样用)
SW_CODE_TO_NAME = {
    "801010": "农林牧渔", "801030": "基础化工", "801040": "钢铁",
    "801050": "有色金属", "801080": "电子", "801110": "家用电器",
    "801120": "食品饮料", "801130": "纺织服饰", "801140": "轻工制造",
    "801150": "医药生物", "801160": "公用事业", "801170": "交通运输",
    "801180": "房地产", "801200": "商贸零售", "801210": "社会服务",
    "801230": "综合", "801710": "建筑材料", "801720": "建筑装饰",
    "801730": "电力设备", "801740": "国防军工", "801750": "计算机",
    "801760": "传媒", "801770": "通信", "801780": "银行",
    "801790": "非银金融", "801880": "汽车", "801890": "机械设备",
    "801950": "煤炭", "801960": "石油石化", "801970": "环保",
    "801980": "美容护理",
}
SW_NAME_TO_CODE = {v: k for k, v in SW_CODE_TO_NAME.items()}

STATIC_STOCK_INDUSTRY = {
    "600519": "801120", "601318": "801790", "600036": "801780",
    "601899": "801050", "600030": "801790", "600900": "801160",
    "601012": "801730", "600887": "801120", "600309": "801030",
    "603259": "801150", "000001": "801780", "000858": "801120",
    "000333": "801110", "000651": "801110", "002594": "801880",
    "002415": "801080", "300750": "801730", "300059": "801790",
    "300124": "801730", "002230": "801750",
}

_A_STOCK_PREFIXES = ("60", "68", "00", "30")
_MAP_PATH = os.path.join(config.DATA_DIR, "industry_code_map.json")
_map_cache = {}
_FAIL_WARNED = set()


def get_industry_sw(code: str):
    """返回股票对应的申万一级行业代码(或 None)。本地缓存 + 静态表优先。"""
    code = str(code).zfill(6)
    if not code.startswith(_A_STOCK_PREFIXES):
        return None
    if code in STATIC_STOCK_INDUSTRY:
        return STATIC_STOCK_INDUSTRY[code]
    if not _map_cache:
        _load_map()
    if code in _map_cache:
        return _map_cache[code]
    return _resolve_dynamic(code)


def _load_map():
    global _map_cache
    try:
        with open(_MAP_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    _map_cache.clear()
    _map_cache.update(data)


def _save_map():
    global _map_cache
    try:
        os.makedirs(os.path.dirname(_MAP_PATH), exist_ok=True)
        with open(_MAP_PATH, "w", encoding="utf-8") as f:
            json.dump(_map_cache, f, ensure_ascii=False, indent=2)
    except OSError:
        pass


def _resolve_dynamic(code: str):
    """动态解析个股申万行业(akshare,失败返回 None)。失败不写缓存。"""
    sw = None
    try:
        import akshare as ak
        info = ak.stock_individual_info_em(symbol=code)
        for _, row in info.iterrows():
            if row.get("item") == "行业":
                name = str(row.get("value", "")).strip()
                if name in SW_NAME_TO_CODE:
                    sw = SW_NAME_TO_CODE[name]
                else:
                    for nm, c in SW_NAME_TO_CODE.items():
                        if nm in name or name in nm:
                            sw = c
                            break
                break
    except Exception as e:  # noqa: BLE001
        if code not in _FAIL_WARNED:
            _FAIL_WARNED.add(code)
            print(f"[industry] {code} 行业解析失败({type(e).__name__}),"
                  f"已记录,后续不再重复打印")
    if sw is not None:
        _map_cache[code] = sw
        _save_map()
    return sw


# 特征装配接口:转发到概念实现(同花顺概念指数)
from app.features.concept_features import (  # noqa: E402,F401
    attach_industry_features,
    get_concepts,
    main_concept_sw,
    prefetch_concepts as prefetch_industry_indices,
    prepare_features,
)


if __name__ == "__main__":
    for c in ("600519", "300750", "000001"):
        sw = get_industry_sw(c)
        print(f"{c} -> {sw}({SW_CODE_TO_NAME.get(sw, '?')})")
