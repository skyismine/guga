"""月度重训调度:固定月度频率调用 trainer 的 walk-forward 训练,持续适配市场风格变化。

- last_trained_at(): 读取模型 meta.trained_at(与 trainer.py 保存口径一致);
- should_retrain(): 判断是否到期(间隔天数模式 或 每月指定日模式);
- retrain_if_due(): 到期(或 force)时调用 trainer.train_all()——即现有 trainer.py
  的 walk-forward 重训逻辑,完成后写日志到 data_cache/retrain.log;
- start_daemon(): 后台线程定期检查(供 Web 自动重训);
- CLI: python -m app.scheduler [--check] [--force] [--daemon]
"""
import argparse
import datetime as dt
import logging
import os
import threading
import time

from app import config

_lock = threading.Lock()
_daemon_started = False
_logger = logging.getLogger("retrain")
_logger.addHandler(logging.NullHandler())


def _log(msg: str) -> None:
    try:
        os.makedirs(os.path.dirname(config.RETRAIN_LOG), exist_ok=True)
        with open(config.RETRAIN_LOG, "a", encoding="utf-8") as f:
            f.write(f"[{dt.datetime.now().isoformat(timespec='seconds')}] {msg}\n")
    except OSError:
        pass


def last_trained_at():
    """最近一次重训时间(模型 meta.trained_at,无模型则返回 None)。"""
    from app.ml.predictor import load_meta
    meta = load_meta()
    ts = (meta or {}).get("trained_at")
    if not ts:
        return None
    try:
        return dt.datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return None


def should_retrain(now: dt.datetime = None):
    """是否到期。返回 (是否, 原因)。模型缺失视为到期(首次训练)。"""
    if not getattr(config, "RETRAIN_ENABLED", True):
        return False, "自动重训已禁用(RETRAIN_ENABLED=False)"
    now = now or dt.datetime.now()
    last = last_trained_at()
    if last is None:
        return True, "模型不存在或缺少 trained_at(需首次训练)"
    if now < last:
        return False, "模型时间戳异常(晚于当前时间)"

    day = int(getattr(config, "RETRAIN_DAY_OF_MONTH", 0))
    if day > 0:
        if (now.year, now.month) != (last.year, last.month) and now.day >= day:
            return True, f"已到每月 {day} 日且本月尚未重训"
        return False, f"本月已重训({last.month} 月 {last.day} 日)"

    interval = max(1, int(getattr(config, "RETRAIN_INTERVAL_DAYS", 30)))
    days = (now - last).days
    if days >= interval:
        return True, f"距上次重训 {days} 天 >= {interval} 天(固定月度)"
    return False, f"距上次重训 {days} 天 < {interval} 天(固定月度)"


def retrain_if_due(force: bool = False, verbose: bool = True) -> dict:
    """到期(或 force)时调用 trainer.train_all() 重训,返回结果摘要。线程安全。"""
    with _lock:
        if force:
            reason = "手动强制重训"
        else:
            due, reason = should_retrain()
            if not due:
                return {"retrained": False, "reason": reason}
        _log(f"开始重训({reason})")
        try:
            from app.ml.trainer import train_all
            summary = train_all(verbose=verbose)
        except Exception as e:  # noqa: BLE001
            _log(f"重训失败: {e}")
            raise
        m = summary.get("metrics", {})
        _log(f"重训完成: method={summary.get('validation', {}).get('method')} "
             f"acc={m.get('accuracy')} f1={m.get('f1_weighted')} "
             f"up_auc={m.get('up_rank_metric')} down_auc={m.get('down_rank_metric')} "
             f"split={summary.get('split_date')} n_train={summary.get('n_train')}")
        return {
            "retrained": True,
            "reason": reason,
            "summary": {k: v for k, v in summary.items() if k != "feature_names"},
        }


def start_daemon(verbose: bool = True):
    """后台线程:定期检查并按固定月度频率重训(供 Web 自动重训)。"""
    global _daemon_started
    if _daemon_started:
        return None
    _daemon_started = True
    interval = max(60, int(getattr(config, "RETRAIN_CHECK_SECONDS", 3600)))

    def _loop():
        while True:
            try:
                res = retrain_if_due(verbose=verbose)
                if res["retrained"] and verbose:
                    print(f"[重训] 已完成,验证方法: "
                          f"{res['summary'].get('validation', {}).get('method')}")
            except Exception as e:  # noqa: BLE001
                _logger.warning("retrain daemon 异常: %s", e)
            time.sleep(interval)

    t = threading.Thread(target=_loop, name="retrain-daemon", daemon=True)
    t.start()
    return t


def main():
    ap = argparse.ArgumentParser(description="月度重训调度(与 trainer.py 重训逻辑一致)")
    ap.add_argument("--check", action="store_true", help="仅检查是否到期")
    ap.add_argument("--force", action="store_true", help="强制重训")
    ap.add_argument("--daemon", action="store_true", help="后台循环检查(守护模式)")
    args = ap.parse_args()

    if args.daemon:
        start_daemon()
        print(f"[重训] daemon 已启动,每 {config.RETRAIN_CHECK_SECONDS}s 检查一次,"
              f"到期自动重训(Ctrl+C 退出)")
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            print("\n[重训] daemon 已停止")
        return

    due, reason = should_retrain()
    if args.check:
        print(f"[重训] {'需要' if due else '无需'}重训: {reason}")
        return
    if args.force or due:
        res = retrain_if_due(force=args.force)
        s = res.get("summary", {})
        v = s.get("validation", {})
        m = s.get("metrics", {})
        print(f"[重训] 完成(force={args.force}): 验证方法 {v.get('method')}, "
              f"acc {m.get('accuracy')}, F1 {m.get('f1_weighted')}")
    else:
        print(f"[重训] 未到期: {reason}")


if __name__ == "__main__":
    main()
