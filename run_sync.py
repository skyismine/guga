# -*- coding: utf-8 -*-
"""跨机同步「决策/模型监控/复盘」运行时数据(本地/服务器之间)。

用法:
  1) 在【服务器】打包(只需标准库, Linux/Windows 均可):
       python run_sync.py pack                 # 默认产出 runtime_sync_<日期>.zip(项目根)
       python run_sync.py pack --out D:/tmp/runtime_20260903.zip
       python run_sync.py pack --with-snapshots # 额外含行情/情绪/板块快照(体积更大)
    完成后按提示 scp 回本地, 例如:
       scp user@host:/path/to/runtime_sync_20260903.zip .
  2) 在【本地】解包到 data_cache:
       python run_sync.py unpack --src runtime_sync_20260903.zip
       python run_sync.py --backfill unpack --src ...     # 解包后用本地日线缓存补回填
  3) 或一次性拉取(需本机已配好 ssh/scp):
       python run_sync.py pull --host host --user u --remote-dir /srv/guga --out .
       (等价于: scp → 解包, 带 --backfill 则补回填)

安全: 仅打包白名单文件, 解包仅还原白名单, 不做任意路径写入。
"""
import argparse
import datetime as dt
import os
import shutil
import subprocess
import sys
import zipfile

_WHITELIST_DIRS = ("reports",)          # data_cache 下按目录整包(仅 .md/.jsonl)
_WHITELIST_FILES = (
    "decision_history.jsonl",           # 决策分层快照(含 price/stop/目标价/类型/多周期actual)
    "model_monitor.jsonl",              # 模型方向归因(gbm/ens_dir)
    "portfolio.csv",                    # 持仓
    "operations.jsonl",                 # 交易流水
)
_SNAPSHOT_KEYS = ("market", "activity", "sector", "index", "vol_arch", "review", "alert")


def _data_dir() -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(here, "data_cache")


def _candidates(include_snapshots: bool):
    files = []
    for name in _WHITELIST_FILES:
        files.append(os.path.join("data_cache", name))
    for name in _WHITELIST_DIRS:
        files.append(os.path.join("data_cache", name))
    if include_snapshots:
        for n in os.listdir(_data_dir()):
            low = n.lower()
            if any(k in low for k in _SNAPSHOT_KEYS) and (n.endswith(".json") or n.endswith(".jsonl")):
                files.append(os.path.join("data_cache", n))
    out = []
    for rel in files:
        p = os.path.join(_data_dir(), os.path.relpath(rel, "data_cache"))
        if os.path.exists(p):
            out.append((rel, p))
    return out


def _default_out() -> str:
    return os.path.join(os.path.dirname(_data_dir()),
                        f"runtime_sync_{dt.date.today().strftime('%Y%m%d')}.zip")


def cmd_pack(args) -> int:
    out = args.out or _default_out()
    cands = _candidates(args.with_snapshots)
    if not cands:
        print("无白名单文件可打包(先确保 data_cache 下已有决策/监控记录)。")
        return 1
    n = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for rel, p in cands:
            if os.path.isdir(p):
                for root, _, fs in os.walk(p):
                    for f in fs:
                        if f.endswith((".md", ".jsonl")):
                            fp = os.path.join(root, f)
                            z.write(fp, os.path.join(rel, os.path.relpath(fp, p)))
                            n += 1
            else:
                z.write(p, rel)
                n += 1
    print(f"打包完成: {out} ({n} 项)")
    print(f"\n拷回本地示例:\n  scp user@host:{out} .")
    print("本地解包:\n  python run_sync.py unpack --src <该zip> [--backfill]")
    return 0


def cmd_unpack(args) -> int:
    src = args.src
    if not os.path.exists(src):
        print(f"找不到 {src}")
        return 1
    n = 0
    with zipfile.ZipFile(src) as z:
        for m in z.infolist():
            rel = m.filename.replace("\\", "/")
            parts = rel.split("/")
            if len(parts) < 2 or parts[0] != "data_cache":
                continue
            if parts[1] in _WHITELIST_DIRS:
                if not rel.endswith((".md", ".jsonl")):
                    continue
            elif rel not in os.path.join("data_cache", parts[1]) and \
                    rel != f"data_cache/{parts[1]}":
                # 白名单单文件: data_cache/<name>
                if len(parts) != 2 or parts[1] not in _WHITELIST_FILES:
                    continue
            dest = os.path.join(_data_dir(), *parts[1:])
            os.makedirs(os.path.dirname(dest) or _data_dir(), exist_ok=True)
            with z.open(m) as srcf, open(dest, "wb") as dstf:
                shutil.copyfileobj(srcf, dstf)
            n += 1
    print(f"解包完成: {src} → {_data_dir()} ({n} 项)")
    if args.backfill:
        return _backfill()
    return 0


def _backfill() -> int:
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from app.support import decision_tracker as _dt
        from app.support import model_monitor as _mm
        print("决策回填:", _dt.backfill_actuals(), "条(缺本地日线缓存的仍会留待补抓后重试)")
        print("模型回填:", _mm.backfill_actuals(), "条")
        print("决策指标:", _dt.metrics())
        return 0
    except Exception as e:  # noqa: BLE001
        print(f"回填失败(数据文件已就位,可稍后重试): {e}")
        return 1


def cmd_pull(args) -> int:
    out = args.out or _default_out()
    src = f"{args.user}@{args.host}:{args.remote_dir.rstrip('/')}/{os.path.basename(out)}"
    cmd = ["scp"]
    if args.port:
        cmd += ["-P", str(args.port)]
    cmd += [src, out]
    print("执行:", " ".join(cmd))
    try:
        subprocess.run(cmd, check=True)
    except Exception as e:  # noqa: BLE001
        print(f"scp 拉取失败(请手动拷贝后执行 unpack): {e}")
        return 1
    args.src = out
    return cmd_unpack(args)


def main() -> int:
    ap = argparse.ArgumentParser(description="决策/监控/复盘运行时数据 打包·同步·回填")
    sub = ap.add_subparsers(dest="mode", required=True)
    p1 = sub.add_parser("pack", help="(服务器)打包 data_cache 白名单为 zip")
    p1.add_argument("--out", default=None, help="输出 zip 路径(默认项目根 runtime_sync_<日期>.zip)")
    p1.add_argument("--with-snapshots", action="store_true", help="额外含行情/情绪/板块快照")
    p1.set_defaults(func=cmd_pack)
    p2 = sub.add_parser("unpack", help="(本地)把 zip 解到 data_cache")
    p2.add_argument("--src", required=True, help="zip 路径")
    p2.add_argument("--backfill", action="store_true", help="解包后用本地日线缓存补回填")
    p2.set_defaults(func=cmd_unpack)
    p3 = sub.add_parser("pull", help="(本地)scp 拉取服务器 zip 并解包")
    p3.add_argument("--host", required=True)
    p3.add_argument("--user", required=True)
    p3.add_argument("--port", type=int, default=None)
    p3.add_argument("--remote-dir", required=True, help="服务器上项目根(含 runtime_sync zip)")
    p3.add_argument("--out", default=None)
    p3.add_argument("--backfill", action="store_true")
    p3.set_defaults(func=cmd_pull)
    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
