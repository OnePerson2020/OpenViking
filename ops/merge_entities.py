"""Merge entity cards that are the same entity filed under different categories.

    python3.13 ops/merge_entities.py plan  [--out DIR]   # LLM-merge every group, write DIR/plan.jsonl; no data change
    python3.13 ops/merge_entities.py apply DIR           # tar backup, then write merged card + delete the others
    python3.13 ops/merge_entities.py plan DIR --keys k1 k2 --same   # only these groups, user-confirmed one entity
    python3.13 ops/merge_entities.py plan DIR --manual groups.json   # reviewed groups: [[uri, uri, ...], ...]

A group = cards in one entities root (user or one peer; never across spaces) whose file names
match after folding case and ./-/_/space, the same key extract_loop uses to route new entities.
Lossless: the largest card is kept verbatim (many cards are 10-90k chars, which an LLM rewrite
shortens); the model only lists facts from the other cards that the kept card lacks, appended as one
section. The kept card's metadata stays; its body is replaced through POST /content/write (memory
path: schema overview + embedding refresh). The others go through DELETE /fs, then category dirs left
without cards are removed. A group the model judges to be different entities is skipped, and
apply skips any group whose files changed since plan. Undo: restore the tar, then /content/reindex.
"""
import argparse, collections, concurrent.futures, hashlib, json, os, re, tarfile, time, urllib.error, urllib.parse, urllib.request
from pathlib import Path

OV = Path.home()/".openviking"
DATA = OV/"data/viking/default"
CONF = json.load(open(OV/"ov.conf"))
KEY = json.load(open(OV/"ovcli.conf"))["api_key"]
FIELDS = "<!-- MEMORY_FIELDS"


def key(name):
    return re.sub(r"[\s._\-]+", "_", name[:-3].strip().lower())


def uri(path):
    return "viking://" + str(path.relative_to(DATA))


def body(path):
    return path.read_text(encoding="utf-8").split(FIELDS, 1)[0].rstrip()


def groups(keys=None):
    for root in sorted(DATA.glob("user/*/memories/entities")) + sorted(DATA.glob("user/*/peers/*/memories/entities")):
        cards = [p for p in root.rglob("*.md") if not p.name.startswith(".")]
        by_key = collections.defaultdict(list)
        for p in cards:
            by_key[key(p.name)].append(p)
        for files in by_key.values():
            if len(files) > 1 and (not keys or key(files[0].name) in keys):
                files.sort(key=lambda p: (-len(body(p)), str(p)))
                yield files


SCHEMA = {"type": "object", "additionalProperties": False, "required": ["same_entity", "missing_facts"],
          "properties": {"same_entity": {"type": "boolean"},
                         "missing_facts": {"type": "array", "items": {"type": "string"}}}}
PROMPT = """These memory cards were filed for what may be the same real-world entity under different
categories. Card 1 is the base card and will be kept verbatim. Decide whether all cards describe the
same entity (same_entity). If yes, list in missing_facts every fact stated in cards 2..n that card 1
does not already state: names, IDs, paths, versions, dates, counts, relations, decisions, caveats.
- One self-contained fact per item, written like a card bullet without the leading "- ", keeping the
  date or version context it had (e.g. "2026-09-10: ..."). Keep identifiers, paths and code unchanged.
- Skip a fact only if card 1 clearly states it already; when unsure, include it.
- If a fact conflicts with card 1, include it with its date so the conflict stays visible.
- Write in card 1's language. Do not invent facts.
If they are different entities, set same_entity=false and missing_facts=[].
Return JSON only."""


def merge(files, same=False):
    cards = "\n\n".join(f"### card {i + 1}: {uri(p)} (modified {time.strftime('%Y-%m-%d', time.localtime(p.stat().st_mtime))})\n{body(p)}"
                        for i, p in enumerate(files))
    vlm = CONF["vlm"]
    req = {"model": vlm["model"], "temperature": 0, "max_tokens": 16000, "thinking": {"type": "disabled"},
           "messages": [{"role": "system", "content": PROMPT + ("\nThe user confirmed that all cards describe the same entity: same_entity must be true." if same else "")},
                        {"role": "user", "content": cards}],
           "response_format": {"type": "json_schema", "json_schema": {"name": "merge", "strict": True, "schema": SCHEMA}}}
    for attempt in range(4):
        try:
            r = urllib.request.urlopen(urllib.request.Request(
                vlm["api_base"].rstrip("/") + "/chat/completions", data=json.dumps(req).encode(),
                headers={"Authorization": f"Bearer {vlm['api_key']}", "Content-Type": "application/json"}), timeout=300)
            return json.loads(json.load(r)["choices"][0]["message"]["content"])
        except Exception as e:  # noqa: BLE001 - retry transient provider errors
            err = e
            time.sleep(10 * (attempt + 1))
    return {"error": repr(err)[:300]}


def merged(base, facts):
    if not facts:
        return base
    title = "## 合并补充（来自重复卡片）" if re.search(r"[\u4e00-\u9fff]", base) else "## Merged from duplicate cards"
    return base + "\n\n" + title + "\n" + "\n".join("- " + f.strip().lstrip("-* ").strip() for f in facts if f.strip())


def api(method, path, body=None, **query):
    url = "http://127.0.0.1:1933/api/v1" + path + ("?" + urllib.parse.urlencode(query) if query else "")
    req = urllib.request.Request(url, method=method, data=None if body is None else json.dumps(body).encode(),
                                 headers={"X-API-Key": KEY, "Content-Type": "application/json"})
    try:
        return json.load(urllib.request.urlopen(req, timeout=120))
    except urllib.error.HTTPError as e:
        return {"http": e.code, "body": e.read().decode()[:500]}


def plan(out, keys=None, same=False, manual=None):
    out.mkdir(parents=True, exist_ok=True)
    gs = list(groups(keys)) if not manual else [
        sorted((DATA/u[len("viking://"):] for u in g), key=lambda p: (-len(body(p)), str(p))) for g in json.load(open(manual))]
    same = same or bool(manual)
    print(f"{len(gs)} groups, {sum(map(len, gs))} cards")
    with concurrent.futures.ThreadPoolExecutor(6) as pool, open(out/"plan.jsonl", "w") as f:
        for files, res in zip(gs, pool.map(lambda fs: merge(fs, same), gs)):
            if res.get("same_entity"):
                res["content"] = merged(body(files[0]), res.get("missing_facts") or [])
            rec = {"keep": uri(files[0]), "delete": [uri(p) for p in files[1:]],
                   "sha256": {uri(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in files},
                   "sizes": [len(body(p)) for p in files], **res}
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            print(("ERROR " if "error" in rec else "" if rec.get("same_entity") else "DIFFERENT ") + rec["keep"], len(rec["delete"]), flush=True)


def apply(out):
    recs = [json.loads(l) for l in open(out/"plan.jsonl")]
    with tarfile.open(out/"entities-before.tar.gz", "w:gz") as tf:
        for root in list(DATA.glob("user/*/memories/entities")) + list(DATA.glob("user/*/peers/*/memories/entities")):
            tf.add(root, arcname=str(root.relative_to(DATA)))
    log, dirs = open(out/"apply.jsonl", "a"), set()
    for rec in recs:
        path = lambda u: DATA/u[len("viking://"):]
        stale = [u for u, h in rec["sha256"].items() if not path(u).exists() or hashlib.sha256(path(u).read_bytes()).hexdigest() != h]
        if not rec.get("same_entity") or not rec.get("content") or stale:
            log.write(json.dumps({"keep": rec["keep"], "skipped": "stale" if stale else "not merged", "stale": stale}, ensure_ascii=False) + "\n")
            continue
        target, delete = re.sub(r"\s", "_", rec["keep"]), list(rec["delete"])
        if target == rec["keep"]:  # replace keeps the card's metadata, refreshes overview + embedding
            w = api("POST", "/content/write", {"uri": target, "content": rec["content"] + "\n", "mode": "replace", "wait": False})
        else:  # the HTTP API maps whitespace in paths to "_": create there with the metadata, drop the old card
            raw = path(rec["keep"]).read_text(encoding="utf-8")
            meta = "\n\n" + FIELDS + raw.split(FIELDS, 1)[1] if FIELDS in raw else "\n"
            w = api("POST", "/content/write", {"uri": target, "content": rec["content"] + meta, "mode": "create", "wait": False})
            delete.append(rec["keep"])
        ok = w.get("status") == "ok"
        dels = [{"uri": u, "r": api("DELETE", "/fs", uri=u)} for u in delete] if ok else []
        dirs.update(path(u).parent for u in rec["delete"])
        log.write(json.dumps({"keep": target, "write": w.get("status") or w, "deleted": [d["uri"] for d in dels if d["r"].get("status") == "ok"],
                              "delete_errors": [d for d in dels if d["r"].get("status") != "ok"]}, ensure_ascii=False) + "\n")
        log.flush()
    for d in sorted(dirs):
        if d.exists() and not any(p for p in d.rglob("*.md") if not p.name.startswith(".")):
            log.write(json.dumps({"rmdir": uri(d), "r": api("DELETE", "/fs", uri=uri(d), recursive="true")}, ensure_ascii=False) + "\n")
    log.close()
    print("applied; see", out/"apply.jsonl")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["plan", "apply"])
    ap.add_argument("out", nargs="?", default=str(OV/f"local_patches/entity-merge/{time.strftime('%Y%m%d-%H%M')}"))
    ap.add_argument("--keys", nargs="+", help="only groups with these folded names")
    ap.add_argument("--same", action="store_true", help="user confirmed each group is one entity")
    ap.add_argument("--manual", help="JSON list of reviewed groups (lists of entity URIs); implies --same")
    a = ap.parse_args()
    plan(Path(a.out), a.keys, a.same, a.manual) if a.cmd == "plan" else apply(Path(a.out))
