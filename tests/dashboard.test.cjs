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
    this.style = {};
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

function renderOutcomes(outcomes) {
  context.outcomeSnapshot = { counts: { outcomes } };
  evaluate("renderOutcomes(outcomeSnapshot)");
  return elements.get("outcomes");
}

test("outcome breakdown ignores historical and future aborted claims without mutating data", () => {
  const outcomes = {
    "claim aborted: work package 503 status changed before the claim": 100,
    "Claim Aborted: Work Package 504 status changed before the claim": 50,
    approved: 4,
    nonzero_exit: 2,
  };
  const before = JSON.stringify(outcomes);
  const list = renderOutcomes(outcomes);
  assert.equal(list.children.length, 2);
  assert.doesNotMatch(text(list), /Claim Aborted/i);
  assert.match(text(list), /Approved.*4/);
  assert.match(text(list), /Nonzero Exit.*2/);
  assert.equal(list.children[0].children[1].children[0].style.width, "100%");
  assert.equal(list.children[1].children[1].children[0].style.width, "50%");
  assert.equal(JSON.stringify(outcomes), before);

  outcomes["claim aborted: work package 505 no longer exists"] = 200;
  assert.equal(renderOutcomes(outcomes).children.length, 2);
});

test("empty and claim-only breakdowns show the empty state and clear previous rows", () => {
  renderOutcomes({ approved: 1 });
  for (const outcomes of [{ "claim aborted: work package 503 conflict": 3 }, {}, undefined]) {
    const list = renderOutcomes(outcomes);
    assert.equal(list.children.length, 1);
    assert.equal(list.children[0].className, "empty");
    assert.match(text(list), /No completed outcomes recorded yet/);
  }
});

test("unrelated failures and non-prefix claim text remain visible", () => {
  const list = renderOutcomes({
    "transition_aborted: status changed": 2,
    engagement_error: 1,
    "error: claim aborted: unexpected failure": 1,
    claim_aborted_other: 1,
  });
  assert.equal(list.children.length, 4);
  assert.match(text(list), /Transition Aborted/);
  assert.match(text(list), /Engagement Error/);
});

function renderKpis(current, extra = {}, failed = false) {
  context.kpiSnapshot = {
    counts: { total: 100, running: 2, completed: 30, declined: 4, blocked: 90, failed: 5 },
    current_work_packages: current,
    generated_at: new Date().toISOString(),
    ...extra,
  };
  context.kpiFailed = failed;
  evaluate("renderKpis(kpiSnapshot, kpiFailed)");
  return Object.fromEntries(elements.get("kpis").children.map((card) => [card.dataset.tone, card]));
}
const currentCounts = {
  status: "fresh", blocked: 3, on_hold: 0, age_seconds: 10, stale_after_seconds: 45,
  observed_at: "2026-11-16T17:00:00+00:00",
};
const kpiValue = (card) => card.children[1].textContent;

test("current Blocked and On Hold cards use live ticket counts including zero, not historical outcomes", () => {
  const cards = renderKpis(currentCounts);
  assert.equal(Object.keys(cards).length, 7);
  assert.equal(kpiValue(cards.blocked), "3");
  assert.equal(kpiValue(cards.on_hold), "0");
  assert.match(text(cards.blocked), /waiting on dependencies now/);
  assert.match(text(cards.on_hold), /on hold.*needs a human now/);
  assert.match(cards.blocked.title, /monitored projects.*Last observed/);
  assert.equal(cards.on_hold.children[0].children[0].attributes["aria-hidden"], "true");
  for (const [key, value] of Object.entries({ total: 100, running: 2, completed: 30, declined: 4, failed: 5 })) {
    assert.equal(kpiValue(cards[key]), String(value));
  }
  const updated = renderKpis({ ...currentCounts, blocked: 0, on_hold: 2 });
  assert.equal(kpiValue(updated.blocked), "0");
  assert.equal(kpiValue(updated.on_hold), "2");
});

test("missing, stale, unavailable and invalid counts never fall back to zero or history", () => {
  for (const current of [undefined, null, { ...currentCounts, status: "stale" },
    { ...currentCounts, status: "unavailable" }, { ...currentCounts, blocked: null, on_hold: null },
    { ...currentCounts, blocked: -1, on_hold: "2" }]) {
    const cards = renderKpis(current);
    for (const key of ["blocked", "on_hold"]) {
      assert.equal(kpiValue(cards[key]), "—");
      assert.match(text(cards[key]), /stale|unavailable/);
    }
  }
});

test("cached observations expire and failed updates hide current values until recovery", () => {
  const expired = renderKpis(currentCounts, { generated_at: new Date(Date.now() - 60000).toISOString() });
  assert.equal(kpiValue(expired.blocked), "—");
  assert.match(text(expired.on_hold), /stale/);
  const failed = renderKpis(currentCounts, {}, true);
  assert.equal(kpiValue(failed.blocked), "—");
  assert.match(text(failed.on_hold), /unavailable/);
  assert.equal(kpiValue(failed.total), "100");
  assert.equal(kpiValue(renderKpis(currentCounts).blocked), "3");
});

test("poll failures keep current counts unavailable on timer ticks and recover on success", async () => {
  let tick;
  let fail = false;
  const snapshot = {
    counts: { total: 100, blocked: 90 }, current_work_packages: currentCounts,
    generated_at: new Date().toISOString(),
  };
  const isolated = vm.createContext({
    document,
    setInterval: (callback) => { tick = callback; },
    setTimeout: () => 1,
    clearTimeout: () => {},
    fetch: () => new Promise(() => {}),
  });
  vm.runInContext(readFileSync(new URL("app.js", assets), "utf8"), isolated);
  isolated.snapshot = snapshot;
  isolated.fetch = async () => {
    if (fail) throw new Error("network offline");
    return { ok: true, json: async () => snapshot };
  };
  // Isolate poll/KPI behavior from unrelated panels, preserving production poll and tick.
  vm.runInContext(`render = (snapshot) => { lastSnapshot = snapshot; renderKpis(snapshot); };
    renderBanner = () => {}; renderAgents = () => {}; renderSessions = () => {};`, isolated);
  const blocked = () => elements.get("kpis").children.find((card) => card.dataset.tone === "blocked");
  await vm.runInContext("poll()", isolated);
  assert.equal(kpiValue(blocked()), "3");
  fail = true;
  await vm.runInContext("poll()", isolated);
  assert.equal(kpiValue(blocked()), "—");
  tick();
  assert.equal(kpiValue(blocked()), "—");
  assert.match(text(blocked()), /unavailable/);
  fail = false;
  await vm.runInContext("poll()", isolated);
  assert.equal(kpiValue(blocked()), "3");
});

test("KPI layout accommodates seven cards with tablet and mobile breakpoints", () => {
  const css = readFileSync(new URL("style.css", assets), "utf8");
  assert.match(css, /grid-template-columns: repeat\(7, minmax\(0, 1fr\)\)/);
  assert.match(css, /@media \(max-width: 1080px\)[\s\S]*?repeat\(3, minmax\(0, 1fr\)\)/);
  assert.match(css, /@media \(max-width: 720px\)[\s\S]*?repeat\(2, minmax\(0, 1fr\)\)/);
});

test("progress pills have a pink foreground, border and background", () => {
  const css = readFileSync(new URL("style.css", assets), "utf8");
  const rule = css.match(/\.state-pill\.progress\s*\{([^}]+)\}/)[1];
  assert.match(rule, /color: #f9a8d4;/);
  assert.match(rule, /border-color: rgba\(244, 114, 182, 0\.4\)/);
  assert.match(rule, /background: rgba\(244, 114, 182, 0\.09\)/);
});
