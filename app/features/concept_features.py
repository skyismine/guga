"""概念特征:同花顺概念指数 beta 与相对概念 alpha(替代申万行业指数)。

设计(与行业版一致,数据源改为同花顺概念):
- 个股 -> 所属概念:同花顺概念成分页(q.10jqka.com.cn)分页抓取,全量预取到
  data_cache/concept/concept_map.json,{股票: [概念名, ...]};
- 主概念:个股所属多个概念中取"概念指数近 20 日涨幅最强"者(跟随最强主线);
- 概念指数:akshare stock_board_concept_index_ths(同花顺),逐概念缓存
  data_cache/concept/index/{code}.pkl(仅收盘序列,TTL 同行业);
- 特征列沿用 ind_*/alpha_* 前缀(避免破坏既有特征集/模型配置,语义实为概念 beta);
- 全部为当日及历史信息,无前视;无概念/解析失败的标的特征置中性 0。
"""
import json
import os
import pickle
import re
import time

import numpy as np
import pandas as pd
import requests
from bs4 import BeautifulSoup

from app import config
from app.features.indicators import compute_features
from app.features.market_features import attach_market_features
from app.features.standardize import zscore_frame, standardize_stock_frame

_CONCEPT_DIR = os.path.join(config.DATA_DIR, "concept")
_INDEX_DIR = os.path.join(_CONCEPT_DIR, "index")
_MAP_PATH = os.path.join(_CONCEPT_DIR, "concept_map.json")
_NAME_CODE_PATH = os.path.join(_CONCEPT_DIR, "name_code.json")
_PROCESSED_PATH = os.path.join(_CONCEPT_DIR, "processed.json")
_INDEX_START = "20240101"
_A_PREFIXES = ("60", "68", "00", "30")
_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

_name_code = None
_v_code = None
_map = None
_idx_fail = set()


# ---------------------------------------------------------------- 概念列表 name->code
def _name_to_code() -> dict:
    """同花顺概念 名称->代码 映射(本地缓存 json 优先,否则走 akshare)。"""
    global _name_code
    if _name_code is not None:
        return _name_code
    if os.path.exists(_NAME_CODE_PATH):
        try:
            with open(_NAME_CODE_PATH, encoding="utf-8") as f:
                _name_code = json.load(f)
            return _name_code
        except (OSError, ValueError):
            pass
    from akshare.stock_feature.stock_board_concept_ths import _get_stock_board_concept_name_ths
    _name_code = _get_stock_board_concept_name_ths()
    try:
        os.makedirs(_CONCEPT_DIR, exist_ok=True)
        with open(_NAME_CODE_PATH, "w", encoding="utf-8") as f:
            json.dump(_name_code, f, ensure_ascii=False, indent=1)
    except OSError:
        pass
    return _name_code


def _cookie_v() -> str:
    """同花顺页面 JS 混淆 v 值(生成请求 cookie)。"""
    global _v_code
    if _v_code is None:
        from akshare.stock_feature.stock_board_concept_ths import _get_file_content_ths, py_mini_racer
        js = py_mini_racer.MiniRacer()
        js.eval(_get_file_content_ths("ths.js"))
        _v_code = js.call("v")
    return _v_code


# ---------------------------------------------------------------- 概念成分
def _fetch_concept_stocks(code: str) -> dict:
    """抓取某概念全部成分 {code: 名称}(同花顺概念详情页,分页)。"""
    out = {}
    cookie = {"Cookie": "v=" + _cookie_v()}
    headers = dict(_UA)
    headers.update(cookie)
    page = 1
    while page <= 80:
        url = "https://q.10jqka.com.cn/gn/detail/code/%s/page/%d/" % (code, page)
        try:
            r = requests.get(url, headers=headers, timeout=20)
            soup = BeautifulSoup(r.text, "lxml")
        except Exception:
            time.sleep(1.5)
            continue
        new = 0
        for tr in soup.find_all("tr"):
            a = tr.find_all("a", href=re.compile(r"stockpage\.10jqka\.com\.cn/\d{6}"))
            if len(a) >= 2 and a[0].text.strip().isdigit():
                c = a[0].text.strip()
                nm = a[1].text.strip()
                if c not in out:
                    out[c] = nm
                    new += 1
        if new == 0:
            break
        page += 1
        time.sleep(0.15)
    return out


def _load_map() -> dict:
    global _map
    if _map is None:
        try:
            with open(_MAP_PATH, encoding="utf-8") as f:
                _map = json.load(f)
        except (OSError, ValueError):
            _map = {}
    return _map


def _save_map():
    global _map
    try:
        os.makedirs(_CONCEPT_DIR, exist_ok=True)
        with open(_MAP_PATH, "w", encoding="utf-8") as f:
            json.dump(_map, f, ensure_ascii=False)
    except OSError:
        pass


def get_concepts(code: str) -> list:
    """返回股票所属同花顺概念名称列表(本地映射,未覆盖返回空)。"""
    code = str(code).zfill(6)
    if not code.startswith(_A_PREFIXES):
        return []
    return _load_map().get(code, [])


def main_concept_sw(code: str):
    """主概念:所属概念中概念指数近 20 日涨幅最强者(无前视,当日及历史)。"""
    concepts = get_concepts(code)
    if not concepts:
        return None
    best, best_ret = None, -np.inf
    for c in concepts:
        try:
            close = _get_concept_close(c)
        except Exception:
            continue
        if len(close) < 21:
            continue
        ret = close.iloc[-1] / close.iloc[-21] - 1
        if ret > best_ret:
            best, best_ret = c, ret
    return best


# ---------------------------------------------------------------- 概念指数
def _get_concept_close(name: str) -> pd.Series:
    """概念指数日收盘序列(本地缓存 TTL,失败重试)。"""
    code = _name_to_code().get(name)
    if code is None:
        raise KeyError(name)
    os.makedirs(_INDEX_DIR, exist_ok=True)
    path = os.path.join(_INDEX_DIR, f"{code}.pkl")
    if os.path.exists(path) and time.time() - os.path.getmtime(path) <= config.CACHE_TTL_SECONDS:
        try:
            with open(path, "rb") as f:
                return pickle.load(f)
        except Exception:
            pass
    import akshare as ak
    last = None
    for i in range(4):
        try:
            df = ak.stock_board_concept_index_ths(symbol=name, start_date=_INDEX_START,
                                                  end_date=time.strftime("%Y%m%d"))
            close_col = next((c for c in df.columns if "收盘" in str(c)), None)
            if close_col is None:
                raise KeyError(f"{name} 无收盘列: {list(df.columns)}")
            close = df.set_index("日期")[close_col].astype(float).sort_index()
            with open(path, "wb") as f:
                pickle.dump(close, f)
            return close
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(1.5 * (i + 1))
    raise last


# ---------------------------------------------------------------- 特征装配
def _concept_frame_for(name: str, index: pd.DatetimeIndex):
    """概念特征帧,对齐到给定日期索引(feat=滚动 z-score,raw=原始值)。"""
    close = _get_concept_close(name)
    ret1 = close.pct_change()
    raw = pd.DataFrame(index=close.index)
    raw["ind_ret_1"] = ret1
    raw["ind_ret_5"] = close.pct_change(5)
    raw["ind_ret_20"] = close.pct_change(20)
    raw["ind_ma20_gap"] = close / close.rolling(20).mean() - 1
    raw["ind_vol20"] = ret1.rolling(20).std()
    if config.STANDARDIZE_ROLLING:
        feat = zscore_frame(raw)
    else:
        feat = raw
    return feat.reindex(index).ffill(), raw.reindex(index).ffill()


_NEUTRAL_COLS = ("ind_ret_1", "ind_ret_5", "ind_ret_20", "ind_ma20_gap",
                 "ind_vol20", "alpha_1", "alpha_5", "alpha_20", "alpha_trend")


def attach_industry_features(data: pd.DataFrame) -> pd.DataFrame:
    """概念特征并入数据集(训练路径,data 含 code 列)。失败该股留空由 LightGBM 处理。"""
    if "code" not in data.columns:
        return data
    out = data.copy()
    for code in out["code"].unique():
        mc = main_concept_sw(code)
        if mc is None:
            continue
        mask = out["code"] == code
        idx = out.index[mask]
        try:
            ind, ind_raw = _concept_frame_for(mc, pd.DatetimeIndex(idx))
        except Exception as e:  # noqa: BLE001
            if code not in _idx_fail:
                _idx_fail.add(code)
                print(f"[concept] {code} 概念指数({mc})获取失败,跳过概念特征: {e}")
            continue
        for col in ind.columns:
            out.loc[mask, col] = ind[col].values
        for h in (1, 5, 20):
            out.loc[mask, f"alpha_{h}"] = out.loc[mask, f"ret_{h}"].values - ind_raw[f"ind_ret_{h}"].values
        out.loc[mask, "alpha_trend"] = out.loc[mask, "close_ma20"].values - ind_raw["ind_ma20_gap"].values
    return out


def prepare_features(df: pd.DataFrame, code: str = None) -> pd.DataFrame:
    """统一特征装配(推断/回测路径):个股技术 + 市场级 + 概念特征。"""
    code = str(code).zfill(6) if code else (getattr(df, "name", None) or None)
    features = attach_market_features(compute_features(df))
    mc = main_concept_sw(code) if code else None
    ind = ind_raw = None
    if mc:
        try:
            ind, ind_raw = _concept_frame_for(mc, features.index)
        except Exception as e:  # noqa: BLE001  概念指数失败不阻断信号生成
            if code and code not in _idx_fail:
                _idx_fail.add(code)
                print(f"[concept] {code} 概念指数({mc})获取失败,概念特征置 0: {e}")
    if ind is not None and ind_raw is not None:
        features = features.join(ind, how="left")
        for h in (1, 5, 20):
            features[f"alpha_{h}"] = features[f"ret_{h}"] - ind_raw[f"ind_ret_{h}"]
        features["alpha_trend"] = features["close_ma20"] - ind_raw["ind_ma20_gap"]
    else:
        for col in _NEUTRAL_COLS:
            features[col] = 0.0
    return standardize_stock_frame(features)


# ---------------------------------------------------------------- 全量预取
def prefetch_concepts(sleep: float = 0.2) -> None:
    """全量预取:概念成分映射(375 概念分页抓取)+ 各概念指数历史(本地缓存)。"""
    os.makedirs(_CONCEPT_DIR, exist_ok=True)
    os.makedirs(_INDEX_DIR, exist_ok=True)
    cm = _name_to_code()
    _load_map()
    processed = {}
    if os.path.exists(_PROCESSED_PATH):
        try:
            with open(_PROCESSED_PATH, encoding="utf-8") as f:
                processed = json.load(f)
        except (OSError, ValueError):
            pass
    n_concept = len(cm)
    for i, (name, code) in enumerate(cm.items(), 1):
        if name not in processed:
            try:
                stocks = _fetch_concept_stocks(code)
                for c in stocks:
                    lst = _map.setdefault(c, [])
                    if name not in lst:
                        lst.append(name)
                processed[name] = 1
                print(f"  [成分] {i}/{n_concept} {name}({code}): {len(stocks)} 只")
            except Exception as e:  # noqa: BLE001
                print(f"  [成分] {i}/{n_concept} {name} 失败: {e}")
            _save_map()
            with open(_PROCESSED_PATH, "w", encoding="utf-8") as f:
                json.dump(processed, f)
            time.sleep(sleep)
        else:
            print(f"  [成分] {i}/{n_concept} {name} 已缓存")
    for i, (name, code) in enumerate(cm.items(), 1):
        try:
            close = _get_concept_close(name)
            print(f"  [指数] {i}/{n_concept} {name}: {len(close)} 日")
        except Exception as e:  # noqa: BLE001
            print(f"  [指数] {i}/{n_concept} {name} 失败: {e}")
        time.sleep(sleep)
    print(f"\n概念映射 {len(_map)} 只股票,指数 {len(cm)} 个概念")


if __name__ == "__main__":
    prefetch_concepts()
