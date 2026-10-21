#!/usr/bin/env node
// Claude Code UserPromptSubmit gate in front of the OpenViking plugin's auto-recall.
//
// The plugin recalls on every prompt. With `plugin.claude_code.autoRecall: false` in
// ovcli.conf its own hook is a no-op; this one drops what the plugin's recallQueryFilters
// drop, asks bjev (LocalBridge /v1/systemone) whether the prompt needs memory, and only skips:
//   need < SKIP_BELOW                         -> no recall
//   otherwise, or bjev failed                 -> the plugin's auto-recall.mjs with the
//                                                query-expansion planner (~4-5 s)
// SKIP_BELOW comes from 2026-10-17 evals (58 prompts; no true-need prompt scored need < 0.19);
// decisions go to ~/.openviking/logs/recall-gate.log for re-tuning.

import { spawn } from "node:child_process";
import { appendFileSync, readFileSync, realpathSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

export const SKIP_BELOW = 0.15;
const BJEV_URL = "http://127.0.0.1:8321/v1/systemone";
const BJEV_TIMEOUT_MS = 1500;
const LOG = join(homedir(), ".openviking", "logs", "recall-gate.log");

export const QUESTIONS = {
  need: {
    type: "choice",
    instructions: "回答这句话之前，需要先翻查用户的历史记忆（过去的项目、任务、系统、文档、决定）吗？",
    criteria: {
      Y: "提到了具体的项目、任务、系统、表、文档或过去发生的事",
      N: "只是确认、继续、闲聊，或通用的编程/知识问题",
    },
  },
};

// need: bjev's probability that the prompt needs memory; missing (bjev failed) recalls.
export const decide = (need) => (need < SKIP_BELOW ? "skip" : "full");

export async function score(prompt) {
  const res = await fetch(BJEV_URL, {
    method: "POST",
    headers: { "content-type": "application/json" },
    signal: AbortSignal.timeout(BJEV_TIMEOUT_MS),
    body: JSON.stringify({ model: "bjev", state: `用户对编程助手说：「${prompt.slice(0, 2000)}」`, questions: QUESTIONS }),
  });
  if (!res.ok) throw new Error(`bjev HTTP ${res.status}`);
  const answers = (await res.json())?.answers;
  const need = answers?.need?.probabilities?.Y;
  if (typeof need !== "number") throw new Error("bjev: missing probability");
  return { need };
}

function pluginScripts() {
  const installed = JSON.parse(readFileSync(join(homedir(), ".claude", "plugins", "installed_plugins.json"), "utf8"));
  const root = installed.plugins?.["openviking-memory@openviking"]?.[0]?.installPath;
  if (!root) throw new Error("openviking-memory plugin not installed");
  return join(root, "scripts");
}

// The plugin's own recallQueryFilters (ovcli.conf), applied here too so a prompt they drop
// never costs a bjev call; the plugin hook re-applies them to the same stdin.
async function applyRecallFilters(prompt, cwd) {
  const scripts = pluginScripts();
  const { loadConfig } = await import(pathToFileURL(join(scripts, "config.mjs")));
  const { compileInputFilters, applyInputFilters } = await import(pathToFileURL(join(scripts, "shared", "input-filters.mjs")));
  const { rules } = compileInputFilters(loadConfig(cwd).recallQueryFilters);
  return applyInputFilters(prompt, rules, { role: "user" });
}

// Run the plugin hook with the same stdin; its stdout is the hook's answer.
function runPluginRecall(raw) {
  const env = { ...process.env, OPENVIKING_AUTO_RECALL: "1" };
  return new Promise((resolve, reject) => {
    const child = spawn(process.execPath, [join(pluginScripts(), "auto-recall.mjs")], { env, stdio: ["pipe", "pipe", "inherit"] });
    let out = "";
    child.stdout.on("data", (d) => { out += d; });
    child.on("error", reject);
    child.on("close", () => resolve(out));
    child.stdin.end(raw);
  });
}

async function main() {
  const t0 = Date.now();
  let raw = "";
  for await (const chunk of process.stdin) raw += chunk;
  let input = {};
  try { input = JSON.parse(raw); } catch {}
  const prompt = String(input.prompt || "").trim();

  let scores = {}, err, tier = "skip", filtered = false;
  try {
    const verdict = await applyRecallFilters(prompt, input.cwd || process.cwd());
    filtered = verdict.dropped;
    if (!filtered) scores = await score(verdict.text);
  } catch (e) { err = String(e?.message || e).slice(0, 120); }
  if (!filtered) tier = decide(scores.need);
  const gateMs = Date.now() - t0;

  let out = JSON.stringify({ decision: "approve" }) + "\n";
  if (tier !== "skip") {
    try { out = (await runPluginRecall(raw)) || out; } catch (e) { err = String(e?.message || e).slice(0, 120); }
  }
  process.stdout.write(out);
  try {
    appendFileSync(LOG, JSON.stringify({
      ts: new Date().toISOString(), need: scores.need, tier, filtered, gate_ms: gateMs,
      total_ms: Date.now() - t0, injected: out.includes("additionalContext"), err, prompt: prompt.slice(0, 80),
    }) + "\n");
  } catch {}
}

// realpath: the hook is run through the agent-integrations symlink
if (realpathSync(process.argv[1]) === fileURLToPath(import.meta.url)) main();
