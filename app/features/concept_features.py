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
from datetime import datetime

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
_FLOW_PATH = os.path.join(_CONCEPT_DIR, "flow.json")
_PROCESSED_PATH = os.path.join(_CONCEPT_DIR, "processed.json")
_INDEX_START = "20240101"
_A_PREFIXES = ("60", "68", "00", "30")
_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

_name_code = None
_v_code = None
_map = None
_idx_fail = set()


def _get_ths_js() -> str:
    """读取 akshare 内置的同花顺 JS 混淆脚本内容(用于生成防爬 v 值)。"""
    from akshare.stock_feature.stock_board_concept_ths import _get_file_content_ths
    return _get_file_content_ths("ths.js")


# ---------------------------------------------------------------- 概念列表 name->code
def _flow_concepts() -> dict:
    """从同花顺概念资金流排行页提取 板块名->代码 映射。

    概念名录(_name_to_code)覆盖 375 个板块,但资金流排行页还含名录缺失的
    板块(如 CRO概念/308734、ChatGPT概念 等),其链接同样指向概念详情页。
    两源合并可保证资金流板块与成分映射/指数体系一致,消除"有板块无成分"缺口。
    """
    from py_mini_racer import MiniRacer as _MR
    js_code = _MR()
    js_content = _get_ths_js()
    js_code.eval(js_content)
    v_code = js_code.call("v")
    headers = dict(_UA)
    headers.update({
        "hexin-v": v_code,
        "Host": "data.10jqka.com.cn",
        "Referer": "http://data.10jqka.com.cn/funds/gnzjl/",
        "X-Requested-With": "XMLHttpRequest",
    })
    out = {}
    try:
        r = requests.get(
            "https://data.10jqka.com.cn/funds/gnzjl/field/tradezdf/order/desc/ajax/1/free/1/",
            headers=headers, timeout=20)
        soup = BeautifulSoup(r.text, "lxml")
        raw_page = soup.find(name="span", attrs={"class": "page_info"})
        page_num = 1
        if raw_page and "/" in raw_page.text:
            page_num = int(raw_page.text.split("/")[1])
        for page in range(1, page_num + 1):
            url = ("https://data.10jqka.com.cn/funds/gnzjl/"
                   "field/tradezdf/order/desc/page/%d/ajax/1/free/1/" % page)
            try:
                rr = requests.get(url, headers=headers, timeout=20)
                s2 = BeautifulSoup(rr.text, "lxml")
            except Exception:  # noqa: BLE001
                time.sleep(1.5)
                continue
            for a in s2.find_all("a", href=re.compile(r"gn/detail/code/\d+/")):
                m = re.search(r"gn/detail/code/(\d+)/", a.get("href", ""))
                nm = a.get_text(strip=True)
                if m and nm:
                    out[nm] = m.group(1)
            time.sleep(0.1)
    except Exception:  # noqa: BLE001
        pass
    return out


def _name_to_code() -> dict:
    """同花顺概念 名称->代码 映射(本地缓存 json 优先,否则走 akshare)。

    在 akshare 概念名录基础上,合并概念资金流排行板块(补全名录缺失板块),
    保证资金流口径的板块名(如 CRO概念)与成分/指数体系一致。
    """
    global _name_code
    if _name_code is not None:
        return _name_code
    merged = {}
    if os.path.exists(_NAME_CODE_PATH):
        try:
            with open(_NAME_CODE_PATH, encoding="utf-8") as f:
                merged = json.load(f)
        except (OSError, ValueError):
            merged = {}
    if not merged:
        from akshare.stock_feature.stock_board_concept_ths import _get_stock_board_concept_name_ths
        merged = _get_stock_board_concept_name_ths()
    # 合并资金流板块:本地 flow.json 优先,缺失时抓取后持久化,避免每次启动请求
    flow = {}
    if os.path.exists(_FLOW_PATH):
        try:
            with open(_FLOW_PATH, encoding="utf-8") as f:
                flow = json.load(f)
        except (OSError, ValueError):
            flow = {}
    if not flow:
        flow = _flow_concepts()
        try:
            os.makedirs(_CONCEPT_DIR, exist_ok=True)
            with open(_FLOW_PATH, "w", encoding="utf-8") as f:
                json.dump(flow, f, ensure_ascii=False, indent=1)
        except OSError:
            pass
    merged.update(flow)
    try:
        os.makedirs(_CONCEPT_DIR, exist_ok=True)
        with open(_NAME_CODE_PATH, "w", encoding="utf-8") as f:
            json.dump(merged, f, ensure_ascii=False, indent=1)
    except OSError:
        pass
    _name_code = merged
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
def _fetch_index_close_by_code(code: str, name: str, start_date: str) -> pd.Series:
    """按板块代码直连抓取同花顺概念指数收盘序列。

    akshare 的 stock_board_concept_index_ths 内部使用概念名录映射,
    名录缺失的板块(如 CRO概念/308734)会 KeyError;这里直接用代码取
    clid 再拉取 bk 日线,兼容名录外板块。
    """
    from py_mini_racer import MiniRacer as _MR
    js_code = _MR()
    js_code.eval(_get_ths_js())
    v_code = js_code.call("v")
    headers = dict(_UA)
    headers.update({"Cookie": f"v={v_code}"})
    page_url = f"https://q.10jqka.com.cn/gn/detail/code/{code}"
    r = requests.get(page_url, headers=headers, timeout=20)
    soup = BeautifulSoup(r.text, "lxml")
    clid = soup.find(name="input", attrs={"id": "clid"})
    if clid is None:
        raise KeyError(f"{name} 无 clid")
    inner_code = clid["value"]
    cur_year = datetime.now().year
    begin_year = int(start_date[:4])
    hd = dict(headers)
    hd.update({"Referer": "http://q.10jqka.com.cn", "Host": "d.10jqka.com.cn"})
    frames = []
    for year in range(begin_year, cur_year + 1):
        url = f"https://d.10jqka.com.cn/v4/line/bk_{inner_code}/01/{year}.js"
        try:
            rr = requests.get(url, headers=hd, timeout=20)
            text = rr.text
        except Exception:  # noqa: BLE001
            continue
        try:
            from akshare.utils import demjson
            obj = demjson.decode(text[text.find("{") : -1])
            rows = obj["data"].split(";")
        except Exception:  # noqa: BLE001
            continue
        for row in rows:
            parts = row.split(",")
            if len(parts) < 6:
                continue
            try:
                frames.append([parts[0], float(parts[2])])  # 日期,收盘
            except (IndexError, ValueError):
                continue
    if not frames:
        raise KeyError(f"{name} 无指数数据")
    df = pd.DataFrame(frames, columns=["date", "close"])
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna().drop_duplicates("date").sort_values("date")
    df = df[(df["date"] >= pd.to_datetime(start_date))]
    return pd.Series(df["close"].values, index=df["date"].values, name="close").astype(float)


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
    last = None
    for i in range(4):
        try:
            close = _fetch_index_close_by_code(code, name, _INDEX_START)
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
    """全量预取:概念成分映射(概念名录+资金流板块分页抓取)+ 各概念指数历史(本地缓存)。"""
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
