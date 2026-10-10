import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, readdirSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import {
  createStore, decodeDataUrl, extractMentionedPaths, readJsonl, scanClaudeCode, scanCodex, scanPi, stage,
} from "./core.mjs";
import { ovSessionFor } from "./hook.mjs";

const PNG_1PX = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==";

test("mentioned paths keep artifact extensions and strip trailing punctuation", () => {
  const text = "saved to /tmp/out/chart.png。 see ![x](/tmp/a b.svg) and ~/r/report.pdf, not /tmp/x.py";
  const paths = extractMentionedPaths(text);
  assert.ok(paths.includes("/tmp/out/chart.png"));
  assert.ok(paths.some((p) => p.endsWith("/r/report.pdf")));
  assert.ok(!paths.some((p) => p.endsWith(".py")));
});

test("claude code: pasted image, tool_result image, written files", () => {
  const entries = [
    { line: 0, value: { type: "user", uuid: "u1", cwd: "/w", message: { role: "user", content: [
      { type: "text", text: "look" },
      { type: "image", source: { type: "base64", media_type: "image/png", data: PNG_1PX } },
    ] } } },
    { line: 1, value: { type: "assistant", uuid: "a1", cwd: "/w", message: { role: "assistant", content: [
      { type: "tool_use", id: "t1", name: "Write", input: { file_path: "/w/index.html", content: "<html/>" } },
      { type: "tool_use", id: "t2", name: "Edit", input: { file_path: "src/app.ts" } },
      { type: "text", text: "Chart at /w/out/plot.png" },
    ] } } },
    { line: 2, value: { type: "user", uuid: "u2", message: { role: "user", content: [
      { type: "tool_result", tool_use_id: "t3", content: [
        { type: "image", source: { type: "base64", media_type: "image/png", data: PNG_1PX } },
      ] },
    ] } } },
  ];
  const scan = scanClaudeCode(entries);
  assert.equal(scan.images.length, 2);
  assert.deepEqual(scan.files.map((f) => f.path), ["/w/index.html", "/w/src/app.ts"]);
  assert.deepEqual(scan.mentioned, ["/w/out/plot.png"]);
});

test("codex: input_image data URL and apply_patch paths resolved against cwd", () => {
  const entries = [
    { line: 0, value: { type: "session_meta", payload: { id: "t", cwd: "/repo" } } },
    { line: 1, value: { type: "response_item", payload: { type: "message", role: "user", content: [
      { type: "input_image", image_url: `data:image/png;base64,${PNG_1PX}` },
    ] } } },
    { line: 2, value: { type: "response_item", payload: { type: "custom_tool_call", name: "apply_patch",
      input: "*** Begin Patch\n*** Add File: docs/a.md\n+x\n*** Update File: /abs/b.py\n*** End Patch" } } },
  ];
  const scan = scanCodex(entries);
  assert.equal(scan.images.length, 1);
  assert.deepEqual(scan.files.map((f) => f.path), ["/repo/docs/a.md", "/abs/b.py"]);
  assert.equal(decodeDataUrl(scan.images[0].dataUrl).ext, "png");
});

test("pi: write/edit tool calls and mentioned artifacts", () => {
  const branch = [
    { type: "message", message: { role: "assistant", content: [
      { type: "toolCall", name: "write", arguments: { path: "/p/app.py", content: "x" } },
      { type: "toolCall", name: "edit", arguments: { path: "rel/notes.md" } },
      { type: "toolCall", name: "bash", arguments: { command: "ls" } },
      { type: "text", text: "Plot saved to /p/out.png" },
    ] } },
  ];
  const scan = scanPi(branch, { cwd: "/w" });
  assert.deepEqual(scan.files.map((f) => f.path), ["/p/app.py", "/w/rel/notes.md"]);
  assert.deepEqual(scan.mentioned, ["/p/out.png"]);
});

test("pi image blocks are captured like Claude Code's", () => {
  const branch = [
    { type: "message", id: "e1", message: { role: "user", content: [
      { type: "text", text: "look" }, { type: "image", data: PNG_1PX, mimeType: "image/png" }] } },
    { type: "message", id: "e2", message: { role: "toolResult", content: [
      { type: "image", data: PNG_1PX, mimeType: "image/png" }] } },
  ];
  const scan = scanPi(branch, { cwd: "/w" });
  assert.deepEqual(scan.images.map((i) => [i.identity, i.role, i.mime]), [["e1:1", "user", "image/png"], ["e2:0", "user", "image/png"]]);
  const store = createStore({ harness: "pi", conn: { url: "http://x" }, rootDir: mkdtempSync(join(tmpdir(), "ac-")) });
  assert.equal(stage(store, "pi-s", scan), 2);
  assert.equal(stage(store, "pi-s", scan), 0);
});

test("session ids match the memory plugins", () => {
  assert.equal(ovSessionFor("claude-code", { session_id: "abc", hook_event_name: "Stop" }), "cc-abc");
  assert.equal(
    ovSessionFor("claude-code", { session_id: "abc", agent_id: "x:y", hook_event_name: "SubagentStop" }),
    "cc-abc__subagent-x-y",
  );
  assert.equal(ovSessionFor("codex", { session_id: "01a1-x", hook_event_name: "Stop" }), "cx-01a1-x");
});

test("staging is durable and idempotent; changed content is a new version", () => {
  const root = mkdtempSync(join(tmpdir(), "ac-"));
  const file = join(root, "page.html");
  writeFileSync(file, "<p>v1</p>");
  const store = createStore({ harness: "claude-code", conn: { url: "http://x", apiKey: "k" }, rootDir: join(root, "store") });
  const scan = { images: [{ role: "user", mime: "image/png", data: PNG_1PX, identity: "i", origin: { label: "img" } }],
    files: [{ path: file, origin: { tool: "Write" } }], mentioned: [file] };
  assert.equal(stage(store, "cc-s", scan), 2);
  assert.equal(stage(store, "cc-s", scan), 0);
  writeFileSync(file, "<p>v2</p>");
  assert.equal(stage(store, "cc-s", scan), 1);
  const records = readdirSync(join(store.directory, "records")).map((n) => JSON.parse(readFileSync(join(store.directory, "records", n), "utf8")));
  assert.equal(records.filter((r) => r.kind === "file").length, 2);
  assert.ok(records.every((r) => r.status === "pending"));
});

test("readJsonl cursor rescans a rewritten (shorter) file", () => {
  const root = mkdtempSync(join(tmpdir(), "ac-"));
  const path = join(root, "t.jsonl");
  writeFileSync(path, '{"a":1}\n{"a":2}\n');
  assert.equal(readJsonl(path, 0).entries.length, 2);
  assert.equal(readJsonl(path, 2).entries.length, 0);
  writeFileSync(path, '{"a":3}\n');
  assert.equal(readJsonl(path, 2).entries.length, 1);
});

test("an original over the batch-write limit is skipped, not retried forever", async () => {
  const root = mkdtempSync(join(tmpdir(), "ac-"));
  const big = join(root, "big.pdf");
  writeFileSync(big, Buffer.concat([Buffer.from("%PDF-1.7\n"), Buffer.alloc(9 * 1024 * 1024, 1)]));
  // a pi record drained by the Codex hook's store
  const piStore = createStore({ harness: "pi", conn: { url: "http://x" }, rootDir: root });
  assert.ok(piStore.addFile({ ovSessionId: "pi-s", role: "assistant", path: big, origin: {} }));
  const calls = [];
  const client = {
    uploadResource: async (_blob, { to }) => { calls.push(["upload", to]); return to; },
    writeOriginal: async (uri) => { calls.push(["original", uri]); return uri; },
    addMessage: async (sid, _role, header) => { calls.push(["message", sid, header]); },
  };
  const codexStore = createStore({ harness: "codex", conn: { url: "http://x" }, rootDir: root });
  assert.deepEqual(await codexStore.drain(client), { linked: 1, failed: 0 });
  assert.match(calls[0][1], /agent-artifacts\/pi\/pi-s\//);
  assert.ok(!calls.some((c) => c[0] === "original"));
  assert.match(calls.find((c) => c[0] === "message")[2], /original_skipped/);
});
