/* The console, in one file, with no dependencies and no build step.
 *
 * Chat and Work are transcripts, not forms with an output box. A run in this
 * system is a sequence of things that happened -- a tool proposed, a node
 * finished, a person asked -- and the interesting part is the sequence. Putting
 * the steps beside the answer instead of before it makes the reader correlate
 * two panes by timestamp to reconstruct what the runtime actually did.
 *
 * Two things here are decisions rather than shortcuts.
 *
 * Chat events arrive over fetch, parsed by hand. EventSource cannot send the
 * identity headers -- its constructor takes withCredentials and nothing else --
 * so it cannot authenticate against this API at all. Parsing the stream also
 * means Last-Event-ID is sent explicitly, which is what the server's cursor was
 * built for: a reconnect is a subscription starting further along.
 *
 * Work is polled. The event route is mounted per chat session, so a task has no
 * stream to subscribe to; polling its timeline by cursor is the honest thing to
 * do rather than pretending otherwise. It stops the moment the task reaches a
 * status it can no longer leave.
 */

"use strict";

const TERMINAL = new Set(["succeeded", "failed", "cancelled", "dead_letter"]);
const POLL_MS = 2500;

/* Node ids of the fixed v1 graph, in declaration order. Used only to decide
 * what counts as a node step; an unknown id still renders. */
const NODES = new Set([
  "understand", "plan", "route", "research_internal", "research_external",
  "synthesize", "critic", "quality_gate", "approval", "export",
]);

const el = (id) => document.getElementById(id);

/* ------------------------------------------------------------------ identity */

function identityHeaders() {
  const headers = {
    "x-tenant-id": el("tenant").value.trim(),
    "x-principal-id": el("principal").value.trim(),
  };
  const scopes = el("scopes").value.trim();
  // Absent and empty both mean "no scopes". Sending the empty string makes a
  // request look like it decided something.
  if (scopes) headers["x-principal-scopes"] = scopes;
  return headers;
}

/* ---------------------------------------------------------------------- http */

class ApiError extends Error {
  constructor(status, detail) {
    super(detail || `request failed with ${status}`);
    this.status = status;
    this.detail = detail;
  }
}

async function api(path, options = {}) {
  const headers = { ...identityHeaders(), ...(options.headers || {}) };
  if (options.body !== undefined) headers["content-type"] = "application/json";
  const response = await fetch(path, {
    ...options,
    headers,
    body: options.body === undefined ? undefined : JSON.stringify(options.body),
  });
  if (!response.ok) {
    let detail = "";
    try {
      detail = (await response.json()).detail || "";
    } catch {
      /* a body that is not JSON tells us nothing worth showing */
    }
    throw new ApiError(response.status, detail);
  }
  return response.status === 204 ? null : response.json();
}

function newKey() {
  const raw =
    typeof crypto !== "undefined" && crypto.randomUUID
      ? crypto.randomUUID()
      : String(Date.now()) + Math.random().toString(16).slice(2);
  return "ui_" + raw.replace(/-/g, "");
}

let toastTimer = null;
function toast(message) {
  const node = el("toast");
  node.textContent = message;
  node.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (node.hidden = true), 6500);
}

function reportError(error, context) {
  if (error instanceof ApiError) {
    // A 404 on a chat path is this deployment saying it serves no model: the
    // route is not registered rather than registered and failing.
    if (error.status === 404 && context.startsWith("chat")) {
      toast("This deployment serves no chat — the route is not registered.");
      return;
    }
    toast(`${context}: ${error.status}${error.detail ? " — " + error.detail : ""}`);
    return;
  }
  toast(`${context}: ${error.message}`);
}

/* --------------------------------------------------------------- transcript */

function stepNode(glyph, name, { arg, tail, tone } = {}) {
  const row = document.createElement("div");
  row.className = "step" + (tone ? " " + tone : "");
  row.append(span("glyph", glyph), span("name", name));
  if (arg) row.append(span("arg", arg));
  if (tail) row.append(span("tail", tail));
  return row;
}

function resultNode(body) {
  const row = document.createElement("div");
  row.className = "result";
  row.append(span("body", body));
  return row;
}

function span(className, textContent) {
  const node = document.createElement("span");
  node.className = className;
  node.textContent = textContent;
  return node;
}

function atBottom(container) {
  return container.scrollHeight - container.scrollTop - container.clientHeight < 120;
}

function keepPinned(container, wasAtBottom) {
  if (wasAtBottom) container.scrollTop = container.scrollHeight;
}

/* ---------------------------------------------------------------------- tabs */

el("tabs").addEventListener("click", (event) => {
  const button = event.target.closest("button[data-tab]");
  if (!button) return;
  for (const other of el("tabs").querySelectorAll("button")) {
    other.classList.toggle("active", other === button);
  }
  for (const section of document.querySelectorAll(".tab")) {
    section.hidden = section.id !== `tab-${button.dataset.tab}`;
  }
  if (button.dataset.tab === "work") refreshRuns();
  if (button.dataset.tab === "approvals") refreshApprovals();
});

/* -------------------------------------------------------------------- health */

async function checkHealth() {
  const pill = el("health");
  try {
    const response = await fetch("/health/ready");
    pill.textContent = response.ok ? "ready" : "unready";
    pill.className = "pill " + (response.ok ? "pill-ok" : "pill-bad");
  } catch {
    pill.textContent = "offline";
    pill.className = "pill pill-bad";
  }
}

/* ---------------------------------------------------------------------- chat */

const sessions = new Map(); /* id -> {id, title, host} */
let currentSession = null;
let chatStream = null;

/* Steps are keyed by the run they belong to, not by "whatever turn is open".
 *
 * The first version appended to a mutable `liveTurn` and cleared it when the
 * request returned, which silently dropped every event that arrived after the
 * answer -- and rendered nothing at all when a reconnect replayed a session's
 * history. A run id is in every envelope and is the thing that actually
 * identifies what the steps describe.
 *
 * The client learns a turn's run id from the response, which may arrive before
 * or after the run's first event. Both orders happen, so events for a run
 * nobody has claimed yet are held until one does. */
const turnsByRun = new Map(); /* run_id -> turn */
const orphanEvents = new Map(); /* run_id -> [[kind, payload], …] */
let unclaimedTurn = null; /* submitted, run id not yet known */

el("chat-new").addEventListener("click", () => {
  currentSession = null;
  unclaimedTurn = null;
  turnsByRun.clear();
  orphanEvents.clear();
  if (chatStream) chatStream.abort();
  chatStream = null;
  renderSessionRail();
  showChatTranscript(null);
});

autoGrow(el("chat-question"));
submitOnEnter(el("chat-question"), el("chat-form"));

el("chat-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const question = el("chat-question").value.trim();
  if (!question) return;
  el("chat-send").disabled = true;
  el("chat-question").value = "";
  autoGrow(el("chat-question"))();

  try {
    if (!currentSession) {
      const opened = await api("/v1/chat/sessions", { method: "POST", body: {} });
      currentSession = opened.session_id;
      sessions.set(currentSession, {
        id: currentSession,
        title: question,
        host: document.createElement("div"),
      });
      renderSessionRail();
      showChatTranscript(currentSession);
      startEventStream(currentSession);
    }
    const host = sessions.get(currentSession).host;
    unclaimedTurn = beginTurn(host, question);
    const submitted = unclaimedTurn;
    const answered = await api(`/v1/chat/sessions/${currentSession}/messages`, {
      method: "POST",
      // A turn without a key is a turn a timed-out client cannot retry without
      // asking the model again and paying for it twice.
      headers: { "Idempotency-Key": newKey() },
      body: { question, knowledge_base_id: el("chat-kb").value, top_k: 8 },
    });
    claimRun(answered.run_id, submitted);
    finishTurn(submitted, answered);
  } catch (error) {
    if (unclaimedTurn) {
      unclaimedTurn.steps.append(stepNode("✗", "turn failed", { tone: "bad" }));
      unclaimedTurn.answer.classList.remove("streaming");
    }
    reportError(error, "chat ask");
  } finally {
    unclaimedTurn = null;
    el("chat-send").disabled = false;
  }
});

/* Bind a turn to its run, and replay anything that arrived before the binding.
 * A caller may already have been handed the run's events. */
function claimRun(runId, turn) {
  if (!runId || turnsByRun.has(runId)) return;
  turnsByRun.set(runId, turn);
  if (unclaimedTurn === turn) unclaimedTurn = null;
  const held = orphanEvents.get(runId);
  if (!held) return;
  orphanEvents.delete(runId);
  for (const [kind, payload] of held) absorbRunEvent(runId, kind, payload);
}


function beginTurn(host, question) {
  const turn = document.createElement("article");
  turn.className = "turn";
  const user = document.createElement("div");
  user.className = "turn-user";
  user.append(document.createTextNode(question));
  const steps = document.createElement("div");
  const answer = document.createElement("div");
  answer.className = "answer streaming";
  const cites = document.createElement("div");
  cites.className = "cites";
  turn.append(user, steps, answer, cites);
  host.append(turn);
  el("chat-transcript").scrollTop = el("chat-transcript").scrollHeight;
  return { steps, answer, cites, tools: new Map() };
}

function finishTurn(turn, answered) {
  turn.answer.classList.remove("streaming");
  turn.answer.textContent = answered.answer;
  if (answered.withheld) {
    turn.steps.append(
      stepNode("!", "withheld", {
        arg: "a source stopped being readable while the answer was written",
        tone: "wait",
      })
    );
  }
  turn.cites.replaceChildren();
  for (const citation of answered.citations) {
    const chip = document.createElement("span");
    chip.className = "cite";
    chip.textContent = citation.chunk_id;
    chip.title = `${citation.document_id} · ${citation.document_version}${
      citation.quote ? "\n\n" + citation.quote : ""
    }`;
    turn.cites.append(chip);
  }
  if (!answered.citations.length) {
    turn.cites.append(
      span("faint", "no citations — the model named no passage it was shown")
    );
  }
  el("chat-transcript").scrollTop = el("chat-transcript").scrollHeight;
}

function renderSessionRail() {
  const rail = el("chat-sessions");
  rail.replaceChildren();
  for (const session of sessions.values()) {
    const item = document.createElement("button");
    item.className = "rail-item" + (session.id === currentSession ? " active" : "");
    item.type = "button";
    item.append(span("title", session.title), span("id", session.id));
    item.addEventListener("click", () => {
      currentSession = session.id;
      renderSessionRail();
      showChatTranscript(session.id);
      startEventStream(session.id);
    });
    rail.append(item);
  }
}

function showChatTranscript(sessionId) {
  const container = el("chat-transcript");
  if (!sessionId) {
    container.replaceChildren(
      placeholder(
        "Ask something grounded in an indexed knowledge base.",
        "Answers cite only passages the model was shown and named."
      )
    );
    return;
  }
  container.replaceChildren(sessions.get(sessionId).host);
  container.scrollTop = container.scrollHeight;
}

/* Read the event stream with fetch, because EventSource cannot authenticate.
 *
 * Frames are `id:`, `event:` and `data:` lines terminated by a blank line, plus
 * `:` comment lines the server sends as heartbeats. The last id seen goes back
 * as Last-Event-ID on reconnect, which is the cursor the log was built for. */
function startEventStream(sessionId) {
  if (chatStream) chatStream.abort();
  const controller = new AbortController();
  chatStream = controller;

  (async () => {
    let lastEventId = null;
    while (!controller.signal.aborted) {
      try {
        const headers = identityHeaders();
        headers.accept = "text/event-stream";
        if (lastEventId) headers["last-event-id"] = lastEventId;
        const response = await fetch(`/v1/chat/sessions/${sessionId}/events`, {
          headers,
          signal: controller.signal,
        });
        if (!response.ok || !response.body) return;
        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";
        for (;;) {
          const { done, value } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });
          let boundary;
          while ((boundary = buffer.indexOf("\n\n")) >= 0) {
            const frame = parseFrame(buffer.slice(0, boundary));
            buffer = buffer.slice(boundary + 2);
            if (!frame) continue;
            if (frame.id) lastEventId = frame.id;
            absorbRunEvent(frame.runId, frame.event, frame.payload);
          }
        }
      } catch (error) {
        if (controller.signal.aborted) return;
        // A dropped connection resumes from the last id rather than starting
        // over, which is the whole point of the cursor.
        await new Promise((resolve) => setTimeout(resolve, 1500));
      }
    }
  })();
}

function parseFrame(raw) {
  let id = null;
  let event = "message";
  let data = "";
  for (const line of raw.split("\n")) {
    if (!line || line.startsWith(":")) continue; /* heartbeat */
    const separator = line.indexOf(":");
    const field = separator < 0 ? line : line.slice(0, separator);
    const value = separator < 0 ? "" : line.slice(separator + 1).trimStart();
    if (field === "id") id = value;
    else if (field === "event") event = value;
    else if (field === "data") data += value;
  }
  if (!data) return null;
  try {
    const envelope = JSON.parse(data);
    return { id, event, runId: envelope.run_id, payload: envelope.payload || {} };
  } catch {
    return { id, event, runId: null, payload: {} };
  }
}

/* One event, folded into the turn it belongs to. Tool events update the step
 * their proposal created rather than appending a second line, so a call reads
 * as one thing that happened and not as four. */
function absorbRunEvent(runId, kind, payload) {
  let turn = turnsByRun.get(runId);
  if (!turn) {
    // The response has not named this run yet. An unclaimed turn takes the
    // first run it sees; anything else is held rather than dropped, because a
    // replay can deliver a whole run before its request ever returns.
    if (unclaimedTurn && kind === "RunStarted") {
      claimRun(runId, unclaimedTurn);
      turn = turnsByRun.get(runId);
    } else {
      const held = orphanEvents.get(runId) || [];
      held.push([kind, payload]);
      orphanEvents.set(runId, held);
      return;
    }
  }
  const liveTurn = turn;
  const container = el("chat-transcript");
  const pinned = atBottom(container);

  if (kind === "ContextBuilt") {
    liveTurn.steps.append(
      stepNode("⏺", "retrieve", {
        arg: `${payload.chunk_count} passages`,
        tail: `${payload.token_estimate} tok`,
        tone: "ok",
      })
    );
  } else if (kind === "ModelStarted") {
    liveTurn.model = stepNode("⏺", "model", {
      arg: payload.model_id || "",
      tone: "running",
    });
    liveTurn.steps.append(liveTurn.model);
  } else if (kind === "ModelCompleted") {
    if (liveTurn.model) {
      liveTurn.model.className = "step ok";
      liveTurn.model.querySelector(".glyph").textContent = "⏺";
      const usage = payload.usage || {};
      const total = (usage.input_tokens || 0) + (usage.output_tokens || 0);
      liveTurn.model.append(span("tail", `${total} tok · ${payload.finish_reason}`));
    }
  } else if (kind === "ToolProposed") {
    const step = stepNode("⏺", payload.tool_name, {
      arg: `${payload.argument_bytes}B`,
      tone: "running",
    });
    liveTurn.tools.set(payload.tool_call_id, step);
    liveTurn.steps.append(step);
  } else if (kind === "PermissionResolved") {
    const step = liveTurn.tools.get(payload.tool_call_id);
    if (step && payload.effect === "deny") {
      step.className = "step bad";
      step.append(span("tail", payload.reason_code));
    }
  } else if (kind === "ToolCompleted" || kind === "ToolFailed") {
    const step = liveTurn.tools.get(payload.tool_call_id);
    const failed = kind === "ToolFailed";
    if (step) {
      step.className = "step " + (failed ? "bad" : "ok");
      step.append(
        span("tail", failed ? payload.error_code || "failed" : `${payload.duration_ms || 0}ms`)
      );
    }
    if (failed && payload.message) liveTurn.steps.append(resultNode(payload.message));
  } else if (kind === "AnswerCommitted") {
    // The documented publication boundary: ModelCompleted records what the
    // provider did, and a grant can still be withdrawn between the two. Answer
    // text is only ever displayed from here.
    finishTurn(liveTurn, {
      answer: payload.text || "",
      citations: payload.citations || [],
      withheld: false,
    });
  } else if (kind === "AnswerWithheld") {
    liveTurn.steps.append(stepNode("!", "withheld", { arg: payload.reason_code, tone: "wait" }));
    finishTurn(liveTurn, { answer: payload.text || "", citations: [], withheld: true });
  }

  keepPinned(container, pinned);
}

/* ---------------------------------------------------------------------- work */

let runCursor = null;
let selectedRun = null;
let runTimer = null;

autoGrow(el("work-objective"));
submitOnEnter(el("work-objective"), el("work-form"));

el("work-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const objective = el("work-objective").value.trim();
  if (!objective) return;
  el("work-send").disabled = true;
  try {
    const body = { objective, max_revisions: 2 };
    const kb = el("work-kb").value.trim();
    if (kb) body.knowledge_base_id = kb;
    const task = await api("/v1/tasks", {
      method: "POST",
      headers: { "Idempotency-Key": newKey() },
      body,
    });
    el("work-objective").value = "";
    autoGrow(el("work-objective"))();
    await refreshRuns();
    selectRun(task.task_id, objective);
  } catch (error) {
    reportError(error, "task submit");
  } finally {
    el("work-send").disabled = false;
  }
});

el("work-status").addEventListener("change", () => refreshRuns());
el("work-more").addEventListener("click", () => refreshRuns({ append: true }));

async function refreshRuns({ append = false } = {}) {
  const params = new URLSearchParams({ limit: "25" });
  const status = el("work-status").value;
  if (status) params.set("status", status);
  if (append && runCursor) params.set("cursor", runCursor);
  try {
    const page = await api(`/v1/tasks?${params}`);
    runCursor = page.cursor;
    // The cursor is what says whether there is more, not the page length: a
    // short page cannot have anything behind it.
    el("work-more").hidden = !page.cursor;
    renderRunRail(page.tasks, append);
  } catch (error) {
    reportError(error, "task list");
  }
}

function renderRunRail(tasks, append) {
  const rail = el("work-list");
  if (!append) rail.replaceChildren();
  if (!tasks.length && !append) {
    rail.replaceChildren(span("empty", "No runs yet."));
    return;
  }
  for (const task of tasks) {
    const item = document.createElement("button");
    item.className = "rail-item" + (task.task_id === selectedRun ? " active" : "");
    item.type = "button";
    const head = document.createElement("span");
    head.className = "title";
    head.append(statusPill(task.status));
    item.append(head, span("id", task.task_id));
    item.addEventListener("click", () => selectRun(task.task_id));
    rail.append(item);
  }
}

function statusPill(status) {
  const tone =
    status === "succeeded" ? "pill-ok"
    : status === "waiting_approval" ? "pill-warn"
    : TERMINAL.has(status) ? "pill-bad"
    : "pill-run";
  const node = span("pill " + tone, status);
  return node;
}

async function selectRun(taskId, objective) {
  selectedRun = taskId;
  clearTimeout(runTimer);
  const host = document.createElement("div");
  host.className = "turn";
  if (objective) {
    const user = document.createElement("div");
    user.className = "turn-user";
    user.append(document.createTextNode(objective));
    host.append(user);
  }
  const steps = document.createElement("div");
  host.append(steps);
  el("work-transcript").replaceChildren(host);
  renderRunRail([], true);
  for (const item of el("work-list").querySelectorAll(".rail-item")) {
    item.classList.toggle("active", item.querySelector(".id").textContent === taskId);
  }
  await pollRun(steps, { cursor: null, seenNode: null });
}

/* Poll the timeline and fold it into node-shaped steps.
 *
 * Every event carries the graph node it came from, so a run reads as the graph
 * it is: one line per node, with the notable things underneath. Lifecycle
 * events have no node and stay top level, which is what makes the boundary
 * between "the product state moved" and "a node did something" visible. */
async function pollRun(steps, state) {
  if (!selectedRun) return;
  try {
    const task = await api(`/v1/tasks/${selectedRun}`);
    const params = new URLSearchParams({ limit: "200" });
    if (state.cursor) params.set("cursor", state.cursor);
    const page = await api(`/v1/tasks/${selectedRun}/timeline?${params}`);
    if (page.cursor) state.cursor = page.cursor;

    const container = el("work-transcript");
    const pinned = atBottom(container);
    for (const envelope of page.events) {
      const kind = (envelope.payload && envelope.payload.kind) || envelope.event_type;
      const node = envelope.graph_node_id;
      const when = (envelope.timestamp || "").slice(11, 19);

      if (node && NODES.has(node)) {
        if (node !== state.seenNode) {
          state.seenNode = node;
          state.nodeStep = stepNode("⏺", node, { tone: "running", tail: when });
          steps.append(state.nodeStep);
        }
        if (state.nodeStep) {
          if (/Failed|Denied/.test(kind)) {
            state.nodeStep.className = "step bad";
            steps.append(resultNode(kind));
          } else if (kind === "RunCompleted" || kind === "ModelCompleted") {
            state.nodeStep.className = "step ok";
          } else if (kind === "ToolProposed") {
            steps.append(resultNode(`${envelope.payload.tool_name}`));
          }
        }
        continue;
      }

      state.seenNode = null;
      if (kind === "TaskApprovalRequested") {
        const asked = stepNode("⏸", "approval", { arg: "waiting for you", tone: "wait" });
        steps.append(asked);
        steps.append(await gateNode(envelope.payload.approval_id, asked, steps, state));
      } else if (kind === "TaskApprovalDecided") {
        steps.append(
          stepNode(envelope.payload.decision === "approved" ? "⏺" : "✗", "decision", {
            arg: envelope.payload.decision,
            tone: envelope.payload.decision === "approved" ? "ok" : "bad",
            tail: when,
          })
        );
      } else {
        steps.append(
          stepNode(
            /Failed|Cancelled|DeadLettered/.test(kind) ? "✗" : "⏺",
            kind,
            {
              tone: /Failed|Cancelled|DeadLettered/.test(kind) ? "bad"
                : /Succeeded/.test(kind) ? "ok" : undefined,
              tail: when,
            }
          )
        );
      }
    }
    if (task.status_detail) steps.append(resultNode(task.status_detail));
    keepPinned(container, pinned);

    // Polling stops the moment the task can no longer change.
    if (!TERMINAL.has(task.status)) {
      runTimer = setTimeout(() => pollRun(steps, state), POLL_MS);
    }
  } catch (error) {
    reportError(error, "task timeline");
  }
}

/* The decision, offered where it happened. The person reading the run is the
 * person being asked, so sending them to another tab to answer is one step of
 * ceremony between a stopped task and the human who can unstop it. */
async function gateNode(approvalId, asked, steps, state) {
  const gate = document.createElement("div");
  gate.className = "gate";
  const paragraph = document.createElement("p");
  paragraph.textContent = "Export the report this run produced?";
  const row = document.createElement("div");
  row.className = "row";

  let record = null;
  try {
    record = await api(`/v1/approvals/${approvalId}`);
  } catch {
    record = { approval_id: approvalId, decision_version: 0, status: "pending" };
  }
  if (record.status !== "pending") {
    // Replayed history, not a live gate. The step says what the run did rather
    // than continuing to claim it is waiting on somebody.
    asked.className = "step " + (record.status === "approved" ? "ok" : "bad");
    asked.querySelector(".glyph").textContent = record.status === "approved" ? "⏺" : "✗";
    asked.querySelector(".arg").textContent = record.status;
    gate.remove();
    return document.createComment("decided");
  }

  for (const [decision, label, primary] of [
    ["approved", "Approve", true],
    ["rejected", "Reject", false],
  ]) {
    const button = document.createElement("button");
    button.textContent = label;
    if (primary) button.className = "primary";
    button.addEventListener("click", async () => {
      for (const other of row.querySelectorAll("button")) other.disabled = true;
      try {
        await api(`/v1/approvals/${approvalId}/decisions`, {
          method: "POST",
          // The version is the idempotency key for a decision: the same one
          // twice records one answer and requeues once.
          body: { decision, decision_version: record.decision_version + 1 },
        });
        asked.className = "step " + (decision === "approved" ? "ok" : "bad");
        asked.querySelector(".glyph").textContent = decision === "approved" ? "⏺" : "✗";
        asked.querySelector(".arg").textContent = `you ${decision} this`;
        gate.remove();
        refreshApprovals();
        clearTimeout(runTimer);
        pollRun(steps, state);
      } catch (error) {
        // A 409 is the task having moved while a person was deciding --
        // cancelled, most often -- not a failure of the click.
        reportError(error, "approval decision");
        for (const other of row.querySelectorAll("button")) other.disabled = false;
      }
    });
    row.append(button);
  }
  gate.append(paragraph, row);
  return gate;
}

/* -------------------------------------------------------------------- search */

el("search-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = event.target.querySelector("button");
  button.disabled = true;
  el("search-meta").textContent = "searching…";
  el("search-results").replaceChildren();
  try {
    const packet = await api("/v1/search", {
      method: "POST",
      body: {
        query: el("search-query").value,
        knowledge_base_id: el("search-kb").value,
        top_k: Number(el("search-topk").value),
      },
    });
    renderSearch(packet);
  } catch (error) {
    el("search-meta").textContent = "";
    reportError(error, "search");
  } finally {
    button.disabled = false;
  }
});

function renderSearch(packet) {
  // Which retriever answered is part of the answer: a result set means
  // something different under each.
  el("search-meta").replaceChildren(
    span("pill pill-ok", packet.retriever),
    span("dim", `${packet.hits.length} hits · ${packet.citations.length} citations`)
  );
  const results = el("search-results");
  if (!packet.hits.length) {
    results.replaceChildren(
      span(
        "empty",
        'No readable passages matched. "There is nothing there" and "there is ' +
          'nothing there for you" are deliberately the same answer.'
      )
    );
    return;
  }
  results.replaceChildren(
    ...packet.hits.map((hit) => {
      const node = document.createElement("div");
      node.className = "card";
      const header = document.createElement("header");
      header.append(
        codeNode(hit.chunk_id),
        codeNode(hit.document_id),
        span("faint", `rev ${hit.document_version}`)
      );
      node.append(header, span("body", hit.text));
      return node;
    })
  );
}

/* ----------------------------------------------------------------- approvals */

let approvalCursor = null;

el("approval-refresh").addEventListener("click", () => refreshApprovals());
el("approval-more").addEventListener("click", () => refreshApprovals({ append: true }));
el("approval-status").addEventListener("change", () => refreshApprovals());

async function refreshApprovals({ append = false } = {}) {
  const params = new URLSearchParams({ limit: "25" });
  const status = el("approval-status").value;
  if (status) params.set("status", status);
  if (append && approvalCursor) params.set("cursor", approvalCursor);
  try {
    const page = await api(`/v1/approvals?${params}`);
    approvalCursor = page.cursor;
    el("approval-more").hidden = !page.cursor;
    renderApprovals(page.approvals, append);
    if (status === "pending" && !append) {
      const badge = el("approval-badge");
      badge.textContent = String(page.approvals.length);
      badge.hidden = page.approvals.length === 0;
    }
  } catch (error) {
    reportError(error, "approval list");
  }
}

function renderApprovals(approvals, append) {
  const list = el("approval-list");
  if (!append) list.replaceChildren();
  if (!approvals.length && !append) {
    list.replaceChildren(span("empty", "Nothing is waiting on you."));
    return;
  }
  for (const record of approvals) {
    const node = document.createElement("div");
    node.className = "card";
    const header = document.createElement("header");
    header.append(
      span(
        "pill " + (record.status === "pending" ? "pill-warn" : "pill"),
        record.status
      ),
      codeNode(record.approval_id),
      span("faint", "on"),
      codeNode(record.task_id)
    );
    node.append(header);
    if (record.status === "pending") {
      const row = document.createElement("div");
      row.className = "row";
      row.style.margin = "0.5rem 0 0";
      for (const [decision, label, primary] of [
        ["approved", "Approve", true],
        ["rejected", "Reject", false],
      ]) {
        const button = document.createElement("button");
        button.textContent = label;
        if (primary) button.className = "primary";
        button.addEventListener("click", async () => {
          button.disabled = true;
          try {
            await api(`/v1/approvals/${record.approval_id}/decisions`, {
              method: "POST",
              body: { decision, decision_version: record.decision_version + 1 },
            });
            await refreshApprovals();
          } catch (error) {
            reportError(error, "approval decision");
            button.disabled = false;
          }
        });
        row.append(button);
      }
      node.append(row);
    }
    // Jumping to the run is the point of a queue: the decision needs the
    // context, and the context is the transcript.
    const open = document.createElement("button");
    open.className = "ghost";
    open.textContent = "open run ›";
    open.addEventListener("click", () => {
      el("tabs").querySelector('button[data-tab="work"]').click();
      selectRun(record.task_id);
    });
    node.append(open);
    list.append(node);
  }
}

/* ------------------------------------------------------------------ elements */

function codeNode(value) {
  const node = document.createElement("code");
  node.textContent = value;
  return node;
}

function placeholder(title, detail) {
  const node = document.createElement("div");
  node.className = "placeholder";
  const first = document.createElement("p");
  first.textContent = title;
  const second = document.createElement("p");
  second.className = "dim";
  second.textContent = detail;
  node.append(first, second);
  return node;
}

function autoGrow(textarea) {
  const resize = () => {
    textarea.style.height = "auto";
    textarea.style.height = Math.min(textarea.scrollHeight, 144) + "px";
  };
  textarea.addEventListener("input", resize);
  return resize;
}

function submitOnEnter(textarea, form) {
  textarea.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      form.requestSubmit();
    }
  });
}

el("identity").addEventListener("submit", (event) => event.preventDefault());
checkHealth();
refreshApprovals();
