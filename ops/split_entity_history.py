"""Split oversized entity cards: dated process records -> events, stable facts -> one compact card.

    python3.13 ops/split_entity_history.py plan DIR KEEP_URI URI [URI ...]   # LLM work, writes DIR/plan.json; no data change
    python3.13 ops/split_entity_history.py apply DIR                         # backup, archive resource, events, card, deletes

Lossless by construction: the original cards go verbatim into one resource document (resources
are chunk-indexed, the right home for long reference text); every dated record becomes an event
unless an existing event on that day already covers it; the compact card keeps only facts that
are still true. KEEP_URI's metadata stays (its body is replaced); the other cards are deleted.
"""
import collections, concurrent.futures, hashlib, json, re, sys, tarfile, time, urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from merge_entities import DATA, FIELDS, CONF, KEY, api, body  # noqa: E402

CHUNK = 15000


def llm(system, user, schema, max_tokens=16000):
    vlm = CONF["vlm"]
    req = {"model": vlm["model"], "temperature": 0, "max_tokens": max_tokens, "thinking": {"type": "disabled"},
           "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
           "response_format": {"type": "json_schema", "json_schema": {"name": "out", "strict": True, "schema": schema}}}
    for attempt in range(4):
        try:
            r = urllib.request.urlopen(urllib.request.Request(
                vlm["api_base"].rstrip("/") + "/chat/completions", data=json.dumps(req).encode(),
                headers={"Authorization": f"Bearer {vlm['api_key']}", "Content-Type": "application/json"}), timeout=600)
            return json.loads(json.load(r)["choices"][0]["message"]["content"])
        except Exception as e:  # noqa: BLE001 - retry transient provider errors
            err = repr(e)[:200] + (" " + e.read().decode(errors="replace")[:300] if hasattr(e, "read") else "")
            time.sleep(10 * (attempt + 1))
    raise RuntimeError(err)


def obj(props):
    return {"type": "object", "additionalProperties": False, "required": list(props), "properties": props}


STR, STRS = {"type": "string"}, {"type": "array", "items": {"type": "string"}}
CLASSIFY = obj({"stable": STRS, "records": {"type": "array", "items": obj({"date": STR, "text": STR})}})
CLASSIFY_PROMPT = """You split part of a memory card about one software system. Put every statement into exactly one list:
- stable: what the system is and what is currently true about it (identity, aliases, location, ports, architecture,
  components, configuration, current behavior and rules). Drop a statement here only if it is an exact repeat.
- records: something that happened at a point in time (a change, fix, deploy, investigation, test, measurement,
  incident, decision) with its YYYY-MM-DD date; use the date written in or next to the statement, "" if none.
Keep identifiers, paths, numbers and code unchanged; one self-contained statement per item, in the source language.
Return JSON only."""
EVENTS = obj({"events": {"type": "array", "items": obj({"event_name": STR, "goal": STR, "summary": STR})}})
COVER = obj({"uncovered": {"type": "array", "items": {"type": "integer"}}})
COVER_PROMPT = """Each numbered record comes with the most similar events that already exist. A record is covered when one
of those events already states the same fact (wording may differ). Return the numbers of the records that are NOT
covered. Return JSON only."""
EVENTS_PROMPT = """You turn dated process records about one software system, all from the same day, into event memories.
- Group the records by topic (one feature, fix series, review, investigation, deploy, decision) into at most {cap}
  events; an event's summary may list several related changes. Keep every record's facts in some event; drop only
  records with no information beyond "something was done".
- event_name: short, in the records' language; English names lowercase_with_underscores, max 4 words; no dates/numbers.
- goal: 5 words or less. summary: complete facts with the exact date (YYYY-MM-DD), keeping identifiers, paths,
  numbers, causes and outcomes.
Return JSON only."""
CARD = obj({"card": STR})
CARD_PROMPT = """Write the single compact entity card for this software system from the stable facts below (they come
from several overlapping cards; some are outdated or contradictory).
- Markdown: one H1 display name, then one plain sentence on the next line (not starting with -, * or 1.), then H2
  sections with bullet lists only: names/aliases (every name the system went by), location and runtime, architecture
  and components, configuration and operating rules, related entities.
- Keep what is still true and useful for later questions; merge repeats; where facts conflict prefer the newer one.
  Leave out dated history: it lives in events. Target 3000-7000 characters. A pointer to the archive is appended
  for you; do not add one.
Write in Chinese, keeping identifiers, paths, ports and code unchanged. Return JSON only."""


def chunks(text):
    parts, cur = [], ""
    for sec in re.split(r"(?m)^(?=## )", text):
        while len(sec) > CHUNK:  # one oversized section: cut at line breaks
            cut = sec.rfind("\n", 0, CHUNK)
            cut = cut if cut > 0 else CHUNK
            parts.append(sec[:cut]); sec = sec[cut:]
        if len(cur) + len(sec) > CHUNK and cur:
            parts.append(cur); cur = ""
        cur += sec
    return parts + ([cur] if cur.strip() else [])


def plan(out, keep, uris):
    out.mkdir(parents=True, exist_ok=True)
    cards = {u: body(DATA/u[len("viking://"):]) for u in uris}
    user_space = keep.split("/")[3]
    archive = "\n\n".join(f"# {u}\n\n{t}" for u, t in cards.items())
    work = [(u, c) for u, t in cards.items() for c in chunks(t)]
    print(f"{len(cards)} cards, {sum(map(len, cards.values()))} chars, {len(work)} chunks", flush=True)
    cache = out/"classify.json"  # the slow step; reused when plan is re-run
    if cache.exists():
        parts = json.load(open(cache))
    else:
        with concurrent.futures.ThreadPoolExecutor(6) as pool:
            parts = list(pool.map(lambda w: llm(CLASSIFY_PROMPT, f"Card {w[0]}:\n\n{w[1]}", CLASSIFY), work))
        json.dump(parts, open(cache, "w"), ensure_ascii=False)
    stable = [s for p in parts for s in p["stable"]]
    records = [r for p in parts for r in p["records"]]
    by_day = collections.defaultdict(list)
    for r in records:
        if re.fullmatch(r"20\d\d-\d\d-\d\d", r["date"]):
            by_day[r["date"]].append(r["text"])
    print(f"stable {len(stable)}, records {len(records)} ({len(records) - sum(map(len, by_day.values()))} undated), days {len(by_day)}", flush=True)

    events_root = f"viking://user/{user_space}/memories/events"

    def similar(text):  # coverage check by semantic search: a busy day can hold 1000+ events
        r = api("POST", "/search/find", {"query": text[:500], "target_uri": events_root, "limit": 3})
        return [f"{m['uri'].split('/events/')[-1]}: {(m.get('abstract') or '')[:300]}" for m in (r.get("result") or {}).get("memories", [])]

    with concurrent.futures.ThreadPoolExecutor(6) as pool:
        hits = dict(zip([t for d in by_day for t in by_day[d]], pool.map(similar, [t for d in by_day for t in by_day[d]])))

    def day_events(day):
        recs, todo = by_day[day], []
        for i in range(0, len(recs), 25):  # 1) coverage, in batches
            batch = recs[i:i + 25]
            msg = "\n\n".join(f"{n}. {t}\nMost similar existing events:\n" + ("\n".join("  - " + h for h in hits[t]) or "  (none)")
                               for n, t in enumerate(batch, 1))
            todo += [batch[n - 1] for n in llm(COVER_PROMPT, msg, COVER)["uncovered"] if 1 <= n <= len(batch)]
        if not todo:
            return []
        cap = max(1, min(8, -(-len(todo) // 6)))  # 2) one grouping call per day
        res = llm(EVENTS_PROMPT.format(cap=cap), f"Day {day}\n\n" + "\n".join("- " + t for t in todo), EVENTS)
        return [{"date": day, **e} for e in res["events"]]
    with concurrent.futures.ThreadPoolExecutor(6) as pool:
        events = [e for es in pool.map(day_events, sorted(by_day)) for e in es]
    archive_uri = f"viking://resources/entity-archive/{keep.rsplit('/', 1)[1][:-3]}-cards-{time.strftime('%Y%m%d')}"
    old = json.load(open(out/"plan.json")) if (out/"plan.json").exists() else {}
    card = old.get("card") or llm(CARD_PROMPT, "Stable facts:\n" + "\n".join("- " + s for s in stable), CARD)["card"].rstrip()
    # a markdown link: a bare viking:// URI is rewritten by the server's resource-ref sync
    card += f"\n- 完整历史档案（{len(uris)} 张原卡原文）：[{archive_uri.rsplit('/', 1)[1]}]({archive_uri})"
    (out/"archive.md").write_text(archive, encoding="utf-8")
    json.dump({"keep": keep, "delete": [u for u in uris if u != keep], "archive_uri": archive_uri,
               "sha256": {u: hashlib.sha256((DATA/u[len("viking://"):]).read_bytes()).hexdigest() for u in uris},
               "stable": stable, "records": records, "events": events, "card": card},
              open(out/"plan.json", "w"), ensure_ascii=False, indent=1)
    print(f"events {len(events)}, card {len(card)} chars -> {out/'plan.json'}")


def apply(out):
    p = json.load(open(out/"plan.json"))
    path = lambda u: DATA/u[len("viking://"):]
    stale = [u for u, h in p["sha256"].items() if not path(u).exists() or hashlib.sha256(path(u).read_bytes()).hexdigest() != h]
    if stale:
        raise SystemExit(f"cards changed since plan, re-plan: {stale}")
    with tarfile.open(out/"cards-before.tar.gz", "w:gz") as tf:
        for u in p["sha256"]:
            tf.add(path(u), arcname=u[len("viking://"):])
    log = open(out/"apply.jsonl", "a")
    rec = lambda **kw: (log.write(json.dumps(kw, ensure_ascii=False) + "\n"), log.flush(), print(json.dumps(kw, ensure_ascii=False)[:200]))
    up = urllib.request.Request("http://127.0.0.1:1933/api/v1/resources/temp_upload", method="POST",
                                data=multipart(out/"archive.md"), headers={"X-API-Key": KEY, "Content-Type": "multipart/form-data; boundary=" + BOUNDARY})
    tmp = json.load(urllib.request.urlopen(up, timeout=120))
    tid = (tmp.get("result") or {}).get("temp_file_id")
    rec(step="archive", r=api("POST", "/resources", {"temp_file_id": tid, "to": p["archive_uri"], "reason": "entity card split archive",
                                                     "wait": False}) if tid else tmp)
    user_space = p["keep"].split("/")[3]
    for e in p["events"]:
        name = re.sub(r"[\s/]+", "_", e["event_name"].strip())
        base = f"viking://user/{user_space}/memories/events/{e['date'].replace('-', '/')}/{name}"
        uri, n = base + ".md", 2
        while path(uri).exists():
            uri, n = f"{base}_{n}.md", n + 1
        meta = {"event_name": e["event_name"], "goal": e["goal"], "summary": e["summary"], "memory_type": "events", "version": 1}
        content = f"# Summary\n{e['summary']}\n\n# Source\n拆分自实体卡的过程记录（{time.strftime('%Y-%m-%d')}），原文见 {p['archive_uri']}\n\n{FIELDS}\n{json.dumps(meta, ensure_ascii=False, indent=2)}\n-->"
        rec(step="event", uri=uri, r=api("POST", "/content/write", {"uri": uri, "content": content, "mode": "create", "wait": False}).get("status"))
    w = api("POST", "/content/write", {"uri": p["keep"], "content": p["card"] + "\n", "mode": "replace", "wait": False})
    rec(step="card", uri=p["keep"], r=w.get("status") or w)
    if w.get("status") == "ok" and body(path(p["keep"])) == p["card"].rstrip():
        for u in p["delete"]:
            rec(step="delete", uri=u, r=api("DELETE", "/fs", uri=u).get("status"))
    log.close()


BOUNDARY = "----splitentity" + hashlib.md5(str(time.time()).encode()).hexdigest()


def multipart(f):
    head = (f'--{BOUNDARY}\r\nContent-Disposition: form-data; name="file"; filename="{f.name}"\r\n'
            "Content-Type: text/markdown\r\n\r\n").encode()
    return head + f.read_bytes() + f"\r\n--{BOUNDARY}--\r\n".encode()


if __name__ == "__main__":
    if sys.argv[1] == "plan":
        plan(Path(sys.argv[2]), sys.argv[3], sys.argv[3:] if sys.argv[3] in sys.argv[4:] else [sys.argv[3]] + sys.argv[4:])
    else:
        apply(Path(sys.argv[2]))
