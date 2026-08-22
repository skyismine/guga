"""复盘历史归档 + 核心指标结构化落库(P3)。

- 每日复盘生成后,将当日决策快照(主线稳定器输出 / 三层榜单 / 推荐标的及其预测方向)
  与核心指标(market_grade / 恐贪 / 宽度 / 情绪温度等)追加为一行 JSONL 存档。
- 后续复盘通过 `load_day` 读取前一交易日存档,实现「主线演进追踪」与「当日决策效果验证」;
  `query_metrics` 支持长周期统计查询,服务策略迭代与效果验证。
- 文件损坏/缺失一律静默降级为空,不阻塞报告生成。
"""
import datetime as dt
import json
import os
import threading

from app import config

_ARCHIVE_PATH = os.path.join(config.DATA_DIR, "review_archive.jsonl")
_LOCK = threading.Lock()


def _rows() -> list:
    if not os.path.exists(_ARCHIVE_PATH):
        return []
    try:
        with open(_ARCHIVE_PATH, encoding="utf-8") as f:
            return [json.loads(l) for l in f if l.strip()]
    except (OSError, ValueError):
        return []


def _write(rows: list) -> None:
    os.makedirs(os.path.dirname(_ARCHIVE_PATH), exist_ok=True)
    with open(_ARCHIVE_PATH, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def save_day(rec: dict) -> None:
    """保存(或覆盖)某日存档记录,按日期升序落盘。"""
    date = rec.get("date") or str(dt.date.today())
    rec = {"date": date, **rec}
    with _LOCK:
        rows = [r for r in _rows() if r.get("date") != date]
        rows.append(rec)
        rows.sort(key=lambda r: str(r.get("date", "")))
        _write(rows)


def load_day(date) -> dict:
    """读取指定日期存档;不存在返回空 dict。"""
    date = str(date)
    with _LOCK:
        for r in _rows():
            if r.get("date") == date:
                return r
    return {}


def load_days(n: int = 4) -> list:
    """返回最近 n 个日期的存档(按日期升序)。"""
    with _LOCK:
        rows = sorted(_rows(), key=lambda r: str(r.get("date", "")))
    return rows[-n:]


def prev_day(date) -> dict:
    """读取给定日期之前最近一个交易日的存档(供决策验证)。"""
    date = str(date)
    with _LOCK:
        rows = sorted((r for r in _rows() if str(r.get("date", "")) < date),
                      key=lambda r: str(r.get("date", "")))
    return rows[-1] if rows else {}


def query_metrics(n: int = None, key: str = None):
    """长周期核心指标查询:n=None 全量;key 过滤单指标序列。"""
    rows = _rows()
    if n:
        rows = rows[-n:]
    if key is None:
        return [r.get("metrics") or {} for r in rows if r.get("metrics")]
    return [ (r.get("metrics") or {}).get(key) for r in rows if (r.get("metrics") or {}).get(key) is not None ]
