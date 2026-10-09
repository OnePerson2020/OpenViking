"""ov.conf <-> secret-free template (ops/ov.conf.template.json).

    python3.13 ops/ovconf.py           # diff live ov.conf against the template; exit 1 on drift
    python3.13 ops/ovconf.py --write   # refresh the template from live (then commit it)

Restore: copy the template to ~/.openviking/ov.conf and fill every "<secret>".
"""
import difflib, json, sys
from pathlib import Path

LIVE = Path.home()/".openviking/ov.conf"; TPL = Path(__file__).with_name("ov.conf.template.json")
SECRETS = {"api_key", "root_api_key"}

def mask(o):
    if isinstance(o, dict): return {k: "<secret>" if k in SECRETS and v else mask(v) for k, v in o.items()}
    if isinstance(o, list): return [mask(v) for v in o]
    return o

live = json.dumps(mask(json.loads(LIVE.read_text())), indent=2, ensure_ascii=False) + "\n"
if "--write" in sys.argv:
    TPL.write_text(live); print(f"wrote {TPL}"); raise SystemExit(0)
d = list(difflib.unified_diff(TPL.read_text().splitlines(True), live.splitlines(True), "template", "live"))
sys.stdout.writelines(d or ["ov.conf matches template\n"]); raise SystemExit(1 if d else 0)
