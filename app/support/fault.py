# -*- coding: utf-8 -*-
"""统一异常处理框架 + 熔断器 + 降级助手(6.1/6.2)。

- 结构化日志: JSON 格式,含 timestamp/module/level/message/trace_id/context/stack。
  按模块 + 日期写入 data_cache/logs/; 保留30天(由 daily_cleanup 清理)。
- 异常分级: CRITICAL(致命) / ERROR(严重,模块降级) / WARNING(一般,用默认值) /
  INFO(关键节点) / DEBUG(调试)。
- 熔断器: 单模块连续失败 N 次(默认5)触发熔断, 暂停 OPEN 分钟(默认10), 半开试探;
  全局错误率 >30%(近30次) 触发全局熔断 → 建议观望。
- 降级助手: degrade_cached(缓存/最近有效值+"数据延迟") / 模型→技术面替代("模型不可用") /
  主线→昨日("主线未更新") / 执行参数→保守("计算异常")。
"""
import datetime as dt
import json
import os
import threading
import time
import traceback

from app import config

_LOG_DIR = os.path.join(config.DATA_DIR, "logs")
os.makedirs(_LOG_DIR, exist_ok=True)

_LOCK = threading.Lock()
# 熔断状态: module -> {fail_count, open_until, total_ok, total_fail, last_error}
_CB: dict = {}
# 全局错误率滚动统计
_GLOBAL = {"ok": 0, "fail": 0}
# 全局熔断状态
_GLOBAL_TRIP = {"open": False, "until": 0.0}
_ALERT_HOOK = None      # 可注入告警回调(webhook/邮件), 默认仅日志

_CB_FAIL_THRESHOLD = 5      # 连续失败次数(可由 settings.system 覆盖)
_CB_OPEN_MINUTES = 10       # 熔断时长(分钟)
_GLOBAL_ERROR_RATE = 0.30   # 全局错误率阈值
_GLOBAL_WINDOW = 30         # 滚动窗口


def _sys_cfg() -> dict:
    """熔断/日志参数(settings.system, 缺省回退内置默认, 避免魔法数字散落)。"""
    try:
        from app.support import settings as _st
        return (_st.load().get("system") or {})
    except Exception:  # noqa: BLE001
        return {}


def _fail_threshold() -> int:
    return int(_sys_cfg().get("cb_fail_threshold", _CB_FAIL_THRESHOLD) or _CB_FAIL_THRESHOLD)


def _open_minutes() -> int:
    return int(_sys_cfg().get("cb_open_minutes", _CB_OPEN_MINUTES) or _CB_OPEN_MINUTES)


def _error_rate_thr() -> float:
    return float(_sys_cfg().get("global_error_rate", _GLOBAL_ERROR_RATE) or _GLOBAL_ERROR_RATE)


def _global_window() -> int:
    return int(_sys_cfg().get("global_window", _GLOBAL_WINDOW) or _GLOBAL_WINDOW)


def _ts() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _log_file(module: str) -> str:
    safe = (module or "system").replace("/", "_").replace("\\", "_")
    return os.path.join(_LOG_DIR, f"{dt.date.today().isoformat()}_{safe}.jsonl")


def log(module: str, level: str, message: str, trace_id: str = None,
        context: dict = None, exc: BaseException = None) -> None:
    """写一条结构化 JSON 日志(线程安全, 追加写)。"""
    entry = {
        "ts": _ts(),
        "module": module,
        "level": level,
        "message": str(message),
        "trace_id": trace_id,
        "context": context or {},
    }
    if exc is not None:
        entry["error_type"] = type(exc).__name__
        entry["error"] = str(exc)
        entry["stack"] = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))[-4000:]
    try:
        with _LOCK:
            with open(_log_file(module), "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:  # noqa: BLE001
        pass
    if level in ("ERROR", "CRITICAL") and _ALERT_HOOK:
        try:
            _ALERT_HOOK(entry)
        except Exception:  # noqa: BLE001
            pass


def info(module, msg, trace_id=None, context=None): return log(module, "INFO", msg, trace_id, context)
def warning(module, msg, trace_id=None, context=None, exc=None): return log(module, "WARNING", msg, trace_id, context, exc)
def error(module, msg, trace_id=None, context=None, exc=None): return log(module, "ERROR", msg, trace_id, context, exc)
def critical(module, msg, trace_id=None, context=None, exc=None): return log(module, "CRITICAL", msg, trace_id, context, exc)


def set_alert_hook(fn) -> None:
    """注入告警回调(入参为日志 entry dict), 如钉钉/企业微信 webhook。"""
    global _ALERT_HOOK
    _ALERT_HOOK = fn


def cleanup_logs(keep_days: int = None) -> int:
    """清理超过保留天数的日志文件(默认 settings.system.log_keep_days)。"""
    if keep_days is None:
        keep_days = int(_sys_cfg().get("log_keep_days", 30) or 30)
    cutoff = time.time() - keep_days * 86400
    n = 0
    for f in os.listdir(_LOG_DIR):
        p = os.path.join(_LOG_DIR, f)
        try:
            if os.path.getmtime(p) < cutoff:
                os.remove(p)
                n += 1
        except OSError:  # noqa: BLE001
            pass
    return n


# ---------------------------------------------------------------- 熔断器
def _cb_state(module: str) -> dict:
    return _CB.setdefault(module, {"fail": 0, "open_until": 0.0, "last_error": "",
                                   "opens": 0, "last_open": None})


def is_open(module: str) -> bool:
    """模块是否处于熔断打开状态(期间直接走降级, 不发起外部调用)。"""
    st = _cb_state(module)
    if st["open_until"] > time.time():
        return True
    if st["open_until"] and st["open_until"] <= time.time():
        # 熔断到期: 半开试探一次
        st["open_until"] = 0.0
    return False


def record_success(module: str) -> None:
    st = _cb_state(module)
    st["fail"] = 0
    _GLOBAL["ok"] += 1
    _trim_global()


def record_failure(module: str, err: str) -> bool:
    """记录一次失败; 连续失败超阈值 → 熔断。返回是否刚触发熔断。"""
    st = _cb_state(module)
    st["fail"] += 1
    st["last_error"] = str(err)
    _GLOBAL["fail"] += 1
    _trim_global()
    thr = _fail_threshold()
    mins = _open_minutes()
    if st["fail"] >= thr and st["open_until"] <= time.time():
        st["open_until"] = time.time() + mins * 60
        st["opens"] += 1
        st["last_open"] = _ts()
        log("fault", "ERROR",
            f"模块 {module} 连续失败 {st['fail']} 次,触发熔断 {mins} 分钟",
            context={"last_error": st["last_error"], "opens": st["opens"]})
        return True
    return False


def _trim_global() -> None:
    """限制全局窗口为最近 _GLOBAL_WINDOW 次(settings.system.global_window)。"""
    total = _GLOBAL["ok"] + _GLOBAL["fail"]
    win = _global_window()
    if total > win:
        scale = win / total
        _GLOBAL["ok"] = int(_GLOBAL["ok"] * scale)
        _GLOBAL["fail"] = int(_GLOBAL["fail"] * scale)


def global_error_rate() -> float:
    total = _GLOBAL["ok"] + _GLOBAL["fail"]
    return (_GLOBAL["fail"] / total) if total else 0.0


def global_health() -> dict:
    """全局健康: 错误率 + 全局熔断状态 + 各模块熔断状态。"""
    total = _GLOBAL["ok"] + _GLOBAL["fail"]
    rate = global_error_rate()
    thr = _error_rate_thr()
    mins = _open_minutes()
    g_open = bool(_GLOBAL_TRIP["open"] and _GLOBAL_TRIP["until"] > time.time())
    if not g_open and total >= 5 and rate > thr:
        _GLOBAL_TRIP.update(open=True, until=time.time() + mins * 60)
        log("fault", "CRITICAL", f"全局错误率 {rate:.0%} 超阈值 {thr:.0%},全局熔断",
            context={"window": total})
        g_open = True
    if g_open and _GLOBAL_TRIP["until"] <= time.time():
        _GLOBAL_TRIP.update(open=False, until=0.0)
        g_open = False
    return {"global_open": g_open, "error_rate": round(rate, 4),
            "window": total, "modules": {
                m: {"open": st["open_until"] > time.time(), "fail": st["fail"],
                    "opens": st["opens"], "last_error": st["last_error"]}
                for m, st in _CB.items()},
            "threshold": thr, "open_minutes": mins}


def guarded(module: str, fn, fallback=None, trace_id: str = None, context: dict = None):
    """带熔断的守卫执行: 熔断中/异常 → 走 fallback(降级), 并记录日志。"""
    if is_open(module):
        warning("fault", f"模块 {module} 熔断中,直接降级", trace_id=trace_id, context=context)
        return fallback
    try:
        result = fn()
        record_success(module)
        return result
    except Exception as e:  # noqa: BLE001
        record_failure(module, str(e))
        error(module, f"执行失败,降级处理", trace_id=trace_id,
              context={**(context or {}), "fallback_used": fallback is not None}, exc=e)
        return fallback


def new_trace_id() -> str:
    """生成决策链路 trace_id。"""
    return f"{dt.datetime.now().strftime('%Y%m%d%H%M%S')}{os.getpid()}{threading.get_ident()}"


# ---------------------------------------------------------------- 降级助手(标注)
def degrade_tag(reason: str, base: dict = None) -> dict:
    """给结果附加降级标注字段。"""
    out = dict(base or {})
    out["degraded"] = True
    out["degrade_reason"] = reason
    return out


def is_degraded(obj: dict) -> bool:
    return bool((obj or {}).get("degraded"))


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    print("健康:", global_health())
    for i in range(6):
        record_failure("测试模块", f"err{i}")
    print("测试模块熔断:", is_open("测试模块"))
    print("健康:", global_health())
