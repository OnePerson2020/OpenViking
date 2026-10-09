"""Hash-bound retry of one failed archive, waiting while its session is busy.

    python3.13 ops/retry_archive.py <session_id> <archive_id> [--user mayunxiang]

Uses the user's key from ~/.openviking/ovcli.conf. Long-term steps already recorded
are not repeated. Afterwards clear the old task record with ops/purge_task.py.
"""
import argparse, hashlib, json, time, urllib.request
from pathlib import Path

ap = argparse.ArgumentParser(); ap.add_argument("session"); ap.add_argument("archive"); ap.add_argument("--user", default="mayunxiang")
a = ap.parse_args()
OV = Path.home()/".openviking"; key = json.load(open(OV/"ovcli.conf"))["api_key"]
A = OV/f"data/viking/default/user/{a.user}/sessions/{a.session}/history/{a.archive}"
def call(method, path, body=None):
    req = urllib.request.Request(f"http://127.0.0.1:1933{path}", method=method,
        data=None if body is None else json.dumps(body).encode(),
        headers={"X-API-Key": key, "Content-Type": "application/json"})
    try: return json.load(urllib.request.urlopen(req, timeout=60))
    except urllib.error.HTTPError as e: return {"http": e.code, "body": e.read().decode()[:800]}
h = hashlib.sha256((A/"messages.jsonl").read_bytes()).hexdigest()
for _ in range(240):  # the session's own live commits make retry skip with session_busy
    r = call("POST", f"/api/v1/sessions/{a.session}/archives/{a.archive}/retry", {"expected_messages_sha256": h})
    print(time.strftime("%T"), json.dumps(r, ensure_ascii=False)[:400], flush=True)
    if (r.get("result") or {}).get("reason") not in ("session_busy", "archive_owned"): break
    time.sleep(60)
tid = (r.get("result") or {}).get("task_id")
for _ in range(480):
    if not tid: break
    st = (call("GET", f"/api/v1/tasks/{tid}").get("result") or {}).get("status")
    if st in ("completed", "failed", "cancelled"): print("task", tid, st); break
    time.sleep(15)
print("done:", (A/".done").exists(), "failed marker:", (A/".failed.json").exists())
