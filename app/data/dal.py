"""统一数据访问层(Data Access Layer):分级缓存 + 数据质量评分 + 缺失策略 + 预热。

设计目标(对齐「数据层优化」任务):
1.1 分级缓存: 内存(TTL 60s) → 本地文件(按交易日/类型) → 远程数据源;
    缓存 key 标准化 `{data_type}:{code}:{date}:{period}`;
    提供 warmup_basic() 开盘前预加载当日基础数据。
1.2 数据质量: 每个数据源返回附 data_quality(0-1)/data_source/data_note;
    缺失分级处理(关键数据用最近有效值并标注、非关键按策略填充);
    连续缺失超过阈值触发告警;所有缺失处理写入日志,不得静默跳过。
1.3 一致性: 收盘后校准标记(calibrated)、前复权口径统一、成交额(amount)与成交量(volume)区分。

约定:
- 内存缓存存的是"值本身";文件缓存存 pickle。两者键共用 cache_key。
- fetch() 是统一取数入口: 内存 → 文件(新鲜) → 远程;远程失败回退过期文件时标注降级。
- 所有 collect_* 返回的可 dict 结果,调用方用 with_quality() 注入质量字段;
  列表结果的质量由 collect_review 汇总到 `_quality` 键。
"""
import datetime as dt
import logging
import os
import pickle
import threading
import time
from typing import Callable, Optional

from app import config

logger = logging.getLogger("guga.dal")
if not logger.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("[dal] %(levelname)s %(message)s"))
    logger.addHandler(_h)
    logger.setLevel(logging.INFO)
    # 同时落盘,满足"缺失处理必须记录日志"
    try:
        _f = logging.FileHandler(os.path.join(config.DATA_DIR, "dal.log"), encoding="utf-8")
        _f.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(_f)
    except OSError:  # noqa: BLE001
        pass

_DAL_DIR = os.path.join(config.DATA_DIR, "dal")
os.makedirs(_DAL_DIR, exist_ok=True)

# 内存缓存: key -> (expire_ts, value)
_MEM_DEFAULT_TTL = 60          # 秒(任务要求 60s)
_MEM: dict = {}
_MEM_LOCK = threading.Lock()

# 连续缺失统计: data_type -> 连续失败次数
_MISSING_COUNT: dict = {}
_MISSING_THRESHOLD = 5         # 连续缺失超过该值 → 告警/建议暂停依赖模块

# 关键数据类型(缺失时用最近有效值并标注,非关键填充中性值)
CRITICAL_TYPES = ("index", "market_daily", "activity", "limit_up", "amount")

# 复权口径统一: 所有价格历史默认前复权(qfq), 调用方显式指定
ADJUST_UNIFIED = "qfq"
_ADJUST_NAME = {"qfq": "前复权", "hfq": "后复权", "": "不复权", "forward": "前复权(fuyao)"}


def today_str() -> str:
    return dt.date.today().isoformat()


def trade_date_str() -> str:
    """最近交易日字符串(供缓存 key 使用;失败回退今日)。"""
    try:
        from app.review.data import review_date
        return str(review_date())
    except Exception:  # noqa: BLE001
        return today_str()


# ---------------------------------------------------------------- 缓存 key 标准化
def cache_key(data_type: str, code: str = None, date: str = None, period: str = None) -> str:
    """标准化缓存 key: `{data_type}:{code}:{date}:{period}`(缺省段省略)。"""
    parts = [str(data_type)]
    if code:
        parts.append(str(code).zfill(6))
    if date:
        parts.append(str(date))
    if period:
        parts.append(str(period))
    return ":".join(parts)


def _file_path(key: str) -> str:
    safe = key.replace(":", "_").replace("/", "_").replace("\\", "_")
    return os.path.join(_DAL_DIR, f"{safe}.pkl")# ---------------------------------------------------------------- 内存层
def mem_get(key: str):
    with _MEM_LOCK:
        item = _MEM.get(key)
    if item is None:
        return None
    expire_ts, value = item
    if time.time() > expire_ts:
        with _MEM_LOCK:
            _MEM.pop(key, None)
        return None
    return value


def mem_set(key: str, value, ttl: int = None) -> None:
    ttl = _MEM_DEFAULT_TTL if ttl is None else ttl
    with _MEM_LOCK:
        _MEM[key] = (time.time() + ttl, value)


def mem_clear(prefix: str = None) -> None:
    """清空内存缓存(可指定前缀);开盘/收盘切换时用于强制刷新。"""
    with _MEM_LOCK:
        if prefix is None:
            _MEM.clear()
        else:
            for k in [k for k in _MEM if k.startswith(prefix)]:
                _MEM.pop(k, None)


# ---------------------------------------------------------------- 文件层
def file_get(key: str, ttl: int = None, stale_ok: bool = False):
    """读文件缓存。返回 (value, fresh):
    - fresh=True: 缓存新鲜(TTL 内);
    - fresh=False: 过期但返回(仅 stale_ok=True 时用于降级回退),否则 None。
    """
    p = _file_path(key)
    if not os.path.exists(p):
        return None, False
    try:
        with open(p, "rb") as f:
            value = pickle.load(f)
    except Exception as e:  # noqa: BLE001
        logger.warning("文件缓存损坏 %s: %s", key, e)
        return None, False
    if ttl is None:
        return value, True
    age = time.time() - os.path.getmtime(p)
    if age <= ttl:
        return value, True
    return (value, False) if stale_ok else (None, False)


def file_set(key: str, value) -> None:
    try:
        os.makedirs(_DAL_DIR, exist_ok=True)
        with open(_file_path(key), "wb") as f:
            pickle.dump(value, f)
    except OSError as e:  # noqa: BLE001
        logger.warning("文件缓存写入失败 %s: %s", key, e)


# ---------------------------------------------------------------- 跨平台文件锁
def file_lock(path: str):
    """对文件加独占锁(用于并发写 JSON 存档),返回释放函数;平台无锁支持时返回 None。

    优先 fcntl(Unix/Linux 服务器),回退 msvcrt(Windows),避免并发写冲突损坏存档。
    """
    f = None
    try:  # Unix
        import fcntl  # noqa: PLC0415
        f = open(path, "a+")
        fcntl.flock(f, fcntl.LOCK_EX)
        def _unlock():
            try:
                fcntl.flock(f, fcntl.LOCK_UN)
            finally:
                f.close()
        return _unlock
    except (ImportError, OSError):  # noqa: BLE001
        if f is not None:
            try:
                f.close()
            except OSError:  # noqa: BLE001
                pass
    try:  # Windows
        import msvcrt  # noqa: PLC0415
        f = open(path, "a+")
        if f.tell() == 0:
            f.write("\x00")
            f.flush()
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)
        def _unlock():
            try:
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            finally:
                f.close()
        return _unlock
    except (ImportError, OSError):  # noqa: BLE001
        if f is not None:
            try:
                f.close()
            except OSError:  # noqa: BLE001
                pass
    logger.warning("平台无文件锁支持,并发写存档存在冲突风险: %s", path)
    return None


def locked_write(path: str, text: str, encoding: str = "utf-8") -> None:
    """加独占文件锁后原子写文本(读写同一句柄)。

    注意: Windows 下 msvcrt 字节锁会阻断「另开句柄 truncate 写」,因此必须
    在锁定句柄上直接 seek+truncate+write(否则 PermissionError 被上层吞掉,
    留下 0 字节文件)。无锁支持平台直接写。
    """
    release = fh = None
    try:  # Unix
        import fcntl  # noqa: PLC0415
        fh = open(path, "a+", encoding=encoding)
        fcntl.flock(fh, fcntl.LOCK_EX)
        def _rel():
            try:
                fcntl.flock(fh, fcntl.LOCK_UN)
            finally:
                fh.close()
        release = _rel
    except (ImportError, OSError):  # noqa: BLE001
        if fh is not None:
            try:
                fh.close()
            except OSError:  # noqa: BLE001
                pass
            fh = None
    if fh is None:
        try:  # Windows
            import msvcrt  # noqa: PLC0415
            fh = open(path, "a+", encoding=encoding)
            if fh.tell() == 0:
                fh.write("\x00")
                fh.flush()
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)
            def _rel():
                try:
                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                finally:
                    fh.close()
            release = _rel
        except (ImportError, OSError):  # noqa: BLE001
            if fh is not None:
                try:
                    fh.close()
                except OSError:  # noqa: BLE001
                    pass
                fh = None
    if fh is not None:
        try:
            fh.seek(0)
            fh.truncate(0)
            fh.write(text)
            fh.flush()
        finally:
            if release:
                release()
    else:
        with open(path, "w", encoding=encoding) as f:
            f.write(text)


# ---------------------------------------------------------------- 缺失记录/告警
def record_missing(data_type: str, ok: bool, reason: str = "", extra: str = "") -> bool:
    """记录一次取数成败;连续缺失计数并告警。返回是否已触发告警。"""
    if ok:
        _MISSING_COUNT[data_type] = 0
        return False
    n = _MISSING_COUNT.get(data_type, 0) + 1
    _MISSING_COUNT[data_type] = n
    if n >= _MISSING_THRESHOLD:
        logger.error("连续缺失告警 %s: 已连续 %d 次缺失(阈值 %d),建议暂停依赖该数据的决策模块 | %s",
                     data_type, n, _MISSING_THRESHOLD, reason)
        return True
    logger.warning("数据缺失 %s: %s %s", data_type, reason, extra)
    return False


def missing_stats() -> dict:
    """当前连续缺失计数快照(供监控/页面展示)。"""
    return dict(_MISSING_COUNT)


def is_alarmed(data_type: str) -> bool:
    return _MISSING_COUNT.get(data_type, 0) >= _MISSING_THRESHOLD


# ---------------------------------------------------------------- 数据质量
# 来源可靠性基准(0~1): 主源=1.0, 回退/缓存/过期缓存逐级降低
_SRC_BASE = {
    "fuyao": 1.0, "sina": 1.0, "sina_hq": 1.0, "ths": 0.95, "sina_daily": 0.90,
    "market": 1.0, "legu": 0.80, "eastmoney": 0.85, "em": 0.85, "tencent": 0.85,
    "cache": 0.70, "stale_cache": 0.15, "fallback": 0.40, "-": 0.0, "": 0.0,
}


def quality_for(src: str, age_seconds: float = 0.0, missing_ratio: float = 0.0) -> float:
    """多维度质量评分(0~1, 对齐「数据质量细粒度评分」最小方案)。

    - 来源可靠性(0.2): 主源(fuyao/新浪)=1.0, 东财/腾讯=0.85, 缓存=0.70, 过期缓存=0.40;
    - 新鲜度(0.3): ≤30s 满分, 每 5 分钟扣 0.05, 最低 0;
    - 完整性(0.4): 按缺省填充比例(1 - missing_ratio);
    - 一致性(0.1): 回退源给 0.05(存在偏差风险), 其余 0.1。
    """
    base = _SRC_BASE.get(src or "", 0.60)
    src_w = base * 0.2
    fresh_w = max(0.0, 0.3 - max(0.0, float(age_seconds) - 30) / 300.0 * 0.05)
    comp_w = 0.4 * max(0.0, min(1.0, 1 - float(missing_ratio or 0.0)))
    cons_w = 0.05 if (src or "") in ("stale_cache", "fallback", "legu") else 0.1
    return round(max(0.0, min(1.0, src_w + fresh_w + comp_w + cons_w)), 3)


def with_quality(obj, score: float = None, src: str = "", note: str = "") -> dict:
    """为 dict 结果注入质量字段(新副本,不污染原对象)。score∈[0,1]。
    用于"每个数据源返回数据时附带 data_quality 字段"。score 缺省按 quality_for(src) 计算。"""
    if score is None:
        score = quality_for(src)
    out = dict(obj) if isinstance(obj, dict) else {"value": obj}
    out["data_quality"] = round(max(0.0, min(1.0, score)), 3)
    out["data_source"] = src
    out["data_note"] = note
    record_quality(src, out["data_quality"])
    return out


def attach_quality(obj: dict, score: float = None, src: str = "", note: str = "") -> dict:
    """原地注入质量字段(调用方持有该 dict 时用)。score 缺省按 quality_for(src) 计算。"""
    if score is None:
        score = quality_for(src)
    obj["data_quality"] = round(max(0.0, min(1.0, score)), 3)
    obj["data_source"] = src
    obj["data_note"] = note
    record_quality(src, obj["data_quality"])
    return obj


def record_quality(src: str, score: float) -> None:
    """把一次数据质量分登记到 fault 聚合(供健康面板按来源统计均分与趋势)。"""
    if score is None or src is None:
        return
    try:
        from app.support import fault as _flt
        _flt.record_quality(str(src), float(score))
    except Exception:  # noqa: BLE001
        pass


def quality_summary(collectors: dict) -> dict:
    """汇总各模块质量到 _quality:{type:{score,source,note}}。
    collectors: {type: (quality_value, source, note)} 或 {type: dict(含 data_quality...) }。"""
    out = {}
    for t, v in collectors.items():
        if isinstance(v, dict) and v.get("data_quality") is not None:
            out[t] = {"score": v["data_quality"], "source": v.get("data_source", ""),
                      "note": v.get("data_note", "")}
        elif isinstance(v, tuple) and len(v) == 3:
            out[t] = {"score": v[0], "source": v[1], "note": v[2]}
    return out


# ---------------------------------------------------------------- 统一取数入口
def fetch(data_type: str, fetcher: Callable, *, code: str = None, date: str = None,
          period: str = None, mem_ttl: int = None, file_ttl: int = None,
          stale_ok: bool = True, critical: bool = False, source: str = "", note: str = ""):
    """统一取数:内存 → 文件(新鲜) → 远程。

    - 命中内存/新鲜文件直接返回(不记缺失);
    - 远程失败:critical=True 时优先回退过期文件并标注;stale_ok 控制是否允许过期回退;
    - 全部失败:critical 时回退 on_error(缺省返回 None),否则抛错。
    """
    key = cache_key(data_type, code, date, period)

    hit = mem_get(key)
    if hit is not None:
        return hit

    if file_ttl is not None:
        val, fresh = file_get(key, ttl=file_ttl)
        if val is not None and fresh:
            mem_set(key, val, mem_ttl)
            return val
        if val is not None and stale_ok and critical:
            # 关键数据远程失败时的"最近有效值"降级(先探测远程,下方统一处理)
            pass

    try:
        val = fetcher()
        if val is None:
            raise RuntimeError(f"远程源返回空: {key}")
        file_set(key, val)
        mem_set(key, val, mem_ttl)
        record_missing(data_type, True)
        return val
    except Exception as e:  # noqa: BLE001
        record_missing(data_type, False, f"{key} 抓取失败", str(e))
        if critical:
            val, _ = file_get(key, ttl=None)   # 任意年龄的最近有效值
            if val is not None:
                logger.warning("关键数据 %s 降级: 使用最近有效值(过期缓存), 建议校准 | %s", key, e)
                return val
            if source:
                logger.warning("关键数据 %s 无缓存可回退 | %s", key, e)
            return None
        raise


# ---------------------------------------------------------------- 预热
def warmup_basic(date=None) -> dict:
    """开盘前预加载当日基础数据(指数/市场日度/板块资金/涨跌家数),幂等,单模块失败不中断。"""
    from app.review.data import (collect_activity, collect_indices,
                                 collect_market_daily, collect_sector_flow,
                                 review_date)
    date = date or review_date()
    results = {}
    for name, fn in (
        ("indices", lambda: collect_indices(date)),
        ("market_daily", lambda: collect_market_daily(20)),
        ("sector_flow", lambda: collect_sector_flow()),
        ("activity", lambda: collect_activity()),
    ):
        try:
            v = fn()
            results[name] = {"ok": True, "size": len(v) if hasattr(v, "__len__") else "n/a"}
            logger.info("预热 %s 完成", name)
        except Exception as e:  # noqa: BLE001
            results[name] = {"ok": False, "error": str(e)}
            logger.warning("预热 %s 失败: %s", name, e)
    return {"date": str(date), "results": results}


# ---------------------------------------------------------------- 一致性/校准
def mark_calibrated(row: dict, src: str = "fuyao 收盘快照") -> dict:
    """标记该行已用权威收盘口径校准(1.3 实时/历史一致性)。"""
    row["calibrated"] = True
    row["calibrate_src"] = src
    return row


def adjust_name(adjust: str) -> str:
    return _ADJUST_NAME.get(adjust or "", adjust or "")


# 导出常用常量,便于调用方统一口径
AMOUNT_FIELD = "amount"     # 成交额(元)
VOLUME_FIELD = "volume"     # 成交量(股/手)


# ---------------------------------------------------------------- 收盘校准
def calibrate_market(date=None) -> dict:
    """收盘后校准: 强制刷新最新一日指数/两市成交额到权威收盘口径(1.3 一致性)。

    复用 collect_indices(优先 fuyao 官方快照)与 collect_market_daily(末行 fuyao 覆盖+calibrated 标记)。
    """
    from app.review.data import collect_indices, collect_market_daily, review_date
    date = date or review_date()
    out = {"date": str(date), "ok": True, "notes": []}
    try:
        idx = collect_indices(date)
        out["indices"] = [{"name": i["name"], "close": i.get("close"),
                           "source": i.get("data_source")} for i in idx]
    except Exception as e:  # noqa: BLE001
        out["ok"] = False
        out["notes"].append(f"指数校准失败: {e}")
    try:
        md = collect_market_daily(20)
        last = md[-1] if md else {}
        out["latest"] = {"date": last.get("date"), "amount_yi": last.get("amount_yi"),
                         "close": last.get("close"), "calibrated": last.get("calibrated", False),
                         "calibrate_src": last.get("calibrate_src")}
    except Exception as e:  # noqa: BLE001
        out["ok"] = False
        out["notes"].append(f"市场日度校准失败: {e}")
    if out.get("latest") and out["latest"].get("calibrated"):
        out["notes"].append("最新一日已用 fuyao 收盘快照校准")
    return out


if __name__ == "__main__":
    import json as _json
    _res = warmup_basic()
    print(_json.dumps(_res, ensure_ascii=False, indent=1, default=str))
