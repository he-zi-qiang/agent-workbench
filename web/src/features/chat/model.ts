import type { StreamConnectionState } from "../../api/sse";
import type { StoredChatCursor } from "./storage";
import type {
  AskResponse,
  ChatAnswerMode,
  Citation,
  EventEnvelope,
  LocalChatSession,
  MessageView,
  SourceLocator,
} from "../../api/types";
import {
  isDegradedFrame,
  isLiveFrame,
  isQuarantineFrame,
  type SseChunkFrame,
  type SseFrame,
  type SseLiveFrame,
  type SseQuarantineFrame,
} from "../../api/sse";

export type ChatTurnPhase =
  | "submitting"
  | "running"
  | "committed"
  | "withheld"
  | "failed";

export type ChatActivityState = "running" | "complete" | "waiting" | "failed" | "info";

/**
 * Phases after which a turn produces nothing further.
 *
 * Named here rather than spelled out at each use because live text depends on
 * it twice, in opposite directions: a settled turn must not absorb a late
 * delta, and reaching one of these is what discards the text streamed so far.
 */
const TERMINAL_TURN_PHASES: ReadonlySet<ChatTurnPhase> = new Set([
  "committed",
  "withheld",
  "failed",
]);

export interface ChatActivity {
  key: string;
  eventId: string;
  kind: string;
  label: string;
  state: ChatActivityState;
  timestamp: string;
  detail?: string;
  // The event this was summarised from, kept so a reader can open the step and
  // see the model's own output, the proposed call and the permission verdict
  // rather than only this one-line label.
  envelope: EventEnvelope;
}

export interface ChatTurnState {
  localId: string;
  sessionId: string;
  question: string;
  answerMode: ChatAnswerMode;
  knowledgeBaseId: string | null;
  topK: number;
  idempotencyKey: string;
  submittedAt: string;
  phase: ChatTurnPhase;
  activities: ChatActivity[];
  citations: Citation[];
  historical: boolean;
  runId?: string;
  turnId?: string;
  answer?: string;
  withheldReason?: string;
  error?: string;
  // False when this answer was produced without retrieved evidence (ADR-018).
  // Per-turn rather than per-session on purpose: the routed shape can produce
  // both kinds in one conversation, and a badge driven by "current mode" would
  // relabel every earlier message the moment the mode changed.
  grounded?: boolean;
  /**
   * What the model is writing right now, before anything has been published.
   *
   * Never an answer, and kept in a field of its own so it cannot become one by
   * accident: `answer` is written from `AnswerCommitted` and its two siblings
   * and from nothing else. This is the process, and it is discarded the moment
   * a turn reaches a terminal phase -- including a withheld one, where the text
   * streamed so far is precisely what must not be left on screen.
   */
  stream?: LiveTextState;
}

/**
 * The live text of one model call, and whether there is any to show.
 *
 * `redacted` is a real state rather than an empty string: retrieval-backed
 * shapes stream deltas whose text the server has deliberately blanked
 * (ADR-052), so "the model is writing and you may not see it yet" and "the
 * model has not started" arrive as the same events and must not render the
 * same. `dropped` counts what the server told us it could not deliver.
 */
export interface LiveTextState {
  modelCallId: string;
  text: string;
  redacted: boolean;
  dropped: number;
}

// The shape moved to `api/sse` when a second surface needed it; the name
// stays here because the chat feature reads better in its own vocabulary.
export type ChatConnectionState = StreamConnectionState;

export interface ChatSessionState extends LocalChatSession {
  connection: ChatConnectionState;
  history: "idle" | "loading" | "loaded" | "failed";
  connectionError?: string;
  historyError?: string;
  /**
   * Where this session's stream has got to, when it has got anywhere.
   *
   * Held in state rather than read back from storage at render time: the
   * stored copy is what a *reload* would resume from, and reading it here
   * would show whatever was last flushed rather than where the open stream
   * actually is. Absent until the first frame advances it.
   */
  cursor?: StoredChatCursor;
}

export interface SafeRunEvent {
  eventId: string;
  runId: string;
  kind: string;
  timestamp: string;
  activity: ChatActivity;
  terminal?:
    | { kind: "committed"; text: string; citations: Citation[] }
    | { kind: "ungrounded"; text: string }
    | { kind: "withheld"; text: string; reason: string };
}

export interface ChatState {
  sessions: Record<string, ChatSessionState>;
  sessionOrder: string[];
  turns: Record<string, ChatTurnState>;
  turnOrderBySession: Record<string, string[]>;
  runToTurn: Record<string, string>;
  orphanEvents: Record<string, SafeRunEvent[]>;
  seenEventIds: Record<string, true>;
  /**
   * Positions the server said it examined and could not deliver, per session.
   *
   * Kept apart from `turns` and `orphanEvents` because there is nothing to
   * apply: no payload, no run, no step. What it buys is that the transcript can
   * stop reading as complete -- a history that is short by three positions and
   * says so is a different artifact from one that is merely short.
   */
  quarantinedSequences: Record<string, number[]>;
  revision: number;
}

export interface SubmitTurnInput {
  localId: string;
  sessionId: string;
  question: string;
  answerMode: ChatAnswerMode;
  knowledgeBaseId: string | null;
  topK: number;
  idempotencyKey: string;
  submittedAt: string;
}

export type ChatAction =
  | { type: "sessionAdded"; session: LocalChatSession }
  | { type: "sessionRemoved"; sessionId: string }
  | {
      type: "sessionUpdated";
      sessionId: string;
      answerMode: ChatAnswerMode;
      knowledgeBaseId: string | null;
      updatedAt: string;
    }
  | {
      type: "connectionChanged";
      sessionId: string;
      connection: ChatConnectionState;
      error?: string;
    }
  | { type: "historyLoading"; sessionId: string }
  | { type: "historyLoaded"; sessionId: string; messages: MessageView[] }
  | { type: "historyFailed"; sessionId: string; error: string }
  | { type: "turnSubmitted"; input: SubmitTurnInput }
  | { type: "turnRetrying"; localId: string }
  | { type: "runBound"; localId: string; runId: string; turnId?: string }
  | { type: "askResolved"; localId: string; response: AskResponse }
  | { type: "askRejected"; localId: string; error: string; runId?: string }
  | { type: "cursorAdvanced"; sessionId: string; cursor: StoredChatCursor };

export interface FrameReduction {
  state: ChatState;
  accepted: boolean;
  duplicate: boolean;
}

export function initialChatState(localSessions: LocalChatSession[] = []): ChatState {
  const sessions: Record<string, ChatSessionState> = {};
  const order: string[] = [];
  for (const session of localSessions) {
    if (sessions[session.sessionId] !== undefined) continue;
    sessions[session.sessionId] = {
      ...session,
      connection: "idle",
      history: "idle",
    };
    order.push(session.sessionId);
  }
  return {
    sessions,
    sessionOrder: order,
    turns: {},
    turnOrderBySession: {},
    runToTurn: {},
    orphanEvents: {},
    seenEventIds: {},
    quarantinedSequences: {},
    revision: 0,
  };
}

export function chatReducer(state: ChatState, action: ChatAction): ChatState {
  switch (action.type) {
    case "sessionAdded": {
      const existing = state.sessions[action.session.sessionId];
      const session: ChatSessionState = {
        ...action.session,
        connection: existing?.connection ?? "idle",
        history: existing?.history ?? "idle",
        ...(existing?.connectionError === undefined
          ? {}
          : { connectionError: existing.connectionError }),
        ...(existing?.historyError === undefined ? {} : { historyError: existing.historyError }),
      };
      return bump(state, {
        sessions: { ...state.sessions, [session.sessionId]: session },
        sessionOrder:
          existing === undefined
            ? [session.sessionId, ...state.sessionOrder]
            : state.sessionOrder,
      });
    }
    case "sessionRemoved": {
      if (state.sessions[action.sessionId] === undefined) return state;
      // Every index keyed on the session, and the turns underneath it. Leaving
      // `turns` behind would be invisible -- nothing renders a turn whose
      // session is gone -- and would grow forever in the persisted state,
      // because `persistSessions` writes what this reducer holds.
      const doomed = new Set(state.turnOrderBySession[action.sessionId] ?? []);
      const turns = Object.fromEntries(
        Object.entries(state.turns).filter(([turnId]) => !doomed.has(turnId)),
      );
      const runToTurn = Object.fromEntries(
        Object.entries(state.runToTurn).filter(([, turnId]) => !doomed.has(turnId)),
      );
      return bump(state, {
        sessions: without(state.sessions, action.sessionId),
        sessionOrder: state.sessionOrder.filter((id) => id !== action.sessionId),
        turns,
        turnOrderBySession: without(state.turnOrderBySession, action.sessionId),
        runToTurn,
        orphanEvents: without(state.orphanEvents, action.sessionId),
        quarantinedSequences: without(state.quarantinedSequences, action.sessionId),
      });
    }
    case "sessionUpdated": {
      const current = state.sessions[action.sessionId];
      if (current === undefined) return state;
      return bump(state, {
        sessions: {
          ...state.sessions,
          [action.sessionId]: {
            ...current,
            answerMode: action.answerMode,
            knowledgeBaseId: action.knowledgeBaseId,
            updatedAt: action.updatedAt,
          },
        },
      });
    }
    case "cursorAdvanced": {
      const held = state.sessions[action.sessionId];
      if (held === undefined) return state;
      return {
        ...state,
        sessions: {
          ...state.sessions,
          [action.sessionId]: { ...held, cursor: action.cursor },
        },
      };
    }
    case "connectionChanged": {
      const current = state.sessions[action.sessionId];
      if (current === undefined) return state;
      const next: ChatSessionState = {
        ...current,
        connection: action.connection,
      };
      if (action.error === undefined) delete next.connectionError;
      else next.connectionError = action.error;
      return bump(state, {
        sessions: { ...state.sessions, [action.sessionId]: next },
      });
    }
    case "historyLoading":
      return updateSessionHistory(state, action.sessionId, "loading");
    case "historyLoaded":
      return historyLoaded(state, action.sessionId, action.messages);
    case "historyFailed":
      return updateSessionHistory(state, action.sessionId, "failed", action.error);
    case "turnSubmitted":
      return turnSubmitted(state, action.input);
    case "turnRetrying": {
      const turn = state.turns[action.localId];
      if (turn === undefined || turn.historical) return state;
      const next: ChatTurnState = { ...turn, phase: "submitting" };
      delete next.error;
      return bump(state, { turns: { ...state.turns, [turn.localId]: next } });
    }
    case "runBound":
      return bindRun(state, action.localId, action.runId, action.turnId);
    case "askResolved": {
      if (!canBindRun(state, action.localId, action.response.run_id)) {
        return failRunCorrelation(state, action.localId);
      }
      const bound = bindRun(state, action.localId, action.response.run_id, action.response.turn_id);
      const turn = bound.turns[action.localId];
      if (turn === undefined) return bound;
      return replaceTurn(
        bound,
        finalizeTurn(
          turn,
          action.response.withheld
            ? {
                kind: "withheld",
                text: action.response.answer,
                reason: "sources_changed",
              }
            : action.response.grounded === false
              ? { kind: "ungrounded", text: action.response.answer }
              : {
                  kind: "committed",
                  text: action.response.answer,
                  citations: action.response.citations,
                },
        ),
      );
    }
    case "askRejected": {
      if (
        action.runId !== undefined &&
        !canBindRun(state, action.localId, action.runId)
      ) {
        return failRunCorrelation(state, action.localId);
      }
      const bound =
        action.runId === undefined
          ? state
          : bindRun(state, action.localId, action.runId, undefined);
      const turn = bound.turns[action.localId];
      if (turn === undefined || turn.phase === "committed" || turn.phase === "withheld") {
        return bound;
      }
      return replaceTurn(bound, { ...turn, phase: "failed", error: action.error });
    }
  }
}

/**
 * One frame from the session stream, of either kind it can be.
 *
 * The union stops at this function. A quarantine notice is answered here and
 * never reaches `safeEventFromFrame`, so nothing downstream -- no activity, no
 * turn, no citation -- can be built out of a row nobody could decode. Widening
 * the parameter was the price of showing the hole at all: this reducer is the
 * only path from the stream into anything the page renders.
 */
export function reduceChatFrame(
  state: ChatState,
  sessionId: string,
  frame: SseChunkFrame,
): FrameReduction {
  if (isQuarantineFrame(frame)) return reduceQuarantine(state, sessionId, frame);
  if (isDegradedFrame(frame)) return reduceLiveGap(state, frame.degraded.dropped_events);
  if (isLiveFrame(frame)) return reduceLiveFrame(state, sessionId, frame);

  const safe = safeEventFromFrame(sessionId, frame);
  if (safe === null) return { state, accepted: false, duplicate: false };

  const seenKey = `${sessionId}\u0000${safe.eventId}`;
  if (state.seenEventIds[seenKey]) {
    return { state, accepted: true, duplicate: true };
  }

  let next: ChatState = {
    ...state,
    seenEventIds: { ...state.seenEventIds, [seenKey]: true },
    revision: state.revision + 1,
  };
  const localId = next.runToTurn[safe.runId];

  if (localId === undefined) {
    next = {
      ...next,
      orphanEvents: {
        ...next.orphanEvents,
        [safe.runId]: [...(next.orphanEvents[safe.runId] ?? []), safe],
      },
    };
    return { state: next, accepted: true, duplicate: false };
  }

  const turn = next.turns[localId];
  if (turn === undefined) return { state: next, accepted: true, duplicate: false };
  next = replaceTurn(next, applySafeEvent(turn, safe));
  return { state: next, accepted: true, duplicate: false };
}


/**
 * Text the model is producing, folded onto the turn it belongs to.
 *
 * Three rules, and each of them is the answer to a way this could go wrong.
 *
 * **It never enters `seenEventIds`.** That table is keyed by event id and is
 * never pruned, so feeding it a per-token event would grow it without bound
 * over one long answer. Transient events have nothing to deduplicate anyway:
 * they arrive once, live, and are never replayed.
 *
 * **It does not wait for `ModelStarted`.** That event is durable, so it arrives
 * through the replay -- up to a poll interval *after* the deltas it introduces,
 * ten seconds by default. A rule that dropped deltas until their model call was
 * known would therefore drop most of them, and would look correct in any test
 * that fed the reducer events in logical order.
 *
 * **A new model call replaces the text rather than appending to it.** Two calls
 * in one turn mean a tool round happened in between; running them together
 * would show the reader one paragraph that was never written.
 */
function reduceLiveFrame(
  state: ChatState,
  sessionId: string,
  frame: SseLiveFrame,
): FrameReduction {
  const envelope = frame.envelope;
  if (envelope.stream_id !== sessionId) {
    return { state, accepted: false, duplicate: false };
  }
  if (envelope.event_type !== "ModelDelta") {
    // Live, and not something this view renders. Accepted so the stream does
    // not treat it as a rejection, and dropped so an unknown transient type
    // cannot reach a turn by being mistaken for text.
    return { state, accepted: true, duplicate: false };
  }
  const payload = envelope.payload;
  const modelCallId = typeof payload.model_call_id === "string" ? payload.model_call_id : "";
  const text = typeof payload.text === "string" ? payload.text : "";
  if (modelCallId === "") return { state, accepted: false, duplicate: false };

  const localId = state.runToTurn[envelope.run_id];
  if (localId === undefined) return { state, accepted: true, duplicate: false };
  const turn = state.turns[localId];
  if (turn === undefined) return { state, accepted: true, duplicate: false };
  // A settled turn keeps whatever it settled on. A late delta arriving after
  // the answer was published would otherwise reopen a finished bubble.
  if (TERMINAL_TURN_PHASES.has(turn.phase)) {
    return { state, accepted: true, duplicate: false };
  }

  const previous = turn.stream;
  const carried = previous !== undefined && previous.modelCallId === modelCallId;
  const stream: LiveTextState = {
    modelCallId,
    text: (carried ? previous.text : "") + text,
    // Blank text from the server is a statement, not an absence: the shape
    // streamed a delta and the fence removed its contents (ADR-052). It stays
    // redacted until some delta of this call actually carries text.
    redacted: (carried ? previous.redacted : true) && text === "",
    dropped: carried ? previous.dropped : 0,
  };
  return {
    state: replaceTurn(bump(state, {}), { ...turn, stream }),
    accepted: true,
    duplicate: false,
  };
}

/**
 * The server could not hand this reader everything it produced.
 *
 * Recorded on whichever turn is currently streaming, because that is the text
 * the gap is in. Nothing else changes: the durable history behind it is whole,
 * and saying otherwise would make a live-view hiccup look like a damaged
 * record.
 */
function reduceLiveGap(state: ChatState, dropped: number): FrameReduction {
  if (!Number.isInteger(dropped) || dropped < 1) {
    return { state, accepted: false, duplicate: false };
  }
  const streaming = Object.values(state.turns).find(
    (turn) => turn.stream !== undefined && !TERMINAL_TURN_PHASES.has(turn.phase),
  );
  if (streaming?.stream === undefined) {
    return { state, accepted: true, duplicate: false };
  }
  return {
    state: replaceTurn(bump(state, {}), {
      ...streaming,
      stream: { ...streaming.stream, dropped: streaming.stream.dropped + dropped },
    }),
    accepted: true,
    duplicate: false,
  };
}

/**
 * Remember a position, and stop there.
 *
 * The stream already steps over the row so a reconnect does not meet it again;
 * this is the other half -- the page gets to say which position went missing
 * instead of rendering a shorter history as if it were whole.
 */
function reduceQuarantine(
  state: ChatState,
  sessionId: string,
  frame: SseQuarantineFrame,
): FrameReduction {
  const { sequence, stream_id: streamId } = frame.quarantined;
  // Re-checked rather than taken from the caller, for the same reason every
  // other frame is re-validated in this file: the reducer is the last place
  // that can refuse, and a notice is the one frame that accounts for a
  // position no event will ever fill.
  if (streamId !== sessionId || !Number.isInteger(sequence) || sequence < 1) {
    return { state, accepted: false, duplicate: false };
  }

  const current = state.quarantinedSequences[sessionId] ?? [];
  // A replay re-announces holes already passed, the same as it re-sends events
  // already applied. Counting one twice would inflate the only number this
  // notice contributes.
  if (current.includes(sequence)) return { state, accepted: true, duplicate: true };
  return {
    state: bump(state, {
      quarantinedSequences: {
        ...state.quarantinedSequences,
        [sessionId]: [...current, sequence].sort((left, right) => left - right),
      },
    }),
    accepted: true,
    duplicate: false,
  };
}

export function hasUnfinishedTurn(state: ChatState, sessionId: string): boolean {
  return (state.turnOrderBySession[sessionId] ?? []).some((localId) => {
    const turn = state.turns[localId];
    if (turn === undefined || turn.historical) return false;
    const phase = turn.phase;
    return phase === "submitting" || phase === "running";
  });
}

function turnSubmitted(state: ChatState, input: SubmitTurnInput): ChatState {
  if (state.turns[input.localId] !== undefined) return state;
  const turn: ChatTurnState = {
    ...input,
    phase: "submitting",
    activities: [],
    citations: [],
    historical: false,
  };
  return bump(state, {
    turns: { ...state.turns, [turn.localId]: turn },
    turnOrderBySession: {
      ...state.turnOrderBySession,
      [turn.sessionId]: [...(state.turnOrderBySession[turn.sessionId] ?? []), turn.localId],
    },
  });
}

function bindRun(
  state: ChatState,
  localId: string,
  runId: string,
  turnId: string | undefined,
): ChatState {
  const turn = state.turns[localId];
  if (turn === undefined || !canBindRun(state, localId, runId)) return state;

  let nextTurn: ChatTurnState = {
    ...turn,
    runId,
    phase: turn.phase === "submitting" ? "running" : turn.phase,
    ...(turnId === undefined ? {} : { turnId }),
  };
  const held = state.orphanEvents[runId] ?? [];
  for (const event of held) nextTurn = applySafeEvent(nextTurn, event);

  const orphanEvents = { ...state.orphanEvents };
  delete orphanEvents[runId];
  return bump(state, {
    turns: { ...state.turns, [localId]: nextTurn },
    runToTurn: { ...state.runToTurn, [runId]: localId },
    orphanEvents,
  });
}

function canBindRun(state: ChatState, localId: string, runId: string): boolean {
  if (!runId) return false;
  const turn = state.turns[localId];
  if (turn === undefined) return false;
  if (turn.runId !== undefined && turn.runId !== runId) return false;
  const owner = state.runToTurn[runId];
  return owner === undefined || owner === localId;
}

function failRunCorrelation(state: ChatState, localId: string): ChatState {
  const turn = state.turns[localId];
  if (turn === undefined) return state;
  const next: ChatTurnState = {
    ...turn,
    phase: "failed",
    activities: [],
    citations: [],
    error: "运行相关性冲突；为避免把其他 Turn 的结果显示在这里，已停止发布。",
  };
  delete next.answer;
  delete next.withheldReason;
  delete next.runId;
  delete next.turnId;
  const runToTurn = Object.fromEntries(
    Object.entries(state.runToTurn).filter(([, owner]) => owner !== localId),
  );
  return bump(state, {
    turns: { ...state.turns, [localId]: next },
    runToTurn,
  });
}

/**
 * Fields that arrive on a step's *opening* event and never again.
 *
 * A start and its completion share an activity key so one step is one row, and
 * the completion replaces the start. Anything the start alone carried is thrown
 * away at that moment unless it is named here.
 *
 * - `prompt_preview` rides `ModelStarted`; without it every finished model step
 *   opened onto a missing prompt.
 * - `tool_names` rides `RunStarted`, which `RunCompleted` overwrites under the
 *   same `run:` key. It is the only record of what the turn was permitted to
 *   reach, so losing it meant a finished turn could not say which tools it had
 *   -- the capability list read empty on exactly the turns anyone would look.
 * - `tool_name` rides `ToolProposed` and `ToolStarted`. `ToolFailed` carries
 *   only the call id and the error, so a failed call lost the name of the tool
 *   that failed: measured in the browser, a real `web_search` failure rendered
 *   as "工具执行失败" with nothing saying which tool, and the capability row
 *   above it reported `web_search` as never called.
 * - `argument_preview` rides `ToolProposed` alone -- it is what the call was
 *   *for*, and every later event on that call would drop it.
 */
const CARRIED_FORWARD = [
  "prompt_preview",
  "tool_names",
  "tool_name",
  "argument_preview",
] as const;

function carryForward(
  previous: ChatActivity | undefined,
  next: ChatActivity,
): ChatActivity {
  if (previous === undefined) return next;
  let payload = next.envelope.payload;
  for (const field of CARRIED_FORWARD) {
    const carried = previous.envelope.payload[field];
    if (carried === undefined || carried === "") continue;
    // The newer event wins whenever it said anything at all; this only fills
    // a hole the replacement left.
    if (payload[field] !== undefined) continue;
    payload = { ...payload, [field]: carried };
  }
  if (payload === next.envelope.payload) return next;
  // Recomputed, not patched in place. `label` and `detail` were derived from
  // the event before anything was carried across, so a payload that just
  // gained `tool_name` still carries the generic "工具执行失败" that missing
  // field produced. `activityFromEnvelope` is pure, so deriving again from the
  // completed payload is the whole fix.
  return activityFromEnvelope({ ...next.envelope, payload });
}

function applySafeEvent(turn: ChatTurnState, event: SafeRunEvent): ChatTurnState {
  const activityIndex = turn.activities.findIndex((item) => item.key === event.activity.key);
  const activities = [...turn.activities];
  if (activityIndex < 0) activities.push(event.activity);
  else activities[activityIndex] = carryForward(activities[activityIndex], event.activity);

  let next: ChatTurnState = {
    ...turn,
    activities,
    phase: turn.phase === "submitting" ? "running" : turn.phase,
  };
  if (event.terminal !== undefined) next = finalizeTurn(next, event.terminal);
  else if (["RunFailed", "RunCancelled", "ChatTurnExpired"].includes(event.kind)) {
    next = { ...next, phase: "failed", error: event.activity.detail ?? event.activity.label };
  }
  if (TERMINAL_TURN_PHASES.has(next.phase) && next.stream !== undefined) {
    // The process is over, so the process text goes. This is load-bearing on
    // the withheld path in particular: what was streamed there is exactly the
    // candidate the fence refused, and leaving it under a refusal notice would
    // publish it by other means.
    const settled = { ...next };
    delete settled.stream;
    next = settled;
  }
  return next;
}

function finalizeTurn(
  turn: ChatTurnState,
  terminal:
    | { kind: "committed"; text: string; citations: Citation[] }
    | { kind: "ungrounded"; text: string }
    | { kind: "withheld"; text: string; reason: string },
): ChatTurnState {
  // Withholding is the fail-closed result. A duplicate HTTP response must never
  // turn it back into a committed answer if the two terminal channels race.
  if (turn.phase === "withheld" && terminal.kind === "committed") return turn;
  if (terminal.kind === "withheld") {
    const next: ChatTurnState = {
      ...turn,
      phase: "withheld",
      answer: terminal.text,
      citations: [],
      withheldReason: terminal.reason,
    };
    delete next.error;
    return next;
  }
  if (terminal.kind === "ungrounded") {
    // Committed, because it is a published answer -- but marked, and with no
    // citations to offer. It never retrieved, so an empty list here is the
    // whole truth rather than a gap.
    const ungrounded: ChatTurnState = {
      ...turn,
      phase: "committed",
      answer: terminal.text,
      citations: [],
      grounded: false,
    };
    delete ungrounded.error;
    delete ungrounded.withheldReason;
    return ungrounded;
  }
  const next: ChatTurnState = {
    ...turn,
    phase: "committed",
    answer: terminal.text,
    citations: terminal.citations,
    grounded: true,
  };
  delete next.error;
  delete next.withheldReason;
  return next;
}

function safeEventFromFrame(sessionId: string, frame: SseFrame): SafeRunEvent | null {
  const envelope = frame.envelope;
  if (
    frame.id === null ||
    !frame.id ||
    envelope.stream_id !== sessionId ||
    envelope.durability !== "durable" ||
    envelope.sequence === null ||
    !Number.isInteger(envelope.sequence) ||
    envelope.sequence < 1 ||
    !envelope.event_id ||
    !envelope.run_id ||
    frame.event !== envelope.event_type ||
    envelope.payload.kind !== envelope.event_type
  ) {
    return null;
  }

  const kind = envelope.event_type;
  const activity = activityFromEnvelope(envelope);
  if (kind === "AnswerCommitted") {
    const text = stringField(envelope.payload, "text");
    if (text === null) return null;
    const citations = citationsField(envelope.payload.citations);
    if (citations === null) return null;
    return {
      eventId: envelope.event_id,
      runId: envelope.run_id,
      kind,
      timestamp: envelope.timestamp,
      activity,
      terminal: { kind: "committed", text, citations },
    };
  }
  if (kind === "UngroundedAnswerCommitted") {
    const text = stringField(envelope.payload, "text");
    if (text === null) return null;
    return {
      eventId: envelope.event_id,
      runId: envelope.run_id,
      kind,
      timestamp: envelope.timestamp,
      activity,
      terminal: { kind: "ungrounded", text },
    };
  }
  if (kind === "AnswerWithheld") {
    const text = stringField(envelope.payload, "text");
    if (text === null) return null;
    return {
      eventId: envelope.event_id,
      runId: envelope.run_id,
      kind,
      timestamp: envelope.timestamp,
      activity,
      terminal: {
        kind: "withheld",
        text,
        reason: stringField(envelope.payload, "reason_code") ?? "sources_changed",
      },
    };
  }
  return {
    eventId: envelope.event_id,
    runId: envelope.run_id,
    kind,
    timestamp: envelope.timestamp,
    activity,
  };
}

/**
 * The event, minus anything the release fence owns.
 *
 * Chat publishes an answer only through `AnswerCommitted`, `AnswerWithheld` or
 * the synchronous `AskResponse`. `ModelCompleted.text` and `ModelDelta.text`
 * are the *candidate* -- what the model produced before sources were
 * re-verified -- and a step a reader can open is still a place the reader
 * reads. Keeping them here would let a withheld answer be read anyway, which is
 * the one thing the fence exists to prevent, so they are dropped on the way
 * into state rather than hidden on the way out.
 *
 * The prompt is not dropped: it is what the model was *given*, not what it
 * produced. Note that with `chat.retrieval_shape = "agentic"` a later step's
 * prompt embeds earlier assistant turns, so a deployment that turns on
 * `runtime.record_step_inputs` *and* runs the agentic shape does surface
 * intermediate model text here. That is the deployment's choice; the fixed and
 * routed shapes make one model call and cannot.
 */
function withoutCandidateText(envelope: EventEnvelope): EventEnvelope {
  if (envelope.event_type !== "ModelCompleted" && envelope.event_type !== "ModelDelta") {
    return envelope;
  }
  const redacted = { ...envelope.payload };
  delete redacted.text;
  return { ...envelope, payload: redacted };
}

function activityFromEnvelope(envelope: EventEnvelope): ChatActivity {
  const payload = envelope.payload;
  const kind = envelope.event_type;
  const base = {
    eventId: envelope.event_id,
    kind,
    timestamp: envelope.timestamp,
    envelope: withoutCandidateText(envelope),
  };
  if (kind === "RunStarted") {
    return { ...base, key: `run:${envelope.run_id}`, label: "开始运行", state: "running" };
  }
  if (kind === "ContextBuilt") {
    const chunks = numberField(payload, "chunk_count") ?? 0;
    const tokens = numberField(payload, "token_estimate") ?? 0;
    return {
      ...base,
      key: envelope.event_id,
      label: "检索上下文",
      state: "complete",
      detail: `${chunks} 个片段 · ${tokens} tokens`,
    };
  }
  if (kind === "RetrievalRejected") {
    // The turn searched and chose not to answer from what came back. Saying
    // only "未检索" would be false, and saying nothing leaves the reader
    // unable to tell this from a turn that never looked.
    const chunks = numberField(payload, "chunk_count") ?? 0;
    const relevance = numberField(payload, "top_relevance");
    return {
      ...base,
      key: envelope.event_id,
      label: "检索结果未被采用",
      state: "info",
      detail:
        chunks === 0
          ? "没有可用的资料"
          : relevance === null
            ? `${chunks} 个片段 · 相关度未测出`
            : `${chunks} 个片段 · 最高相关度 ${relevance.toFixed(2)}`,
    };
  }
  if (kind === "ModelStarted") {
    const modelCallId = stringField(payload, "model_call_id") ?? envelope.event_id;
    const modelId = stringField(payload, "model_id");
    return {
      ...base,
      key: `model:${modelCallId}`,
      label: "模型生成",
      state: "running",
      ...(modelId === null || !modelId ? {} : { detail: modelId }),
    };
  }
  if (kind === "ModelCompleted") {
    // Deliberately never copy payload.text. Provider completion is not the
    // publication boundary, even when a future server accidentally sends it.
    const modelCallId = stringField(payload, "model_call_id") ?? envelope.event_id;
    const usage = objectField(payload, "usage");
    const total =
      (usage === null ? 0 : numberField(usage, "input_tokens") ?? 0) +
      (usage === null ? 0 : numberField(usage, "output_tokens") ?? 0);
    const reason = stringField(payload, "finish_reason") ?? "completed";
    return {
      ...base,
      key: `model:${modelCallId}`,
      label: "模型完成",
      state: "complete",
      detail: `${total} tokens · ${reason}`,
    };
  }
  if (kind === "ToolProposed") {
    const callId = stringField(payload, "tool_call_id") ?? envelope.event_id;
    const salient = salientArgument(payload);
    return {
      ...base,
      key: `tool:${callId}`,
      label: stringField(payload, "tool_name") ?? "工具调用",
      state: "running",
      // What the call was *for*, when the deployment records arguments. The
      // byte count it replaces was technically true and never once answered
      // the question a reader opens this line to ask -- "搜的什么？". Falls
      // back to the size only when `record_step_inputs` is off, where the
      // arguments genuinely are not on the event.
      detail: salient ?? `${numberField(payload, "argument_bytes") ?? 0} B 参数`,
    };
  }
  if (kind === "ToolStarted") {
    // Same `tool:` key as the proposal it belongs to, so one call stays one
    // row. Without a branch here it fell through to the generic tail, took
    // `event_id` as its key and rendered a second line reading "ToolStarted"
    // -- the raw event name, beside a row that already said the same thing.
    const callId = stringField(payload, "tool_call_id") ?? envelope.event_id;
    return {
      ...base,
      key: `tool:${callId}`,
      label: stringField(payload, "tool_name") ?? "工具调用",
      state: "running",
      detail: "执行中",
    };
  }
  if (kind === "PermissionResolved") {
    const callId = stringField(payload, "tool_call_id") ?? envelope.event_id;
    const denied = stringField(payload, "effect") === "deny";
    return {
      ...base,
      key: `tool:${callId}`,
      label: denied ? "工具权限被拒绝" : "工具权限已确认",
      state: denied ? "failed" : "running",
      ...(stringField(payload, "reason_code") === null
        ? {}
        : { detail: stringField(payload, "reason_code") ?? "" }),
    };
  }
  if (kind === "ToolCompleted" || kind === "ToolFailed") {
    const callId = stringField(payload, "tool_call_id") ?? envelope.event_id;
    const failed = kind === "ToolFailed";
    // The tool's own name, not "工具执行完成". These rows replace the proposal
    // they share a key with, so a finished turn showed three generic sentences
    // where the reader wanted to see which tools ran.
    const toolName = stringField(payload, "tool_name");
    return {
      ...base,
      key: `tool:${callId}`,
      label: toolName ?? (failed ? "工具执行失败" : "工具执行完成"),
      state: failed ? "failed" : "complete",
      detail: failed
        ? // The message, not the code. `provider_unavailable` is the same code
          // for "no provider configured" and "found 19 pages, read none of
          // them" -- and the second one is the whole reason a reader is
          // looking at this line.
          errorSummary(payload) ?? "失败"
        : `${numberField(payload, "duration_ms") ?? 0} ms`,
    };
  }
  if (kind === "AnswerCommitted") {
    return { ...base, key: "answer", label: "答案已安全发布", state: "complete" };
  }
  if (kind === "UngroundedAnswerCommitted") {
    // Deliberately not "已安全发布": no answer released down this path passed a
    // source check, whether it skipped retrieval or retrieved and found nothing
    // relevant enough. Reusing that label would be the timeline making the claim
    // the separate event type exists to avoid. Which of the two happened is a
    // property of the turn rather than of this event, so the answer block says
    // it and this line stays true for both.
    return { ...base, key: "answer", label: "答案已发布（未经证据核实）", state: "complete" };
  }
  if (kind === "AnswerWithheld") {
    return {
      ...base,
      key: "answer",
      label: "答案已阻止发布",
      state: "waiting",
      detail: stringField(payload, "reason_code") ?? "sources_changed",
    };
  }
  if (kind === "RunCompleted") {
    return { ...base, key: `run:${envelope.run_id}`, label: "运行完成", state: "complete" };
  }
  if (kind === "RunFailed" || kind === "ChatTurnExpired") {
    // Neither of these carries `reason_code`. `RunFailed` carries `error`, and
    // `ChatTurnExpired` carries `error_code` (`domain/events.py`) -- only
    // `RunCancelled` below has `reason_code`. Sharing that branch meant reading
    // a field these events never have, so the line fell back to the raw event
    // name: a reader was shown "RunFailed" and not one word about why.
    // `errorSummary` reads both shapes; the code is a category and the message
    // is the incident, which is why the message wins when there is one.
    const summary = errorSummary(payload);
    return {
      ...base,
      key: `run:${envelope.run_id}`,
      label: kind === "RunFailed" ? "运行失败" : "本轮已过期",
      state: "failed",
      // Left absent when the server sent neither message nor code, so the
      // turn's error line falls back to this label instead of an empty string.
      ...(summary === null || summary === "" ? {} : { detail: summary }),
    };
  }
  if (kind === "RunCancelled") {
    const reason = stringField(payload, "reason_code");
    return {
      ...base,
      key: `run:${envelope.run_id}`,
      label: "运行已取消",
      state: "failed",
      ...(reason === null ? {} : { detail: reason }),
    };
  }
  return { ...base, key: envelope.event_id, label: kind, state: "info" };
}

function historyLoaded(state: ChatState, sessionId: string, messages: MessageView[]): ChatState {
  const session = state.sessions[sessionId];
  if (session === undefined) return state;
  // A live turn contains richer run correlation. Never replace it with the
  // lossy history projection, which has neither run ids nor citations.
  if ((state.turnOrderBySession[sessionId] ?? []).length > 0) {
    return updateSessionHistory(state, sessionId, "loaded");
  }

  const turns = { ...state.turns };
  const order: string[] = [];
  let current: ChatTurnState | null = null;
  messages.forEach((message, index) => {
    if (message.role === "user") {
      const localId = `history:${sessionId}:${index}`;
      current = {
        localId,
        sessionId,
        question: message.text,
        answerMode: session.answerMode,
        knowledgeBaseId: session.knowledgeBaseId,
        topK: 8,
        idempotencyKey: "history",
        submittedAt: session.createdAt,
        phase: "running",
        activities: [],
        citations: [],
        historical: true,
      };
      turns[localId] = current;
      order.push(localId);
      return;
    }
    if (message.role === "assistant" && current !== null) {
      current = {
        ...current,
        phase: "committed",
        answer: message.text,
      };
      turns[current.localId] = current;
    }
  });

  return bump(state, {
    sessions: {
      ...state.sessions,
      [sessionId]: { ...session, history: "loaded" },
    },
    turns,
    turnOrderBySession: { ...state.turnOrderBySession, [sessionId]: order },
  });
}

function updateSessionHistory(
  state: ChatState,
  sessionId: string,
  history: ChatSessionState["history"],
  error?: string,
): ChatState {
  const session = state.sessions[sessionId];
  if (session === undefined) return state;
  const next: ChatSessionState = { ...session, history };
  if (error === undefined) delete next.historyError;
  else next.historyError = error;
  return bump(state, { sessions: { ...state.sessions, [sessionId]: next } });
}

function replaceTurn(state: ChatState, turn: ChatTurnState): ChatState {
  if (state.turns[turn.localId] === turn) return state;
  return bump(state, { turns: { ...state.turns, [turn.localId]: turn } });
}

/**
 * The same record without one key.
 *
 * A named helper rather than `const { [key]: _drop, ...rest } = record`: that
 * idiom binds a variable whose only purpose is to be discarded, which is a
 * lint error here and reads, to anyone who has not met the trick, like a bug.
 */
function without<T>(record: Record<string, T>, key: string): Record<string, T> {
  return Object.fromEntries(
    Object.entries(record).filter(([held]) => held !== key),
  );
}

function bump(state: ChatState, patch: Partial<ChatState>): ChatState {
  return { ...state, ...patch, revision: state.revision + 1 };
}

/**
 * Everything a turn was allowed to reach, from the run's own opening event.
 *
 * Found by the field rather than by `kind`, because the row that holds it is
 * whichever run event arrived last: `RunStarted` and `RunCompleted` share a key,
 * so on a finished turn the surviving activity says `RunCompleted` and carries
 * the names only because `carryForward` moved them across.
 */
export function turnToolNames(activities: readonly ChatActivity[]): string[] {
  for (const activity of activities) {
    const names = activity.envelope.payload.tool_names;
    if (!Array.isArray(names)) continue;
    return names.filter((name): name is string => typeof name === "string");
  }
  return [];
}

/** Which tools this turn actually called, in the order it first called them. */
export function calledToolNames(activities: readonly ChatActivity[]): string[] {
  const called: string[] = [];
  for (const activity of activities) {
    if (!activity.key.startsWith("tool:")) continue;
    const name = stringField(activity.envelope.payload, "tool_name");
    if (name !== null && !called.includes(name)) called.push(name);
  }
  return called;
}

/**
 * The one argument worth putting on a collapsed line.
 *
 * Tool arguments are JSON of arbitrary shape, and the point here is not to
 * render them -- opening the step already does that faithfully. It is to answer
 * "what was this call for" in the width of a summary. So: a small set of keys
 * that carry a call's subject across the tools this system has, first match
 * wins, and nothing at all when none of them fit rather than a truncated blob
 * of braces that reads as noise.
 */
const SALIENT_KEYS = ["query", "url", "question", "path", "name"] as const;
const SALIENT_MAX = 60;

function salientArgument(payload: Record<string, unknown>): string | null {
  const preview = stringField(payload, "argument_preview");
  if (preview === null || preview === "") return null;
  let parsed: unknown;
  try {
    parsed = JSON.parse(preview);
  } catch {
    return null;
  }
  if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
    return null;
  }
  const fields = parsed as Record<string, unknown>;
  for (const key of SALIENT_KEYS) {
    const value = fields[key];
    if (typeof value !== "string" || value.trim() === "") continue;
    const collapsed = value.replace(/\s+/g, " ").trim();
    return collapsed.length <= SALIENT_MAX
      ? collapsed
      : `${collapsed.slice(0, SALIENT_MAX - 1)}…`;
  }
  return null;
}

/** A failed tool's message, trimmed to a line; its code when there is none. */
function errorSummary(payload: Record<string, unknown>): string | null {
  const error = objectField(payload, "error");
  const message = error === null ? null : stringField(error, "message");
  const code =
    (error === null ? null : stringField(error, "code")) ??
    stringField(payload, "error_code");
  if (message === null || message === "") return code;
  const collapsed = message.replace(/\s+/g, " ").trim();
  return collapsed.length <= 90 ? collapsed : `${collapsed.slice(0, 89)}…`;
}

/**
 * The part of a locator that can actually reach the reader.
 *
 * Only `page` and `paragraph` are read. `char_start` / `char_end` exist on the
 * domain model and are computed at ingestion, but the index stores a chunk's
 * `ordinal` and `page` and nothing else (`ports/vector_index.py`), so they can
 * never arrive on a citation -- parsing them would add a field that renders
 * empty forever.
 *
 * Values outside the ranges the server itself enforces (`page >= 1`,
 * `paragraph >= 0`) are dropped rather than shown. 第 0 页 is not a page a
 * reader can turn to, and a locator that points nowhere is worse than a
 * citation that admits it has no position.
 */
function sourceLocator(raw: Record<string, unknown>): SourceLocator {
  const page = numberField(raw, "page");
  const paragraph = numberField(raw, "paragraph");
  return {
    ...(page === null || !Number.isInteger(page) || page < 1 ? {} : { page }),
    ...(paragraph === null || !Number.isInteger(paragraph) || paragraph < 0
      ? {}
      : { paragraph }),
  };
}

function stringField(object: Record<string, unknown>, field: string): string | null {
  const value = object[field];
  return typeof value === "string" ? value : null;
}

function numberField(object: Record<string, unknown>, field: string): number | null {
  const value = object[field];
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function objectField(
  object: Record<string, unknown>,
  field: string,
): Record<string, unknown> | null {
  const value = object[field];
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function citationsField(value: unknown): Citation[] | null {
  if (value === undefined) return [];
  if (!Array.isArray(value)) return null;
  const citations: Citation[] = [];
  for (const candidate of value) {
    if (typeof candidate !== "object" || candidate === null || Array.isArray(candidate)) {
      return null;
    }
    const item = candidate as Record<string, unknown>;
    const chunkId = stringField(item, "chunk_id");
    const documentId = stringField(item, "document_id");
    const version = stringField(item, "document_version");
    if (chunkId === null || documentId === null || version === null) return null;
    const raw = objectField(item, "locator");
    citations.push({
      chunk_id: chunkId,
      document_id: documentId,
      document_version: version,
      locator: raw === null ? {} : sourceLocator(raw),
      ...(typeof item.quote === "string" || item.quote === null
        ? { quote: item.quote }
        : {}),
    });
  }
  return citations;
}
