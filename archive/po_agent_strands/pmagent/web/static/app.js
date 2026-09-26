// PM Agent web client — talks to /api/v1 and renders the thinking chain.
//
// Flow of one turn:
//   POST /conversations/{id}/turns          → 202 + Location (the turn)
//   GET  {events_url}   (SSE over fetch)     → turn.started, route.decided, reasoning.delta,
//                                              text.delta, tool.started/finished, message.completed,
//                                              approval.required | turn.completed | turn.failed
//   the stream closes when the turn rests; after an approval decision we reconnect
//   with Last-Event-ID and keep reading the same turn.
//
// Security: model text is Markdown rendered through DOMPurify (no images, styles,
// forms, frames); tool output and user text are only ever set as textContent.

const API = "/api/v1";
const $ = (sel) => document.querySelector(sel);

const state = {
  token: safeGet("pmagent.token"),
  conversation: null,         // conversation id
  turns: new Map(),           // turn id → view model
  selected: null,             // turn id shown in the chain
  busy: false,
};

// ---------------------------------------------------------------- storage (per tab)
function safeGet(key) { try { return sessionStorage.getItem(key); } catch { return null; } }
function safeSet(key, value) { try { value == null ? sessionStorage.removeItem(key) : sessionStorage.setItem(key, value); } catch { /* private mode */ } }

// ---------------------------------------------------------------- HTTP
async function api(path, { method = "GET", body, headers = {} } = {}) {
  const init = { method, headers: { Accept: "application/json", ...headers } };
  if (state.token) init.headers.Authorization = `Bearer ${state.token}`;
  if (body !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(body);
  }
  const response = await fetch(path, init);
  if (response.status === 401) { showTokenGate(); throw new ApiError({ title: "Sign-in required", status: 401 }); }
  if (response.status === 204) return null;
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new ApiError(data, response.status);
  return data;
}

class ApiError extends Error {
  constructor(problem, status) {
    super(problem.detail || problem.title || `HTTP ${status}`);
    this.problem = { status, ...problem };
  }
}

// Server-Sent Events over fetch (so the Authorization header can be sent).
async function readEvents(url, lastId, onEvent) {
  const headers = { Accept: "text/event-stream" };
  if (state.token) headers.Authorization = `Bearer ${state.token}`;
  if (lastId) headers["Last-Event-ID"] = String(lastId);
  const response = await fetch(url, { headers });
  if (!response.ok) throw new ApiError(await response.json().catch(() => ({})), response.status);
  const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
  let buffer = "";
  let last = lastId || 0;
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += value;
    let split;
    while ((split = buffer.search(/\r?\n\r?\n/)) !== -1) {
      const raw = buffer.slice(0, split);
      buffer = buffer.slice(split).replace(/^\r?\n\r?\n/, "");
      const event = { id: null, event: "message", data: "" };
      for (const line of raw.split(/\r?\n/)) {
        if (line.startsWith(":")) continue;                 // comment / heartbeat
        const colon = line.indexOf(":");
        const field = colon === -1 ? line : line.slice(0, colon);
        const val = colon === -1 ? "" : line.slice(colon + 1).replace(/^ /, "");
        if (field === "id") event.id = Number(val);
        else if (field === "event") event.event = val;
        else if (field === "data") event.data += val;
      }
      if (!event.data) continue;
      last = event.id ?? last;
      onEvent({ id: event.id, event: event.event, data: JSON.parse(event.data) });
    }
  }
  return last;
}

// ---------------------------------------------------------------- Markdown (sanitised)
function renderMarkdown(el, text) {
  const html = window.marked ? window.marked.parse(text, { gfm: true, breaks: false }) : null;
  if (!html || !window.DOMPurify) { el.textContent = text; return; }
  el.innerHTML = window.DOMPurify.sanitize(html, {
    FORBID_TAGS: ["img", "style", "form", "iframe", "svg", "math", "input", "button"],
    FORBID_ATTR: ["style", "srcset"],
  });
  for (const a of el.querySelectorAll("a")) { a.target = "_blank"; a.rel = "noopener noreferrer"; }
  for (const table of el.querySelectorAll("table")) {
    const wrap = document.createElement("div");
    wrap.className = "table-wrap";
    table.replaceWith(wrap);
    wrap.append(table);
  }
}

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else if (v !== undefined && v !== null) node.setAttribute(k, v);
  }
  node.append(...children.filter(Boolean));
  return node;
}

// ---------------------------------------------------------------- turn view model
function newTurn(turn) {
  const view = {
    id: turn.id, url: turn.url, eventsUrl: turn.events_url, message: turn.message,
    status: turn.status, events: [], lastId: 0, usage: turn.usage || {},
    liveText: "", segments: [], approvals: new Map(),
    chatNode: null, agentNode: null, bodyNode: null, toolsNode: null,
  };
  state.turns.set(turn.id, view);
  mountTurnInChat(view);
  return view;
}

function mountTurnInChat(view) {
  const chat = $("#chat");
  const user = el("li", { class: "msg msg--user" },
    el("div", { class: "msg__who", text: "You" }),
    el("div", { class: "msg__body", text: view.message }));
  const selectBtn = el("button", { class: "msg__turn", type: "button", text: "show chain" });
  selectBtn.addEventListener("click", () => selectTurn(view.id));
  const agent = el("li", { class: "msg msg--agent" },
    el("div", { class: "msg__who" }, document.createTextNode("Agent"), selectBtn));
  view.toolsNode = el("div", { class: "msg__tools" });
  view.bodyNode = el("div", { class: "msg__body md" });
  agent.append(view.toolsNode, view.bodyNode);
  chat.append(user, agent);
  view.agentNode = agent;
  chat.scrollTop = chat.scrollHeight;
}

// One event → update chat + chain.
function apply(view, evt) {
  if (evt.id && evt.id <= view.lastId) return;       // replay safety
  view.lastId = evt.id || view.lastId;
  view.events.push(evt);
  const d = evt.data;

  switch (evt.event) {
    case "route.decided":
      $("#route-badge").textContent = `route: ${d.route}${d.reason === "continuation" ? " (kept)" : ""}`;
      break;
    case "text.delta":
      view.liveText += d.text;
      renderAgentBody(view, true);
      break;
    case "message.completed":
      if (d.text) view.segments.push(d.text);
      view.liveText = "";
      for (const name of d.tool_calls) view.toolsNode.append(el("span", { text: `-> ${name}` }));
      renderAgentBody(view, false);
      break;
    case "approval.required":
      view.status = "awaiting_approval";
      renderApproval(view, d);
      break;
    case "approval.decided":
      resolveApproval(view, d.approval_id, d.decision);
      // approved/rejected resume the turn; "abandoned" is followed by turn.failed
      // or nothing at all, so it must not flip the turn back to running.
      if (d.decision === "approved" || d.decision === "rejected") view.status = "running";
      break;
    case "usage":
      view.usage = d;
      break;
    case "turn.completed":
      view.status = "completed";
      if (d.reply_is_markdown_document) { view.segments = [d.reply]; }
      else if (!view.segments.length && d.reply) { view.segments = [d.reply]; }
      renderAgentBody(view, false);
      break;
    case "turn.failed":
      view.status = "failed";
      renderProblem(view, d.problem);
      break;
  }
  if (state.selected === view.id) renderChain(view);
}

function renderAgentBody(view, live) {
  const text = [...view.segments, view.liveText].filter(Boolean).join("\n\n");
  renderMarkdown(view.bodyNode, text || (live ? "" : ""));
  view.bodyNode.classList.toggle("caret", live || view.status === "running");
  const chat = $("#chat");
  chat.scrollTop = chat.scrollHeight;
}

function renderProblem(view, problem) {
  view.bodyNode.classList.remove("caret");
  const box = el("div", { class: "problem", role: "alert" },
    el("strong", { text: problem.title || "The turn failed" }),
    el("div", { text: problem.detail || "" }));
  if (problem.type && problem.type.startsWith("/problems/")) {
    box.append(el("a", { href: problem.type, target: "_blank", rel: "noopener", text: "what this means" }));
  }
  view.agentNode.append(box);
}

// ---------------------------------------------------------------- approvals
function renderApproval(view, d) {
  if (view.approvals.has(d.approval_id)) return;
  const card = el("div", { class: "approval", role: "group", "aria-label": "Approval required" },
    el("div", { class: "approval__head", text: "Approval required — the agent wants to make a change" }),
    el("pre", { text: (d.descriptions || []).join("\n\n") }));
  if (d.warning) card.append(el("p", { class: "approval__warning", text: d.warning }));
  const reject = el("button", { class: "btn", type: "button", text: "Reject" });
  const approve = el("button", { class: "btn btn--ink", type: "button", text: "Approve" });
  const result = el("span", { class: "approval__result" });
  card.append(el("div", { class: "approval__actions" }, reject, approve, result));
  reject.addEventListener("click", () => decide(view, d.approval_id, "reject", card));
  approve.addEventListener("click", () => decide(view, d.approval_id, "approve", card));
  view.approvals.set(d.approval_id, { card, result, buttons: [reject, approve] });
  view.bodyNode.classList.remove("caret");
  view.agentNode.append(card);
  setBusy(true, "Waiting for your decision — nothing is written until you approve.");
  $("#chat").scrollTop = $("#chat").scrollHeight;
}

function resolveApproval(view, approvalId, decision) {
  const entry = view.approvals.get(approvalId);
  if (!entry) return;
  entry.buttons.forEach((b) => (b.disabled = true));
  const words = { approved: "Approved — running it now.", rejected: "Rejected — nothing was written.",
                  abandoned: "Abandoned — nothing was written." };
  entry.result.textContent = words[decision] || decision;
  entry.card.classList.toggle("is-approved", decision === "approved");
  entry.card.classList.toggle("is-closed", decision !== "approved");
}

async function decide(view, approvalId, decision, card) {
  const entry = view.approvals.get(approvalId);
  entry.buttons.forEach((b) => (b.disabled = true));
  entry.result.textContent = decision === "approve" ? "Approving…" : "Rejecting…";
  try {
    await api(`${API}/conversations/${state.conversation}/approvals/${approvalId}/decision`,
              { method: "POST", body: { decision } });
    setBusy(true, "Working…");
    await follow(view);
  } catch (err) {
    entry.result.textContent = err.problem?.title || err.message;
    if (err.problem?.status !== 409) entry.buttons.forEach((b) => (b.disabled = false));
    setBusy(false);
  }
}

// ---------------------------------------------------------------- thinking chain
function selectTurn(turnId) {
  state.selected = turnId;
  for (const [id, v] of state.turns) v.agentNode.classList.toggle("is-selected", id === turnId);
  renderChain(state.turns.get(turnId));
}

const t0 = (view) => Date.parse(view.events[0]?.data.ts || 0);
const rel = (view, ts) => `+${((Date.parse(ts) - t0(view)) / 1000).toFixed(2)}s`;

function renderChain(view) {
  const chain = $("#chain");
  chain.replaceChildren();
  const u = view.usage || {};
  $("#chain-usage").textContent = u.total_tokens
    ? `${u.total_tokens.toLocaleString()} tokens · ${u.model_calls} model call${u.model_calls === 1 ? "" : "s"}`
    : "— tokens";

  chain.append(el("div", { class: "chain__turn-head" },
    el("span", { class: `pill pill--${view.status}`, text: view.status.replace("_", " ") }),
    el("q", { text: view.message.length > 120 ? view.message.slice(0, 117) + "…" : view.message })));

  const steps = el("ol", { class: "steps" });
  let reasoning = null, writing = null;
  const openTools = new Map();

  for (const evt of view.events) {
    const d = evt.data;
    const time = rel(view, d.ts);
    const step = (kind, label, meta, ...extra) => {
      const li = el("li", { class: `step step--${kind}` },
        el("div", { class: "step__line" },
          el("span", { class: "step__time", text: time }),
          el("span", { class: "step__label", text: label }),
          el("span", { class: "step__meta", text: meta || "" })),   // always present: steps update it
        ...extra);
      steps.append(li);
      return li;
    };
    switch (evt.event) {
      case "turn.started": step("start", "message received"); break;
      case "route.decided":
        step("route", `route → ${d.lane}`, d.reason === "continuation"
          ? "kept the previous lane: a confirmation, no model call"
          : d.reason === "classifier" ? "chosen by the classifier model"
          : d.reason === "custom" ? "chosen by a fixed router (tests/demo)" : d.reason);
        reasoning = writing = null;
        break;
      case "reasoning.delta":
        if (!reasoning) {
          const box = el("div", { class: "reasoning md" });
          step("reasoning", "model reasoning", "summary streamed by the model", box);
          reasoning = box;
          reasoning.raw = "";
        }
        reasoning.raw += d.text;
        // Summaries arrive as Markdown sections with no separator between them
        // ("…report.**Next heading**"); start each bold heading on its own line.
        renderMarkdown(reasoning, reasoning.raw.replace(/([.!?:])\*\*(?=[A-Z])/g, "$1\n\n**"));
        break;
      case "text.delta":
        if (!writing) { writing = step("text", "model writing", ""); writing.chars = 0; }
        writing.chars += d.text.length;
        writing.querySelector(".step__meta").textContent = `${writing.chars} chars`;
        break;
      case "message.completed":
        reasoning = writing = null;
        if (d.tool_calls.length) step("model", "model asks for tools", d.tool_calls.join(", "));
        break;
      case "tool.started": {
        const li = step("tool", d.name, "running…",
          el("details", {}, el("summary", { text: "input" }), el("pre", { text: JSON.stringify(d.input, null, 2) })));
        openTools.set(d.tool_use_id, li);
        break;
      }
      case "tool.finished": {
        const li = openTools.get(d.tool_use_id) || step("tool", d.name, "");
        const meta = li.querySelector(".step__meta");
        meta.textContent = `${d.status === "success" ? "ok" : d.status}` +
          (d.duration_ms != null ? ` · ${d.duration_ms} ms` : "") +
          (d.images ? ` · ${d.images} image${d.images === 1 ? "" : "s"}` : "");
        meta.className = `step__meta ${d.status === "success" ? "step__ok" : "step__bad"}`;
        li.append(el("details", {}, el("summary", { text: d.output_truncated ? "output (preview)" : "output" }),
          el("pre", { text: d.output || "(empty)" })));
        break;
      }
      case "approval.required":
        step("approval", "paused before a write", `${d.calls.map((c) => c.name).join(", ")} — waiting for you`);
        break;
      case "approval.decided":
        step("approval", `approval ${d.decision}`, d.decision === "approved" ? "the batch runs" : "nothing is written");
        break;
      case "prd.step": {
        const labels = {
          "writer.started": [`PRD writer · pass ${d.pass}`, "drafting"],
          "writer.finished": [`PRD writer · pass ${d.pass}`, `${d.requirements} requirement(s): “${d.title}”`],
          "reviewer.finished": [`PRD reviewer · pass ${d.pass}`, d.approved ? "approved" :
            `sent back: ${[...(d.missing_requirements || []), ...(d.issues || [])].join("; ") || "quality concerns"}`],
          "render": ["render markdown", `${d.passes} pass(es), deterministic Python`],
        };
        const [label, meta] = labels[d.step] || [d.step, ""];
        step(d.step === "reviewer.finished" && !d.approved ? "approval" : "model", label, meta);
        break;
      }
      case "usage": step("done", "usage", `${d.total_tokens} tokens · ${d.model_calls} model call(s)`); break;
      case "turn.completed": step("done", "turn complete"); break;
      case "turn.failed": step("error", "turn failed", d.problem?.title || ""); break;
    }
  }
  chain.append(steps);
  chain.scrollTop = chain.scrollHeight;
}

// ---------------------------------------------------------------- running a turn
// A rendering bug must never cost the user the rest of the stream: log it, keep reading.
function safeApply(view, evt) {
  try { apply(view, evt); } catch (err) { console.error("could not render event", evt.event, err); }
}

async function follow(view) {
  // The server closes the stream only when the turn rests. A stream that ends
  // while the turn is still "running" was cut (proxy timeout, restart): resume
  // from Last-Event-ID. A dropped connection (fetch's TypeError) is retried the
  // same way; anything else is a real error and is shown.
  for (let attempt = 0; ; attempt++) {
    try {
      view.lastId = await readEvents(view.eventsUrl, view.lastId, (evt) => safeApply(view, evt));
      if (view.status !== "running") break;
    } catch (err) {
      if (!(err instanceof TypeError)) throw err;
    }
    if (attempt >= 6) throw new Error("Lost the connection to the server; reload the page to resume.");
    await new Promise((r) => setTimeout(r, Math.min(500 * 2 ** attempt, 8000)));
  }
  const status = view.status;
  if (status === "awaiting_approval") return;
  setBusy(false, status === "failed" ? "That message failed — see above. You can send another." : "");
}

async function send(text) {
  setBusy(true, "Working…");
  try {
    if (!state.conversation) await newConversation(false);
    const key = crypto.randomUUID();
    const turn = await api(`${API}/conversations/${state.conversation}/turns`,
                           { method: "POST", body: { message: text }, headers: { "Idempotency-Key": key } });
    const view = newTurn(turn);
    selectTurn(view.id);
    await follow(view);
  } catch (err) {
    console.error(err);
    setBusy(false, err.problem?.title ? `${err.problem.title}: ${err.problem.detail || ""}` : err.message);
  }
}

function setBusy(busy, note = "") {
  state.busy = busy;
  $("#send").disabled = busy;
  $("#message").disabled = busy;
  $("#composer-state").textContent = note;
  if (!busy) $("#message").focus();
}

async function newConversation(clearUi = true) {
  if (state.conversation && clearUi) {
    try { await api(`${API}/conversations/${state.conversation}`, { method: "DELETE" }); } catch { /* already gone */ }
  }
  const conv = await api(`${API}/conversations`, { method: "POST" });
  state.conversation = conv.id;
  safeSet("pmagent.conversation", conv.id);
  if (clearUi) {
    state.turns.clear();
    state.selected = null;
    $("#chat").replaceChildren();
    $("#chain").replaceChildren(el("p", { class: "chain__empty", text: "New conversation. Steps appear here as the agent works." }));
    $("#route-badge").textContent = "no route yet";
    $("#chain-usage").textContent = "— tokens";
    setBusy(false, "Started a new conversation.");
  }
}

// Rebuild from the server after a reload: the chat and every turn's chain.
async function restore(id) {
  const [conv, turns] = await Promise.all([
    api(`${API}/conversations/${id}`),
    api(`${API}/conversations/${id}/turns`),
  ]);
  state.conversation = conv.id;
  for (const turn of turns.items) {
    const view = newTurn(turn);
    for (const evt of turn.events) safeApply(view, evt);
    view.status = turn.status;
  }
  const last = turns.items.at(-1);
  if (last) selectTurn(last.id);
  // Replaying events re-ran their side effects (an old approval card locks the
  // composer); the server's current status is what decides the final state.
  if (last && last.status === "running") { setBusy(true, "Working…"); await follow(state.turns.get(last.id)); }
  else if (conv.pending_approval) setBusy(true, "Waiting for your decision — nothing is written until you approve.");
  else setBusy(false, "");
}

// ---------------------------------------------------------------- boot
function showTokenGate() { $("#token-gate").hidden = false; $("#token").focus(); }

async function loadMeta() {
  const meta = await api(`${API}/meta`);
  $("#meta-line").textContent = `${meta.provider} · ${meta.model} · Jira ${meta.jira_project}` +
    (meta.lucid_connected ? " · Lucid" : "");
  $("#reasoning-note").textContent = meta.reasoning_note;
}

async function boot() {
  $("#composer").addEventListener("submit", (e) => {
    e.preventDefault();
    const text = $("#message").value.trim();
    if (!text || state.busy) return;
    $("#message").value = "";
    $("#help").hidden = true;                 // the examples have done their job
    $("#btn-help").setAttribute("aria-expanded", "false");
    send(text);
  });
  $("#message").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); $("#composer").requestSubmit(); }
  });
  $("#btn-new").addEventListener("click", () => newConversation(true).catch((err) => setBusy(false, err.message)));
  $("#btn-help").addEventListener("click", (e) => {
    const help = $("#help");
    help.hidden = !help.hidden;
    e.currentTarget.setAttribute("aria-expanded", String(!help.hidden));
  });
  for (const chip of document.querySelectorAll("[data-example]")) {
    chip.addEventListener("click", () => { $("#message").value = chip.dataset.example; $("#message").focus(); });
  }
  $("#token-form").addEventListener("submit", (e) => {
    e.preventDefault();
    state.token = $("#token").value;
    safeSet("pmagent.token", state.token);
    $("#token-gate").hidden = true;
    start();
  });
  start();
}

async function start() {
  try {
    await loadMeta();
    const saved = safeGet("pmagent.conversation");
    if (saved) {
      try { await restore(saved); return; } catch { /* gone after a restart: start fresh */ }
    }
    await newConversation(false);
    $("#help").hidden = false;
    $("#btn-help").setAttribute("aria-expanded", "true");
  } catch (err) {
    if (err.problem?.status !== 401) $("#meta-line").textContent = `offline: ${err.message}`;
  }
}

boot();
