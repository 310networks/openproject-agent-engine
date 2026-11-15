// Run with: node --test tests/dashboard.test.cjs (no npm dependencies).
const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { test } = require("node:test");
const vm = require("node:vm");

const assets = new URL("../src/op_agent_engine/dashboard_assets/", `file://${__filename}`);
const context = vm.createContext({
  document: { getElementById: () => ({}), addEventListener: () => {} },
  setInterval: () => {},
  // Leave the initial poll pending: these tests don't contact the API.
  fetch: () => new Promise(() => {}),
});
vm.runInContext(readFileSync(new URL("app.js", assets), "utf8"), context);
const evaluate = (expression) => vm.runInContext(expression, context);

test("partial progress uses the new label everywhere humanize is used", () => {
  assert.equal(evaluate('humanize("progress_not_finished")'), "Still In Progress");
  assert.equal(evaluate('humanize("ready_for_review")'), "Ready For Review");
  assert.equal(evaluate('humanize(null)'), "unknown");
  assert.equal(evaluate('humanize("")'), "unknown");
});

test("finished partial progress gets its own tone without changing failures or live sessions", () => {
  assert.equal(evaluate('stateTone("succeeded", "progress_not_finished")'), "progress");
  assert.equal(evaluate('stateTone("running", "progress_not_finished")'), "running");
  assert.equal(evaluate('stateTone("failed", "nonzero_exit")'), "bad");
  assert.equal(evaluate('stateTone("succeeded", "no_pr")'), "bad");
  assert.equal(evaluate('stateTone("succeeded", "approved")'), "good");
  assert.equal(evaluate('stateTone("succeeded", "blocked")'), "warn");
  assert.equal(evaluate('stateTone("succeeded", "rejected")'), "declined");
  assert.equal(evaluate('stateTone("failed", null)'), "bad");
});

test("progress pills have a pink foreground, border and background", () => {
  const css = readFileSync(new URL("style.css", assets), "utf8");
  const rule = css.match(/\.state-pill\.progress\s*\{([^}]+)\}/)[1];
  assert.match(rule, /color: #f9a8d4;/);
  assert.match(rule, /border-color: rgba\(244, 114, 182, 0\.4\)/);
  assert.match(rule, /background: rgba\(244, 114, 182, 0\.09\)/);
});
