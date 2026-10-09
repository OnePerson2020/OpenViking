"""Read-only daily health check of the devbox OpenViking server. Exit 1 when something needs attention.

    python3.13 ops/healthcheck.py            # human summary
    python3.13 ops/healthcheck.py --json

Checks: /health; archives with .failed.json or stuck without .done > STUCK_H hours;
failed tasks; queue errors/backlog; ERROR lines in today's openviking.log.
"""
import glob, json, os, re, sys, time, urllib.request
from pathlib import Path

OV = Path.home()/".openviking"; STUCK_H = 2
V = Path(os.environ.get("OV_HEALTH_DATA", OV/"data/viking/default"))  # override for self-test
key = json.load(open(OV/"ov.conf"))["server"]["root_api_key"]
def get(p):
    req = urllib.request.Request(f"http://127.0.0.1:1933{p}", headers={"X-API-Key": key})
    return json.load(urllib.request.urlopen(req, timeout=20))
issues, info = [], {}

try: info["healthy"] = bool(get("/health").get("healthy"))
except Exception as e: info["healthy"] = False; info["health_error"] = repr(e)
if not info["healthy"]: issues.append("server not healthy")

failed, stuck, now = [], [], time.time()
for a in glob.glob(f"{V}/user/*/sessions/*/history/archive_*"):
    if os.path.exists(f"{a}/.failed.json"): failed.append(a.split("/sessions/")[1])
    elif not os.path.exists(f"{a}/.done") and now - os.path.getmtime(a) > STUCK_H * 3600:
        stuck.append(a.split("/sessions/")[1])
info["failed_archives"], info["stuck_archives"] = failed, stuck
if failed: issues.append(f"{len(failed)} failed archive(s)")
if stuck: issues.append(f"{len(stuck)} archive(s) unfinished > {STUCK_H}h")

if info["healthy"]:
    r = get("/api/v1/tasks?status=failed").get("result")
    tasks = r if isinstance(r, list) else (r or {}).get("tasks", [])
    info["failed_tasks"] = [f'{t.get("task_type")} {t.get("resource_id")}: {str(t.get("error"))[:120]}' for t in tasks]
    if tasks: issues.append(f"{len(tasks)} failed task(s)")
    table = str((get("/api/v1/observer/queue").get("result") or {}).get("status", ""))
    for row in table.splitlines():
        cells = [c.strip() for c in row.strip("|").split("|")]
        if len(cells) == 7 and cells[1].isdigit():
            name, pending, errors = cells[0], int(cells[1]), int(cells[5])
            if errors: issues.append(f"queue {name}: {errors} error(s)")
            if pending > 50: issues.append(f"queue {name}: {pending} pending")

log = OV/"logs/openviking.log"; today = time.strftime("%Y-%m-%d")
errs = [l.strip()[:200] for l in open(log, errors="replace")
        if l.startswith(today) and " - ERROR - " in l] if log.exists() else []
info["log_errors_today"] = errs[-10:]
if errs: issues.append(f"{len(errs)} ERROR log line(s) today")

info["issues"] = issues
if "--json" in sys.argv: print(json.dumps(info, ensure_ascii=False, indent=1))
else:
    print("OK" if not issues else "ATTENTION: " + "; ".join(issues))
    for k in ("failed_archives", "stuck_archives", "failed_tasks", "log_errors_today"):
        for x in info.get(k, [])[:10]: print(f"  {k}: {x}")
sys.exit(1 if issues else 0)
