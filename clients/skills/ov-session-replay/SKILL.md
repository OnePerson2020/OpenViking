---
name: ov-session-replay
description: Trace a vaguely remembered past event, decision, error, file or image back to the original agent sessions stored in OpenViking (Claude Code, Codex, pi, including subagents), and replay them at increasing fidelity — L0 one-line summaries, L1 working-memory overviews, L2 raw messages, L3 byte-exact tool outputs and produced files. Use when the user asks "what happened when…", "当时是怎么…", "回溯/回放/找一下之前…", wants the original wording, wants to see how something changed over time, or wants an artifact (code, HTML, image, PDF) produced in an earlier session.
---

# Replaying past sessions from OpenViking

All tools below are the OpenViking MCP tools (`find`, `search`, `tree`, `list`,
`read`, `grep`, `glob`). `viking://~` is the current user's root.
Climb the ladder one rung at a time and stop as soon as the question is
answered — every rung costs more context than the one before.

## Session layout

```
viking://~/sessions/<sid>/                 sid prefix: cc- Claude Code, cx- Codex, pi- pi
  messages.jsonl                           live (not yet archived) messages
  history/archive_NNN/                     one archive per commit, oldest = 001
    .abstract.md                           L0: "<title> — <state at that point>"
    .overview.md                           L1: working memory (goal, state, facts, files, errors, open issues)
    messages.jsonl                         L2: raw messages, one JSON per line
    memory_diff.json                       which long-term memories this archive created/changed
  tool-results/<id>/output.txt             L3: full tool output when a message only holds a stub
```

Subagents are separate sessions: Claude Code `cc-<parent>__subagent-<agent>`;
Codex `cx-<child>` linked from the parent by a `[Subagent session] cx-…` message.

## 1. Locate (cheap)

- Fuzzy topic: `find` with the user's words (`limit` 5–10). Hits are long-term
  memories with an `abstract`. For change over time prefer `events`, which are
  dated (`memories/events/YYYY/MM/DD/…`).
- Exact wording, IDs, paths, error strings: `grep` with
  `uri: "viking://~/sessions"` and the literal pattern. Narrow `uri` to one
  session when you can; a whole-tree grep takes several seconds.

## 2. Memory → source session (provenance)

- `events` carry `source_archive_uri` in their metadata: `read` the memory and
  take it from the trailing metadata comment.
- Any other memory: `grep` its exact URI (or file name) under
  `viking://~/sessions` — the hit is `…/history/archive_NNN/memory_diff.json`,
  i.e. the archive that wrote it. Several hits = the memory was revised in
  several sessions; that list is its history.

## 3. Coarse replay — L0 / L1

- Timeline of a session: `tree` on `viking://~/sessions/<sid>/history` with
  `include_abstract: true`, `level_limit: 1`. Reading the abstracts in order
  shows how the work evolved.
- Detail for one point in time: `read` that archive's `.overview.md`.
- Working Memory is off since 2026-10-09 (server v0.5.0 default): newer archives have
  an empty `.abstract.md` / `.overview.md`. For those, go straight to L2, or use the
  long-term memories they produced (step 2) as the summary.

## 4. Fine replay — L2 raw

- `read` `…/archive_NNN/messages.jsonl` with `offset`/`limit` (lines). Page
  through; do not read the whole file unless the user asks for the transcript.
- A `grep` hit already gives the archive and line — read a window around it.
- Tool calls are `parts` with `type: "tool"`; if `tool_output_ref` is set the
  output was externalized: `read` `<tool_output_ref>/output.txt`.

## 5. Produced files and images — L3

Artifact capture adds link messages to the session:

- `[Artifact snapshot] <path>` — a file the agent wrote or mentioned. For text
  files the message's `artifact_snapshot` tool part holds the exact file
  content (follow `tool_output_ref` if it is a stub). Metadata JSON has
  `sha256`, `resource_uri` (parsed, searchable copy) and, for binary documents,
  `original_uri` (byte-exact copy under `.originals/`).
- `[Image attachment]` — `read` the `resource_uri` directory; the original image
  file inside is returned as an image.
- All artifacts of a session: `list` `viking://resources/agent-artifacts/<harness>/<sid>`
  (harness: `claude-code`, `codex`, `pi`).

## Answering

- Quote original text from L2/L3 when the user wants "what exactly was said/done".
- When facts changed over time, say so: give the latest state and when/where it
  changed, citing archive URIs (newest archive and newest dated event win).
- Report URIs you relied on so the user can open them.
