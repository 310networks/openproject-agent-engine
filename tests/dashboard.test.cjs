// Run with: node --test tests/dashboard.test.cjs (no npm dependencies).
const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { test } = require("node:test");
const vm = require("node:vm");

const assets = new URL("../src/op_agent_engine/dashboard_assets/", `file://${__filename}`);
// Minimal DOM double for the dependency-free renderer; no network or browser globals.
class Element {
  constructor(tagName) {
    this.tagName = tagName;
    this.children = [];
    this.dataset = {};
    this.attributes = {};
    this.listeners = {};
    this.textContent = "";
  }
  get firstChild() { return this.children[0]; }
  append(...nodes) { this.children.push(...nodes); }
  removeChild(node) { this.children.splice(this.children.indexOf(node), 1); }
  setAttribute(name, value) { this.attributes[name] = value; }
  addEventListener(name, callback) { this.listeners[name] = callback; }
  focus() { document.activeElement = this; }
  querySelectorAll(selector) {
    return this.children.flatMap((child) => [
      ...(child.className === selector.slice(1) ? [child] : []),
      ...child.querySelectorAll(selector),
    ]);
  }
}
const elements = new Map();
const document = {
  activeElement: null,
  getElementById: (id) => {
    if (!elements.has(id)) elements.set(id, new Element("div"));
    return elements.get(id);
  },
  createElement: (tag) => new Element(tag),
  addEventListener: () => {},
};
const context = vm.createContext({
  document,
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

function session(id, ticket, outcome = "waiting_ci", extra = {}) {
  return {
    id, work_package_id: ticket, display_id: `OP#${ticket}`,
    started_at: `2026-01-01T00:00:${String(id).padStart(2, "0")}Z`,
    finished_at: "2026-01-01T01:00:00Z", state: "succeeded", outcome,
    agent: "frontend", kind: "implement", duration_seconds: id,
    ...extra,
  };
}
function renderSessions(recent, active = []) {
  context.snapshot = { recent_sessions: recent, active_sessions: active };
  evaluate("renderSessions(snapshot)");
  return elements.get("recent-sessions").children[0];
}
function rows(table) { return table.children[1].children; }
function text(node) { return [node.textContent, ...node.children.map(text)].join(" "); }

test("groups checkpoints across agents by ticket, with newest run and deterministic ties", () => {
  context.sessions = [session(1, 501), session(3, 501, "approved", { agent: "reviewer" }),
    session(2, 502), session(4, 503, "blocked", { started_at: null }),
    session(5, 503, "nonzero_exit", { started_at: null })];
  const result = JSON.parse(evaluate("JSON.stringify(groupRecentSessions(sessions))"));
  assert.deepEqual(result.map((group) => group.map((run) => run.id)), [[3, 1], [2], [5, 4]]);
  assert.deepEqual(context.sessions.map((run) => run.id), [1, 3, 2, 4, 5]);
});

test("empty, single-session and legacy missing-ticket entries remain distinct", () => {
  assert.match(text(renderSessions([])), /No finished sessions yet/);
  const single = renderSessions([session(1, 501)]);
  assert.equal(rows(single).length, 1);
  assert.equal(single.querySelectorAll(".history-toggle").length, 0);
  context.sessions = [session(1, null), session(2, null)];
  assert.equal(evaluate("groupRecentSessions(sessions).length"), 2);
});

test("history is collapsed initially, toggles accessibly, and preserves session details as text", () => {
  const table = renderSessions([session(1, 501, "nonzero_exit", {
    subject: "<script>not markup</script>", pr_url: "https://github.com/example/repo/pull/1",
  }), session(2, 501, "ready_for_review")]);
  const [latest, history] = rows(table);
  assert.match(text(latest), /Ready For Review/);
  assert.doesNotMatch(text(latest), /Nonzero Exit/);
  assert.equal(history.hidden, true);
  const button = table.querySelectorAll(".history-toggle")[0];
  assert.equal(button.tagName, "button");
  assert.equal(button.type, "button");
  assert.equal(button.attributes["aria-label"], "Session history for OP#501");
  assert.equal(button.attributes["aria-controls"], history.id);
  assert.equal(button.attributes["aria-expanded"], "false");
  button.listeners.click();
  assert.equal(history.hidden, false);
  assert.equal(button.attributes["aria-expanded"], "true");
  assert.match(text(history), /Nonzero Exit/);
  assert.match(text(history), /<script>not markup<\/script>/);
  assert.match(text(history), /outside the snapshot limit/);
  button.listeners.click();
  assert.equal(history.hidden, true);
});

test("timer ticks, updated snapshots and stale-data renders preserve expansion and focus", () => {
  const first = [session(1, 601), session(2, 601)];
  const table = renderSessions(first);
  const button = table.querySelectorAll(".history-toggle")[0];
  button.listeners.click();
  button.focus();
  assert.equal(renderSessions(first), table);
  assert.equal(document.activeElement, button);
  const updated = renderSessions([...first, session(3, 601, "approved")]);
  const replacement = updated.querySelectorAll(".history-toggle")[0];
  assert.equal(rows(updated)[1].hidden, false);
  assert.equal(document.activeElement, replacement);
  // poll's failure path renders the same last snapshot; it must not close history.
  assert.equal(renderSessions([...first, session(3, 601, "approved")]), updated);
  replacement.listeners.click();
  assert.equal(rows(updated)[1].hidden, true);
  renderSessions([]);
  const returned = renderSessions(first);
  assert.equal(rows(returned)[1].hidden, true);
});

test("active sessions stay ungrouped and snapshot counts distinguish tickets from runs", () => {
  const active = [session(3, 701), session(4, 701)];
  const recent = renderSessions([session(1, 701), session(2, 701)], active);
  assert.equal(rows(recent).length, 2); // one ticket plus its hidden history row
  assert.equal(rows(elements.get("active-sessions").children[0]).length, 2);
  assert.equal(elements.get("active-count").textContent, "2 running");
  assert.equal(elements.get("recent-count").textContent, "1 tickets · 2 sessions in snapshot");
});

test("progress pills have a pink foreground, border and background", () => {
  const css = readFileSync(new URL("style.css", assets), "utf8");
  const rule = css.match(/\.state-pill\.progress\s*\{([^}]+)\}/)[1];
  assert.match(rule, /color: #f9a8d4;/);
  assert.match(rule, /border-color: rgba\(244, 114, 182, 0\.4\)/);
  assert.match(rule, /background: rgba\(244, 114, 182, 0\.09\)/);
});
