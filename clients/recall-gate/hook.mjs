#!/usr/bin/env node
// Claude Code UserPromptSubmit gate in front of the OpenViking plugin's auto-recall.
//
// The plugin recalls on every prompt. With `plugin.claude_code.autoRecall: false` in
// ovcli.conf its own hook is a no-op; this one drops what the plugin's recallQueryFilters
// drop, asks bjev (LocalBridge /v1/systemone) two questions in one call, and runs the
// plugin's own auto-recall.mjs at the matching depth via its env overrides:
//   need < SKIP_BELOW                         -> no recall
//   depends on earlier turns, need >= PLANNER_FROM -> recall with the query-expansion
//                                                planner, which reads the session (~4 s more)
//   otherwise, or bjev failed                 -> recall on the prompt as written (~1-2 s)
// The planner only pays off for prompts that cannot be read without the conversation
// ("目前进度如何,请继续完成"); for self-contained ones it returned the same items.
// Thresholds come from 2026-10-17 evals (58 prompts; no true-need prompt scored need < 0.19;
// context AUC 0.90); decisions go to ~/.openviking/logs/recall-gate.log for re-tuning.

import { spawn } from "node:child_process";
import { appendFileSync, readFileSync, realpathSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

export const SKIP_BELOW = 0.15;
export const PLANNER_FROM = 0.4;
const BJEV_URL = "http://127.0.0.1:8321/v1/systemone";
const BJEV_TIMEOUT_MS = 1000;
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
  ctx: {
    type: "choice",
    instructions: "不知道之前的对话、只看这一句，能看懂用户具体指的是什么事吗？",
    criteria: {
      Y: "看不懂：用了这、那、上面、继续、进度、这部分之类的指代，具体指什么要看之前的对话",
      N: "看得懂：说的对象在这句话里写清楚了，或者是通用问题",
    },
  },
};

// need / ctx: bjev's probability that the prompt needs memory / depends on earlier turns.
export function decide(need, ctx) {
  if (need == null) return "lite";
  if (need < SKIP_BELOW) return "skip";
  return ctx >= 0.5 && need >= PLANNER_FROM ? "full" : "lite";
}

export async function score(prompt) {
  const res = await fetch(BJEV_URL, {
    method: "POST",
    headers: { "content-type": "application/json" },
    signal: AbortSignal.timeout(BJEV_TIMEOUT_MS),
    body: JSON.stringify({ model: "bjev", state: `用户对编程助手说：「${prompt.slice(0, 2000)}」`, questions: QUESTIONS }),
  });
  if (!res.ok) throw new Error(`bjev HTTP ${res.status}`);
  const answers = (await res.json())?.answers;
  const need = answers?.need?.probabilities?.Y, ctx = answers?.ctx?.probabilities?.Y;
  if (typeof need !== "number" || typeof ctx !== "number") throw new Error("bjev: missing probabilities");
  return { need, ctx };
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
function runPluginRecall(raw, tier) {
  const env = { ...process.env, OPENVIKING_AUTO_RECALL: "1" };
  if (tier === "lite") env.OPENVIKING_RECALL_QUERY_EXPANSION = "off";
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
  if (!filtered) tier = decide(scores.need, scores.ctx);
  const gateMs = Date.now() - t0;

  let out = JSON.stringify({ decision: "approve" }) + "\n";
  if (tier !== "skip") {
    try { out = (await runPluginRecall(raw, tier)) || out; } catch (e) { err = String(e?.message || e).slice(0, 120); }
  }
  process.stdout.write(out);
  try {
    appendFileSync(LOG, JSON.stringify({
      ts: new Date().toISOString(), need: scores.need, ctx: scores.ctx, tier, filtered, gate_ms: gateMs,
      total_ms: Date.now() - t0, injected: out.includes("additionalContext"), err, prompt: prompt.slice(0, 80),
    }) + "\n");
  } catch {}
}

// realpath: the hook is run through the agent-integrations symlink
if (realpathSync(process.argv[1]) === fileURLToPath(import.meta.url)) main();
