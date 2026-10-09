"""Remove one failed task record once its archive is resolved (.done, no .failed.json).

    python3.13 ops/purge_task.py <backup_dir> <task_id> <session_id> <archive_id>

Tar backup first, then wait for an idle queue, stop, remove, start, health
(the task tracker caches records, so the service must be stopped).
"""
import glob, json, os, subprocess, sys, tarfile, time, urllib.request
from pathlib import Path
OV = Path.home()/".openviking"; V = OV/"data/viking/default"; OUT = Path(sys.argv[1])
OLD = sys.argv[2]
A = V/f"user/mayunxiang/sessions/{sys.argv[3]}/history/{sys.argv[4]}"
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
hits = [f for f in glob.glob(f"{V}/_system/tasks/**/*.json", recursive=True) if OLD in os.path.basename(f)]
assert len(hits) == 1 and json.load(open(hits[0])).get("status") == "failed", hits
assert (A/".done").exists() and not (A/".failed.json").exists(), "archive not resolved"
with tarfile.open(OUT/f"purged-task-{OLD[:8]}.tar.gz", "w:gz") as tar:
    tar.add(hits[0], arcname=os.path.relpath(hits[0], V))
for i in range(1440):
    if busy() == 0: break
    time.sleep(30)
else:
    raise SystemExit("queue never idle; record kept")
subprocess.run(["sudo", "-n", "systemctl", "stop", "openviking.service"], check=True)
try:
    os.remove(hits[0])
finally:
    subprocess.run(["sudo", "-n", "systemctl", "start", "openviking.service"], check=True)
log(f"purged failed task record {OLD}; healthy={healthy()}")
