"use strict";

// The dashboard is deliberately dependency-free: one JSON endpoint, plain DOM.
const REFRESH_FALLBACK_MS = 3000;
const KPI_ICONS = {
  total: "Σ",
  running: "▶",
  completed: "✓",
  declined: "✕",
  blocked: "!",
  failed: "×",
};
const KPI_HINTS = {
  total: "recorded sessions",
  running: "in flight now",
  completed: "delivered work",
  declined: "reviewer said no",
  blocked: "needs a human",
  failed: "crashed or timed out",
};

let refreshMs = REFRESH_FALLBACK_MS;
let timer = null;
let lastSnapshot = null;

const els = {
  badge: document.getElementById("engine-badge"),
  status: document.getElementById("engine-status"),
  detail: document.getElementById("engine-detail"),
  banner: document.getElementById("banner"),
  kpis: document.getElementById("kpis"),
  agents: document.getElementById("agents"),
  agentSummary: document.getElementById("agent-summary"),
  active: document.getElementById("active-sessions"),
  activeCount: document.getElementById("active-count"),
  recent: document.getElementById("recent-sessions"),
  recentCount: document.getElementById("recent-count"),
  outcomes: document.getElementById("outcomes"),
  footerMeta: document.getElementById("footer-meta"),
  updated: document.getElementById("updated"),
};

function make(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
}

function humanize(value) {
  if (!value) return "unknown";
  return String(value).replaceAll("_", " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

function formatDuration(seconds) {
  if (seconds === null || seconds === undefined) return "—";
  const total = Math.max(0, Math.round(seconds));
  if (total < 60) return `${total}s`;
  const minutes = Math.floor(total / 60);
  const secs = total % 60;
  if (minutes < 60) return `${minutes}m ${String(secs).padStart(2, "0")}s`;
  const hours = Math.floor(minutes / 60);
  const mins = minutes % 60;
  if (hours < 24) return `${hours}h ${String(mins).padStart(2, "0")}m`;
  const days = Math.floor(hours / 24);
  return `${days}d ${hours % 24}h`;
}

function relativeTime(iso) {
  if (!iso) return "never";
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "unknown";
  const delta = Math.max(0, (Date.now() - then) / 1000);
  if (delta < 5) return "just now";
  if (delta < 60) return `${Math.floor(delta)}s ago`;
  if (delta < 3600) return `${Math.floor(delta / 60)}m ago`;
  if (delta < 86400) return `${Math.floor(delta / 3600)}h ago`;
  return `${Math.floor(delta / 86400)}d ago`;
}

function shortId(value) {
  return value ? String(value).replace(/^#/, "") : "";
}

function stateTone(state, outcome) {
  if (state === "running") return "running";
  if (outcome === "rejected") return "declined";
  if (outcome === "blocked" || outcome === "rejection_cap_reached" || outcome === "no_pr_for_branch") {
    return "warn";
  }
  if (outcome === "approved" || outcome === "ready_for_review" || outcome === "ready_for_review_existing_pr") {
    return "good";
  }
  return "bad";
}

function makeLink(text, href, className) {
  if (!href) return make("span", className || "", text);
  const link = make("a", className || "", text);
  link.href = href;
  link.target = "_blank";
  link.rel = "noopener noreferrer";
  return link;
}

function renderBanner(snapshot, error) {
  clear(els.banner);
  els.banner.classList.add("hidden");
  els.banner.classList.remove("error");
  if (error) {
    els.banner.textContent = `Dashboard update failed: ${error}. Showing the last snapshot.`;
    els.banner.classList.add("error");
    els.banner.classList.remove("hidden");
    return;
  }
  if (!snapshot) return;
  if (snapshot.engine.paused) {
    els.banner.textContent =
      "The engine is paused. New tickets are not claimed; running sessions finish normally. " +
      "Remove DATA_DIR/PAUSE or set ENGINE_PAUSED=false to resume.";
    els.banner.classList.remove("hidden");
  } else if (snapshot.engine.status === "stale") {
    els.banner.textContent =
      `The heartbeat is ${formatDuration(snapshot.engine.heartbeat_age_seconds)} old. ` +
      "The engine process is stopped or stuck — check journalctl -u op-agent-engine.";
    els.banner.classList.remove("hidden");
  } else if (snapshot.engine.status === "offline") {
    els.banner.textContent =
      "No heartbeat found. Start the engine with systemctl start op-agent-engine.";
    els.banner.classList.remove("hidden");
  }
}

function renderEngine(snapshot) {
  const engine = snapshot.engine;
  const labels = {
    active: "Engine active",
    paused: "Engine paused",
    stale: "Engine not polling",
    offline: "Engine offline",
  };
  els.badge.className = `engine-badge ${engine.status}`;
  els.status.textContent = labels[engine.status] || humanize(engine.status);
  if (engine.heartbeat_age_seconds !== null && engine.heartbeat_age_seconds !== undefined) {
    const poll = engine.poll_interval_seconds ? ` · every ${engine.poll_interval_seconds}s` : "";
    els.detail.textContent =
      `last poll ${formatDuration(engine.heartbeat_age_seconds)} ago` +
      ` · ${engine.active_runs}/${engine.max_concurrent_runs} slots in use` + poll;
  } else {
    els.detail.textContent = "no heartbeat file — the engine has not polled yet";
  }
  els.badge.title = engine.heartbeat || "no heartbeat";
}

function renderKpis(snapshot) {
  const counts = snapshot.counts;
  const order = ["total", "running", "completed", "declined", "blocked", "failed"];
  clear(els.kpis);
  for (const key of order) {
    const card = make("div", "kpi");
    card.dataset.tone = key;
    const label = make("div", "label");
    label.append(make("span", "", KPI_ICONS[key]), make("span", "", key));
    card.append(label);
    card.append(make("div", "value", counts[key] ?? 0));
    card.append(make("div", "hint", KPI_HINTS[key]));
    els.kpis.append(card);
  }
}

function sessionLine(session, { active = false } = {}) {
  if (!session) return null;
  const bits = [];
  bits.push(`run #${session.id}`);
  bits.push(session.display_id || `WP#${session.work_package_id}`);
  bits.push(session.kind);
  if (active) {
    bits.push(`${formatDuration(session.duration_seconds)} elapsed`);
  } else {
    bits.push(humanize(session.outcome || session.state));
    bits.push(formatDuration(session.duration_seconds));
  }
  return bits.join(" · ");
}

function renderAgents(snapshot) {
  clear(els.agents);
  const agents = snapshot.agents || [];
  const running = agents.filter((agent) => agent.status === "running").length;
  els.agentSummary.textContent = `${agents.length} configured · ${running} running`;

  if (!agents.length) {
    els.agents.append(make("div", "empty", "No agents configured in workflow.yaml."));
    return;
  }

  for (const agent of agents) {
    const card = make("article", `agent ${agent.status === "running" ? "running" : ""}`);
    const top = make("div", "agent-top");
    const nameBlock = make("div", "agent-name");
    nameBlock.append(make("strong", "", agent.name));
    nameBlock.append(make("span", "agent-key", agent.key));
    const state = make("div", "agent-state");
    state.append(make("span", "dot"));
    state.append(make("span", "", agent.status === "running" ? "running" : "idle"));
    top.append(nameBlock, state);

    const badges = make("div", "badges");
    badges.append(make("span", `pill ${agent.kind}`, agent.kind));
    badges.append(make("span", "pill runner", agent.runner));
    if (agent.model) badges.append(make("span", "pill", agent.model));
    for (const project of agent.projects || []) {
      badges.append(make("span", "pill", project));
    }

    const meta = make("div", "agent-meta");
    meta.append(document.createTextNode("login "));
    meta.append(make("span", "mono", agent.op_login));

    const current = agent.current_session;
    const last = agent.last_session;
    const line = make("div", current ? "agent-current" : "agent-last");
    const text = sessionLine(current, { active: true }) || (last ? `last: ${sessionLine(last)}` : "no sessions yet");
    line.append(document.createTextNode(text));

    const counts = make("div", "agent-counts");
    for (const key of ["total", "completed", "declined", "failed"]) {
      const cell = make("div");
      cell.append(make("b", "", agent.counts[key] ?? 0), document.createTextNode(` ${key}`));
      counts.append(cell);
    }

    card.append(top, badges, meta, line, counts);
    els.agents.append(card);
  }
}

function ticketCell(session) {
  const cell = make("td", "ticket");
  const id = makeLink(session.display_id || `WP#${session.work_package_id}`, session.work_package_url, "id");
  cell.append(id);
  if (session.subject) cell.append(make("div", "subject", session.subject));
  if (session.project) cell.append(make("div", "project", session.project));
  return cell;
}

function sessionTable(sessions, { active = false } = {}) {
  if (!sessions.length) {
    return make(
      "div",
      "empty",
      active ? "Nothing is running right now." : "No finished sessions yet."
    );
  }
  const table = make("table");
  const head = make("thead");
  const headRow = make("tr");
  for (const label of ["Session", "Ticket", "Agent", "Kind", active ? "Started" : "Outcome", "Duration", active ? "State" : "Finished", "PR"]) {
    headRow.append(make("th", "", label));
  }
  head.append(headRow);
  table.append(head);

  const body = make("tbody");
  for (const session of sessions) {
    const row = make("tr");
    const idCell = make("td", "session-id");
    idCell.append(make("span", "", `#${session.id}`));
    if (session.branch) idCell.append(make("span", "branch", session.branch));
    row.append(idCell);
    row.append(ticketCell(session));
    row.append(make("td", "", session.agent_name || session.agent));

    const kind = make("td");
    kind.append(make("span", "small", session.kind));
    row.append(kind);

    if (active) {
      row.append(make("td", "nowrap small", relativeTime(session.started_at)));
    } else {
      const tone = stateTone(session.state, session.outcome);
      const outcome = make("td");
      const pill = make("span", `state-pill ${tone}`, humanize(session.outcome || session.state));
      pill.title = session.result_line || "";
      outcome.append(pill);
      row.append(outcome);
    }

    row.append(make("td", "num", formatDuration(session.duration_seconds)));

    if (active) {
      const state = make("td");
      state.append(make("span", "state-pill running", "running"));
      row.append(state);
    } else {
      const finished = make("td", "nowrap small", relativeTime(session.finished_at));
      finished.title = session.finished_at || "";
      row.append(finished);
    }

    const pr = make("td");
    if (session.pr_url) {
      pr.append(makeLink("↗ PR", session.pr_url, "small"));
    } else {
      pr.append(make("span", "small", "—"));
    }
    row.append(pr);
    body.append(row);
  }
  table.append(body);
  return table;
}

function renderSessions(snapshot) {
  const active = snapshot.active_sessions || [];
  const recent = snapshot.recent_sessions || [];
  els.activeCount.textContent = `${active.length} running`;
  els.recentCount.textContent = `${recent.length} shown`;
  clear(els.active);
  els.active.append(sessionTable(active, { active: true }));
  clear(els.recent);
  els.recent.append(sessionTable(recent));
}

function renderOutcomes(snapshot) {
  const outcomes = snapshot.counts.outcomes || {};
  const entries = Object.entries(outcomes);
  clear(els.outcomes);
  if (!entries.length) {
    els.outcomes.append(make("div", "empty", "No completed outcomes recorded yet."));
    return;
  }
  const max = Math.max(...entries.map(([, count]) => count), 1);
  for (const [name, count] of entries) {
    const row = make("div", "outcome-row");
    row.append(make("div", "outcome-name", humanize(name)));
    const bar = make("div", "outcome-bar");
    const fill = make("span");
    fill.style.width = `${Math.max(3, (count / max) * 100)}%`;
    bar.append(fill);
    row.append(bar);
    row.append(make("div", "outcome-count", count));
    els.outcomes.append(row);
  }
}

function render(snapshot) {
  lastSnapshot = snapshot;
  refreshMs = Math.max(1, Number(snapshot.refresh_seconds || 3)) * 1000;
  renderEngine(snapshot);
  renderBanner(snapshot);
  renderKpis(snapshot);
  renderAgents(snapshot);
  renderSessions(snapshot);
  renderOutcomes(snapshot);
  els.footerMeta.textContent = `${snapshot.data_dir} · v${snapshot.version}`;
  els.updated.textContent = `updated ${relativeTime(snapshot.generated_at)} · refreshing every ${refreshMs / 1000}s`;
}

async function poll() {
  if (timer) {
    clearTimeout(timer);
    timer = null;
  }
  try {
    const response = await fetch("/api/status", { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const snapshot = await response.json();
    render(snapshot);
  } catch (error) {
    if (lastSnapshot) render(lastSnapshot);
    renderBanner(lastSnapshot, error instanceof Error ? error.message : String(error));
  } finally {
    timer = setTimeout(poll, refreshMs);
  }
}

// Refresh the relative timestamps even when the server data is unchanged.
setInterval(() => {
  if (!lastSnapshot) return;
  renderAgents(lastSnapshot);
  renderSessions(lastSnapshot);
  els.updated.textContent =
    `updated ${relativeTime(lastSnapshot.generated_at)} · refreshing every ${refreshMs / 1000}s`;
}, 1000);

document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible") poll();
});

poll();
