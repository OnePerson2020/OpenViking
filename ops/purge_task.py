"""Remove failed task records that are resolved, in one restart.

    python3.13 ops/purge_task.py <backup_dir> [--now] TASK_ID[=SESSION_ID/ARCHIVE_ID] ...

TASK_ID=SESSION/ARCHIVE (session_commit): only once that archive has .done and no
.failed.json. A bare TASK_ID: only for non-session tasks (e.g. add_resource whose
source is gone) — nothing to retry, the record just keeps the daily check red.
Tar backup first, then wait for an idle queue (--now: restart right away; queued
work is redelivered), stop, remove, start, health. The task tracker caches records,
so the service must be stopped.
"""
import argparse, glob, json, os, subprocess, tarfile, time, urllib.request
from pathlib import Path
OV = Path.home()/".openviking"; V = OV/"data/viking/default"
ap = argparse.ArgumentParser(); ap.add_argument("backup_dir", type=Path); ap.add_argument("--now", action="store_true")
ap.add_argument("records", nargs="+"); a = ap.parse_args(); OUT = a.backup_dir; OUT.mkdir(parents=True, exist_ok=True)
key = json.load(open(OV/"ov.conf"))["server"]["root_api_key"]
def log(m):
    line = f"{time.strftime('%F %T')} {m}"; print(line, flush=True); (OUT/"purge.log").open("a").write(line + "\n")
def busy():
    n = 0
    for st in ("running", "pending"):
        r = json.load(urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:1933/api/v1/tasks?status={st}", headers={"X-API-Key": key}), timeout=10)).get("result")
        n += len(r if isinstance(r, list) else (r or {}).get("tasks", []))
    return n
def healthy():
    for _ in range(80):
        try:
            if json.load(urllib.request.urlopen("http://127.0.0.1:1933/health", timeout=3)).get("healthy"): return True
        except Exception: pass
        time.sleep(3)
    return False
files = []
for rec in a.records:
    task, _, where = rec.partition("=")
    hits = [f for f in glob.glob(f"{V}/_system/tasks/**/*.json", recursive=True) if task in os.path.basename(f)]
    assert len(hits) == 1, f"{task}: {hits}"
    record = json.load(open(hits[0]))
    assert record.get("status") == "failed", f"{task} is {record.get('status')}"
    if where:
        A = V/f"user/mayunxiang/sessions/{where.split('/')[0]}/history/{where.split('/')[1]}"
        assert (A/".done").exists() and not (A/".failed.json").exists(), f"{task}: archive not resolved"
    else:
        assert record.get("task_type") != "session_commit", f"{task}: session_commit needs =SESSION/ARCHIVE"
    files.append(hits[0])
with tarfile.open(OUT/f"purged-tasks-{time.strftime('%Y%m%d-%H%M%S')}.tar.gz", "w:gz") as tar:
    for f in files: tar.add(f, arcname=os.path.relpath(f, V))
if not a.now:
    for _ in range(1440):
        if busy() == 0: break
        time.sleep(30)
    else:
        raise SystemExit("queue never idle; records kept")
subprocess.run(["sudo", "-n", "systemctl", "stop", "openviking.service"], check=True)
try:
    for f in files: os.remove(f)
finally:
    subprocess.run(["sudo", "-n", "systemctl", "start", "openviking.service"], check=True)
log(f"purged {len(files)} failed task record(s) {[os.path.basename(f) for f in files]}; healthy={healthy()}")
