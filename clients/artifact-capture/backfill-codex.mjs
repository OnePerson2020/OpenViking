#!/usr/bin/env node
// One-off backfill: Codex rollouts whose cx-<thread> session never reached
// OpenViking (missed Stop captures, and subagent threads the plugin never
// captured). Each rollout goes through the installed plugin's own
// auto-capture, so ids and processing match live capture; child threads are
// linked from their parent session, and artifacts are staged as in hook.mjs.
//
// usage: node backfill-codex.mjs <missing.json>   ([[kind, threadId, parentId, rolloutPath], ...])
import { spawnSync } from "node:child_process";
import { existsSync, readFileSync, readdirSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
import { createClient, createStore, loadConnection, readJsonl, scanCodex, stage } from "./core.mjs";

const rows = JSON.parse(readFileSync(process.argv[2], "utf8"));
const conn = loadConnection();
const client = createClient(conn);
const store = createStore({ harness: "codex", conn });
const safeId = (v) => String(v || "unknown").replace(/[^A-Za-z0-9._-]/g, "_");

const root = join(homedir(), ".codex", "plugins", "cache", "openviking", "openviking-memory");
const version = readdirSync(root).filter((v) => /^\d+\.\d+\.\d+/.test(v))
  .sort((a, b) => a.localeCompare(b, undefined, { numeric: true })).pop();
const pluginDir = join(root, version);
if (!existsSync(join(pluginDir, "scripts", "auto-capture.mjs"))) throw new Error("codex plugin missing");

async function sessionExists(id) {
  const res = await fetch(`${conn.url}/api/v1/sessions/${encodeURIComponent(id)}`, {
    headers: { Authorization: `Bearer ${conn.apiKey}` },
  });
  return res.ok;
}

for (const [kind, threadId, parentId, rollout] of rows) {
  const ovSessionId = `cx-${safeId(threadId)}`;
  if (await sessionExists(ovSessionId)) { console.log("exists", ovSessionId); continue; }
  const cwd = JSON.parse(readFileSync(rollout, "utf8").split("\n", 1)[0]).payload?.cwd || homedir();
  const run = spawnSync(process.execPath, [join(pluginDir, "scripts", "auto-capture.mjs")], {
    cwd: existsSync(cwd) ? cwd : homedir(),
    // Synchronous: the plugin may otherwise detach and this loop would race it.
    env: { ...process.env, PLUGIN_ROOT: pluginDir, CLAUDE_PLUGIN_ROOT: pluginDir, OPENVIKING_ASYNC_WRITE: "0" },
    input: JSON.stringify({ session_id: threadId, transcript_path: rollout, cwd, hook_event_name: "Stop" }),
    encoding: "utf8",
    timeout: 300_000,
  });
  // The plugin may still hand the write to a detached worker; wait for it.
  let captured = false;
  for (let i = 0; i < 30 && !captured; i++) {
    captured = await sessionExists(ovSessionId);
    if (!captured) await new Promise((r) => setTimeout(r, 2000));
  }
  console.log(kind, ovSessionId, `rc=${run.status}`, captured ? "captured" : "NOT CAPTURED",
    (run.stdout || "").trim().slice(0, 160));
  if (captured && kind !== "main" && parentId) {
    try {
      await client.addMessage(`cx-${safeId(parentId)}`, "assistant",
        `[Subagent session] ${ovSessionId}\n${JSON.stringify({ child_session: ovSessionId, child_thread_id: threadId, kind, rollout, backfilled: true })}`);
    } catch (error) { console.log("  parent link failed:", error.message); }
  }
  const { entries } = readJsonl(rollout, 0);
  const added = stage(store, ovSessionId, scanCodex(entries, { cwd }));
  if (added) console.log("  staged artifacts:", added);
}
console.log("drain", await store.drain(client, (m) => console.log("  ", m)));
