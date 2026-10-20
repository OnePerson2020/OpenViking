import { test } from "node:test";
import assert from "node:assert/strict";
import { decide, SKIP_BELOW, PLANNER_FROM } from "./hook.mjs";

test("skips what needs no memory, plans only context-dependent prompts that need it", () => {
  assert.equal(decide(0.05, 0.95), "skip");                  // 继续
  assert.equal(decide(PLANNER_FROM, 0.9), "full");          // 目前进度如何,请继续完成
  assert.equal(decide(0.9, 0.2), "lite");                   // self-contained: the prompt is the query
  assert.equal(decide(PLANNER_FROM - 0.01, 0.9), "lite");   // 提交一下: in-session instruction
  assert.equal(decide(SKIP_BELOW, 0.1), "lite");
});

test("a failed bjev call still recalls, without the planner", () => {
  assert.equal(decide(undefined, undefined), "lite");
});
