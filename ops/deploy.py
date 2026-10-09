"""Deploy the `local` patch series onto the live OpenViking install (overlay over the upstream wheel).

    python3.13 ov-fork/ops/deploy.py            # plan + test only (dry run)
    python3.13 ov-fork/ops/deploy.py --apply    # deploy

Export REF (default `local`) -> plan vs live -> local_tests on a temp stage -> [--apply:]
wait for an idle queue -> back up live files -> stop -> install -> start -> health;
any failure restores the backup, then records the deployed commit in local_patches/deployed.
Files whose patch was dropped (e.g. merged upstream) go back to the upstream version.
Code only: data and ov.conf are untouched. Logs: local_patches/deploys/<timestamp>/.
"""
import argparse, glob, hashlib, io, json, os, shutil, subprocess, tarfile, time, urllib.request
from pathlib import Path

H = Path.home(); OV = H/".openviking"; LP = OV/"local_patches"; FORK = LP/"ov-fork"
SITE = H/".local/lib/python3.13/site-packages"; DEPLOYED = LP/"deployed"  # commit now live
RUNTESTS = FORK/"ops/test.sh"
ap = argparse.ArgumentParser(); ap.add_argument("--apply", action="store_true"); ap.add_argument("--ref", default="local")
ap.add_argument("--idle-wait", type=int, default=7200, help="seconds to wait for an idle queue; 0 = restart now, interrupting running work")
a = ap.parse_args()
OUT = LP/"deploys"/time.strftime("%Y%m%d-%H%M%S"); OUT.mkdir(parents=True)

def log(m):
    line = f"{time.strftime('%F %T')} {m}"; print(line, flush=True)
    with (OUT/"deploy.log").open("a") as f: f.write(line + "\n")
def git(*args, raw=False):
    out = subprocess.run(["git", "-C", str(FORK), *args], check=True, capture_output=True).stdout
    return out if raw else out.decode().strip()
sha = lambda p: hashlib.sha256(Path(p).read_bytes()).hexdigest() if Path(p).exists() else None
def api(path):
    srv = json.load(open(OV/"ov.conf")).get("server", {})
    req = urllib.request.Request(f"http://127.0.0.1:1933{path}",
                                 headers={"X-API-Key": srv.get("root_api_key") or srv.get("api_key") or ""})
    return json.load(urllib.request.urlopen(req, timeout=10))
def busy():
    n = 0
    for st in ("running", "pending"):
        r = api(f"/api/v1/tasks?status={st}").get("result")
        n += len(r if isinstance(r, list) else (r or {}).get("tasks", []))
    return n
def healthy():
    for _ in range(80):
        try:
            if json.load(urllib.request.urlopen("http://127.0.0.1:1933/health", timeout=3)).get("healthy"): return True
        except Exception: pass
        time.sleep(3)
    return False
def systemctl(verb): subprocess.run(["sudo", "-n", "systemctl", verb, "openviking.service"], check=True)

# 1. what to deploy
ver = [Path(d).name[len("openviking-"):-len(".dist-info")] for d in glob.glob(f"{SITE}/openviking-*.dist-info")]
assert len(ver) == 1, f"expected one openviking dist-info, got {ver}"
UP = f"v{ver[0]}"  # upstream release tag
git("rev-parse", "--verify", UP)
rev = git("rev-parse", "--short", a.ref)
overlay = lambda ref: git("diff", "--name-only", "--diff-filter=d", UP, ref, "--", "openviking", "openviking_cli").split()
cur = DEPLOYED.read_text().strip()
new, old = overlay(a.ref), overlay(cur)
exp = OUT/"export"; exp.mkdir()
tarfile.open(fileobj=io.BytesIO(git("archive", a.ref, *new, "local_tests", raw=True))).extractall(exp, filter="data")
up = OUT/"upstream"  # pristine versions of files whose patch was dropped
for f in set(old) - set(new):
    if subprocess.run(["git", "-C", str(FORK), "cat-file", "-e", f"{UP}:{f}"]).returncode == 0:
        (up/f).parent.mkdir(parents=True, exist_ok=True); (up/f).write_bytes(git("show", f"{UP}:{f}", raw=True))
src = {f: exp/f for f in new} | {f: up/f for f in set(old) - set(new)}  # missing path = delete
drift = [f for f in old if sha(SITE/f) != hashlib.sha256(git("show", f"{cur}:{f}", raw=True)).hexdigest()]
assert not drift, f"live differs from deployed {cur[:9]}, refusing: {drift}"
plan = sorted(f for f, s in src.items() if sha(SITE/f) != sha(s))
log(f"{a.ref}={rev} on {UP}: {len(new)} overlay files, {len(plan)} to change")
for f in plan: log(f"  {'delete' if not src[f].exists() else 'install' if f in new else 'revert'} {f}")
if not plan:
    shutil.rmtree(exp); DEPLOYED.write_text(git("rev-parse", a.ref) + "\n")
    log("live already matches; nothing to do"); raise SystemExit(0)

# 2. local_tests against live + plan
stage = OUT/"stage"
for d in ("openviking", "openviking_cli"):
    shutil.copytree(SITE/d, stage/d, ignore=shutil.ignore_patterns("__pycache__"), symlinks=True)
def put(root, f):
    if src[f].exists(): (root/f).parent.mkdir(parents=True, exist_ok=True); shutil.copy2(src[f], root/f)
    elif (root/f).exists(): (root/f).unlink()
for f in plan: put(stage, f)
t = subprocess.run([str(RUNTESTS), str(stage)], env=os.environ | {"TESTS": str(exp/"local_tests")},
                   capture_output=True, text=True)
(OUT/"tests.log").write_text(t.stdout + t.stderr); log("tests: " + (t.stdout.strip().splitlines() or ["?"])[-1])
shutil.rmtree(stage)
assert t.returncode == 0, "local_tests failed; see tests.log"
if not a.apply:
    shutil.rmtree(exp); shutil.rmtree(up, ignore_errors=True)
    log("dry run; re-run with --apply to deploy"); raise SystemExit(0)

# 3. deploy
if a.idle_wait == 0:  # forced: QueueFS redelivers interrupted commits after the restart
    log(f"not waiting for idle queue: {busy()} running/pending will be interrupted")
else:
    for i in range(a.idle_wait // 30 + 1):
        n = busy()
        if n == 0: break
        if i % 10 == 0: log(f"waiting for idle queue: {n} running/pending")
        time.sleep(30)
    else:
        raise SystemExit("queue never idle; nothing changed")
bk = OUT/"backup"
for f in plan:
    if (SITE/f).exists(): (bk/f).parent.mkdir(parents=True, exist_ok=True); shutil.copy2(SITE/f, bk/f)
systemctl("stop")
try:
    for f in plan: put(SITE, f)
    systemctl("start")
    if not healthy(): raise RuntimeError("not healthy after start")
    log("installed; healthy")
except Exception as e:
    log(f"FAILED {e!r}; restoring backup")
    for f in plan:
        if (bk/f).exists(): shutil.copy2(bk/f, SITE/f)
        elif (SITE/f).exists(): (SITE/f).unlink()
    subprocess.run(["sudo", "-n", "systemctl", "restart", "openviking.service"])
    log(f"rollback {'healthy' if healthy() else 'NOT HEALTHY, check journalctl -u openviking'}"); raise

# 4. record what is now live
DEPLOYED.write_text(git("rev-parse", a.ref) + "\n")
shutil.rmtree(exp); shutil.rmtree(up, ignore_errors=True)
log(f"deployed {a.ref}={rev}")
