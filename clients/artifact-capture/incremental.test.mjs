import assert from "node:assert/strict";
import { appendFileSync, mkdtempSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { test } from "node:test";
import { readJsonl, scanCodex } from "./core.mjs";

test("an incomplete final record is read after it is completed", () => {
  const file = join(mkdtempSync(join(tmpdir(), "capture-tail-")), "t.jsonl");
  writeFileSync(file, '{"a":1}\n{"a":');
  const first = readJsonl(file);
  appendFileSync(file, '2}\n');
  const second = readJsonl(file, first.total);
  assert.deepEqual(second.entries.map((e) => e.value), [{ a: 2 }]);
});

test("a complete final record without a newline is consumed once", () => {
  const file = join(mkdtempSync(join(tmpdir(), "capture-tail-")), "t.jsonl");
  writeFileSync(file, '{"a":1}');
  const first = readJsonl(file);
  assert.equal(first.entries.length, 1);
  appendFileSync(file, '\n{"a":2}\n');
  assert.deepEqual(readJsonl(file, first.total).entries.map((e) => e.value), [{ a: 2 }]);
});

test("completed Codex FileChange events capture wrapped edits and moves only", () => {
  const event = (status, changes) => ({ value: { type: "event_msg", payload: {
    type: "item_completed", item: { type: "FileChange", status, changes },
  } }, line: 1 });
  const scan = scanCodex([
    event("completed", { "one.py": { type: "add" }, "old.md": { type: "update", move_path: "new.md" }, "gone.py": { type: "delete" } }),
    event("failed", { "failed.py": { type: "add" } }),
    event("inProgress", { "running.py": { type: "add" } }),
  ], { cwd: "/project" });
  assert.deepEqual(scan.files.map((f) => f.path), ["/project/one.py", "/project/new.md"]);
});

test("namespaced apply_patch retains legacy capture support", () => {
  const scan = scanCodex([{ line: 0, value: { type: "response_item", payload: {
    type: "custom_tool_call", name: "functions.apply_patch",
    input: "*** Begin Patch\n*** Add File: file.py\n+x\n*** End Patch",
  } } }], { cwd: "/project" });
  assert.deepEqual(scan.files.map((f) => f.path), ["/project/file.py"]);
});
