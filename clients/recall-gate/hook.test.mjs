import { test } from "node:test";
import assert from "node:assert/strict";
import { decide, SKIP_BELOW } from "./hook.mjs";

test("skips what needs no memory, recalls with the planner otherwise", () => {
  assert.equal(decide(0.05), "skip");                 // 继续
  assert.equal(decide(SKIP_BELOW), "full");
  assert.equal(decide(0.9), "full");
});

test("a failed bjev call still recalls", () => {
  assert.equal(decide(undefined), "full");
});
