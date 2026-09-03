# -*- coding: utf-8 -*-
"""模型性能监控(4.3): 每日记录预测 → 回填实际涨跌 → 计算准确率/精确率/召回率。

- 记录: _predict_one 每预测一个标的写入一条(当日同 code 去重)。
- 回填: 次日/后续用本地日线缓存取「预测日后首个交易日实际涨跌」标记真实方向。
- 指标: 方向准确率 / 上涨类精确率与召回率(近 window_days 天, 最小样本 min_samples)。
- 健康: 准确率 < accuracy_threshold 时触发告警(dal 日志)并返回 unhealthy,
  上层据此对 GBM 概率作轻度降权(不中断, 降级参考)。
数据文件: data_cache/model_monitor.jsonl。全量逻辑容错, 不影响主流程。
"""
import datetime as dt
import json
import os

from app import config
from app.data import dal

_MON_FILE = os.path.join(config.DATA_DIR, "model_monitor.jsonl")
# 方向映射: 兼容英文(up/flat/down)与引擎输出的中文(上涨/下跌/震荡/持平)
_DIRN = {"up": 1, "flat": 0, "down": -1,
         "上涨": 1, "下跌": -1, "震荡": 0, "平盘": 0, "持平": 0, "观望": 0,
         None: None}
# 当日已记录标的(进程内去重, 避免每标的全量重写 jsonl 的 O(n²) 开销)
_TODAY_MEM = {}


def _cfg() -> dict:
    try:
        from app.support import settings as _st
        return (_st.load().get("target_match", {}) or {}).get("model_monitor", {}) or {}
    except Exception:  # noqa: BLE001
        return {}


def _today() -> str:
    return dt.date.today().isoformat()


def _load_rows() -> list:
    if not os.path.exists(_MON_FILE):
        return []
    out = {}
    try:
        with open(_MON_FILE, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                    if r.get("code") and r.get("date"):
                        out[(r["date"], r["code"])] = r   # 同 date+code 保留最新
                except Exception:  # noqa: BLE001
                    continue
    except OSError:
        return []
    return list(out.values())


def _save_rows(rows: list) -> None:
    try:
        dal.locked_write(_MON_FILE, "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n")
    except Exception:  # noqa: BLE001
        pass


def record(code: str, pred: dict) -> None:
    """记录一次预测(当日同 code 去重; 追加写 + 进程内记忆, 避免每标的全量重写)。

    direction = 模型(Gbm)原始方向(metrics 现行口径, 兼容); 铺路归因字段:
    src = 方向来源(gbm), ens_dir = 集成投票后方向(-1/0/1, 未启用为 None),
    ens_agree = 模型与集成是否一致 —— 样本积累后可按 gbm/集成分别算准确率(动态调权/降级前提)。
    """
    if not (pred and pred.get("direction")):
        return
    today = _today()
    code = str(code).zfill(6)
    ens = pred.get("ensemble") or {}
    rec = {
        "date": today,
        "code": code,
        "p_up": round(float(pred.get("p_up") or 0), 4),
        "p_down": round(float(pred.get("p_down") or 0), 4),
        "direction": _DIRN.get(pred.get("direction")),
        "src": "gbm",
        "ens_dir": ens.get("ensemble_dir"),
        "ens_agree": ens.get("agree"),
        "actual": None,
    }
    if _TODAY_MEM.get(code) == rec:
        return
    _TODAY_MEM[code] = rec
    try:
        with open(_MON_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:  # noqa: BLE001
        pass


def backfill_actuals() -> int:
    """为无 actual 的记录回填「预测日后首个交易日实际涨跌方向」。返回回填条数。"""
    rows = _load_rows()
    filled = 0
    changed = False
    for r in rows:
        if r.get("actual") is not None:
            continue
        act = _next_ret(r.get("date"), r.get("code"))
        if act is None:
            continue
        r["actual"] = 1 if act > 0 else (-1 if act < 0 else 0)
        filled += 1
        changed = True
    if changed:
        _save_rows(rows)
    return filled


def _next_ret(date: str, code: str):
    """预测日后首个交易日的实际涨跌幅(本地日线缓存)。无可得返回 None。"""
    try:
        from app.data import fetcher as _f
        df = _f._load_cache(str(code).zfill(6))
        if df is None or df.empty or "close" not in df.columns:
            return None
        idx = df.index.astype(str)
        mask = idx > str(date)
        if not mask.any():
            return None
        nxt = df.loc[mask, "close"].astype(float)
        pos = df.index.astype(str).get_loc(nxt.index[0])
        prev = float(df["close"].iloc[pos - 1])
        return float(nxt.iloc[0] / prev - 1) if prev > 0 else None
    except Exception:  # noqa: BLE001
        return None


def metrics(window_days: int = None, min_samples: int = None) -> dict:
    """近 window_days 天(自然日)的准确率/精确率/召回率/样本数。"""
    c = _cfg()
    window_days = window_days or int(c.get("window_days", 60) or 60)
    min_samples = min_samples if min_samples is not None else int(c.get("min_samples", 20) or 20)
    rows = _load_rows()
    if not rows:
        return {"ok": False, "samples": 0, "reason": "无预测记录"}
    cutoff = (dt.date.today() - dt.timedelta(days=window_days)).isoformat()
    rs = [r for r in rows if r.get("date", "") >= cutoff and r.get("actual") is not None]
    if not rs:
        return {"ok": False, "samples": 0, "reason": "窗口内无已回填样本"}
    n = len(rs)
    correct = sum(1 for r in rs if r.get("direction") == r.get("actual"))
    acc = correct / n
    # 上涨类精确率/召回率
    pred_up = [r for r in rs if r.get("direction") == 1]
    tp = sum(1 for r in pred_up if r.get("actual") == 1)
    prec = tp / len(pred_up) if pred_up else None
    recall = tp / sum(1 for r in rs if r.get("actual") == 1) if sum(1 for r in rs if r.get("actual") == 1) else None
    return {"ok": n >= min_samples, "samples": n, "accuracy": round(acc, 4),
            "precision_up": round(prec, 4) if prec is not None else None,
            "recall_up": round(recall, 4) if recall is not None else None}


def health() -> dict:
    """模型健康度: 样本充足且准确率>=阈值 → healthy; 否则告警并降级参考。"""
    c = _cfg()
    m = metrics()
    thr = float(c.get("accuracy_threshold", 0.55) or 0.55)
    if not m.get("ok"):
        return {"healthy": True, "reason": "样本不足,暂不判定", "metrics": m}
    healthy = m["accuracy"] >= thr
    if not healthy:
        dal.record_missing("model_monitor", False,
                           f"模型准确率 {m['accuracy']:.1%} < 阈值 {thr:.0%},暂停参考并告警")
    return {"healthy": healthy, "reason": ("达标" if healthy else f"准确率 {m['accuracy']:.1%} < {thr:.0%}"),
            "accuracy": m["accuracy"], "samples": m["samples"], "metrics": m}


if __name__ == "__main__":
    import io, sys
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    print("回填:", backfill_actuals(), "条")
    print("指标:", metrics())
    print("健康:", health())
