#!/usr/bin/env node
// Hook entry: `node hook.mjs claude-code|codex` on Stop and SubagentStop.
//
// Returns immediately; a detached worker stages artifacts locally and then
// uploads/links them. For Codex SubagentStop the worker also feeds the child
// rollout to the installed OpenViking Codex plugin's own auto-capture (the
// plugin has no subagent hook) and links the child session from the parent.
import { spawn } from "node:child_process";
import { appendFileSync, existsSync, mkdirSync, readdirSync, readFileSync, realpathSync, writeFileSync } from "node:fs";
import { homedir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import {
  createClient, createStore, loadConnection, readJsonl, scanClaudeCode, scanCodex, stage,
} from "./core.mjs";

const harness = process.argv[2];
const isWorker = process.argv[3] === "--worker";
const LOG = join(homedir(), ".openviking", "logs", "artifact-capture.log");

function log(message) {
  try {
    mkdirSync(dirname(LOG), { recursive: true });
    appendFileSync(LOG, `${new Date().toISOString()} [${harness}${isWorker ? "/worker" : ""}] ${message}\n`);
  } catch { /* logging is best effort */ }
}

const safeId = (value, replacement = "_") => String(value || "unknown").replace(/[^A-Za-z0-9._-]/g, replacement);

export function ovSessionFor(harnessName, input) {
  const sub = input.hook_event_name === "SubagentStop";
  if (harnessName === "claude-code") {
    if (!input.session_id) return null;
    const base = `cc-${input.session_id}`;
    if (!sub) return base;
    const suffix = `subagent:${input.agent_id || ""}`.replace(/:/g, "-").replace(/[^A-Za-z0-9._-]/g, "-");
    return `${base}__${suffix}`;
  }
  if (harnessName === "codex") {
    const id = sub ? codexThreadId(input.agent_transcript_path) : input.session_id;
    return id ? `cx-${safeId(id)}` : null;
  }
  return null;
}

function codexThreadId(rolloutPath) {
  try {
    const first = readFileSync(rolloutPath, "utf8").split("\n", 1)[0];
    const meta = JSON.parse(first);
    return meta?.type === "session_meta" ? meta.payload?.id : null;
  } catch { return null; }
}

function latestCodexPlugin() {
  const root = join(homedir(), ".codex", "plugins", "cache", "openviking", "openviking-memory");
  try {
    const versions = readdirSync(root).filter((v) => /^\d+\.\d+\.\d+/.test(v))
      .sort((a, b) => a.localeCompare(b, undefined, { numeric: true }));
    const dir = versions.length ? join(root, versions[versions.length - 1]) : null;
    return dir && existsSync(join(dir, "scripts", "auto-capture.mjs")) ? dir : null;
  } catch { return null; }
}

function runCodexChildCapture(input, childId) {
  const pluginDir = latestCodexPlugin();
  if (!pluginDir) return Promise.resolve("codex plugin not installed");
  const payload = JSON.stringify({
    session_id: childId, transcript_path: input.agent_transcript_path, cwd: input.cwd,
    hook_event_name: "Stop", stop_hook_active: false,
  });
  return new Promise((resolve) => {
    const child = spawn(process.execPath, [join(pluginDir, "scripts", "auto-capture.mjs")], {
      cwd: input.cwd || homedir(),
      env: { ...process.env, PLUGIN_ROOT: pluginDir, CLAUDE_PLUGIN_ROOT: pluginDir },
      stdio: ["pipe", "pipe", "pipe"],
    });
    let out = "";
    child.stdout.on("data", (d) => { out += d; });
    child.stderr.on("data", (d) => { out += d; });
    const timer = setTimeout(() => child.kill("SIGTERM"), 120_000);
    child.on("close", (code) => { clearTimeout(timer); resolve(`exit=${code} ${out.trim().slice(0, 300)}`); });
    child.stdin.end(payload);
  });
}

async function work(input) {
  const conn = loadConnection();
  const client = createClient(conn);
  const store = createStore({ harness, conn });
  const sub = input.hook_event_name === "SubagentStop";
  const transcript = sub ? input.agent_transcript_path : input.transcript_path;
  const ovSessionId = ovSessionFor(harness, input);
  if (!transcript || !ovSessionId || !existsSync(transcript)) {
    log(`skip event=${input.hook_event_name} transcript=${transcript ? "missing-file" : "none"} ov=${ovSessionId}`);
  } else {
    if (harness === "codex" && sub) {
      const childId = codexThreadId(transcript);
      log(`codex subagent ${childId}: ${await runCodexChildCapture(input, childId)}`);
      const marker = join(store.directory, "subagent-links", `${safeId(childId)}.json`);
      if (input.session_id && !existsSync(marker)) {
        try {
          await client.addMessage(`cx-${safeId(input.session_id)}`, "assistant",
            `[Subagent session] ${ovSessionId}\n${JSON.stringify({
              child_session: ovSessionId, child_thread_id: childId, agent_id: input.agent_id,
              agent_type: input.agent_type, rollout: transcript,
            })}`);
          mkdirSync(dirname(marker), { recursive: true, mode: 0o700 });
          writeFileSync(marker, JSON.stringify({ linkedAt: new Date().toISOString() }));
        } catch (error) { log(`subagent link failed: ${error.message}`); }
      }
    }
    const cursor = store.cursor(transcript);
    const { entries, total } = readJsonl(transcript, cursor.value);
    const scan = harness === "codex" ? scanCodex(entries, { cwd: input.cwd }) : scanClaudeCode(entries, { cwd: input.cwd });
    const added = stage(store, ovSessionId, scan, log);
    cursor.save(total);
    log(`staged ov=${ovSessionId} lines=${cursor.value}->${total} images=${scan.images.length} ` +
      `files=${scan.files.length} mentioned=${scan.mentioned.length} new=${added}`);
  }
  // Resources parse asynchronously (409 busy) and the last turn of a session
  // gets no later hook, so retry a few times inside this detached worker.
  for (let attempt = 0; attempt < 6; attempt++) {
    if (attempt) await new Promise((r) => setTimeout(r, 30_000));
    const result = await store.drain(client, log);
    if (!result.skipped && (result.linked || result.failed)) {
      log(`drain#${attempt} linked=${result.linked} failed=${result.failed}`);
    }
    if (result.skipped || !result.failed) break;
  }
}

async function readStdin() {
  const chunks = [];
  for await (const chunk of process.stdin) chunks.push(chunk);
  return Buffer.concat(chunks).toString("utf8");
}

async function main() {
  if (!["claude-code", "codex"].includes(harness)) {
    process.stderr.write("usage: hook.mjs claude-code|codex\n");
    process.exit(2);
  }
  if (process.env.OPENVIKING_ARTIFACT_CAPTURE === "0") return;
  if (isWorker) {
    const input = JSON.parse(process.env.OV_ARTIFACT_HOOK_INPUT || "{}");
    try { await work(input); } catch (error) { log(`worker error: ${error?.stack || error}`); }
    return;
  }
  const raw = await readStdin();
  let input;
  try { input = JSON.parse(raw || "{}"); } catch { log("invalid hook stdin"); return; }
  const child = spawn(process.execPath, [fileURLToPath(import.meta.url), harness, "--worker"], {
    detached: true, stdio: "ignore",
    env: { ...process.env, OV_ARTIFACT_HOOK_INPUT: JSON.stringify(input) },
  });
  child.unref();
}

// realpath: the hook is usually run through the ~/.openviking/agent-integrations symlink
if (process.argv[1] && fileURLToPath(import.meta.url) === realpathSync(process.argv[1])) {
  main().catch((error) => log(`hook error: ${error?.stack || error}`));
}
