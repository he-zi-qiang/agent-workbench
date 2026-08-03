import type {
  AskResponse,
  Citation,
  EventEnvelope,
  LocalChatSession,
  MessageView,
} from "../../api/types";
import type { SseFrame } from "../../api/sse";

export type ChatTurnPhase =
  | "submitting"
  | "running"
  | "committed"
  | "withheld"
  | "failed";

export type ChatActivityState = "running" | "complete" | "waiting" | "failed" | "info";

export interface ChatActivity {
  key: string;
  eventId: string;
  kind: string;
  label: string;
  state: ChatActivityState;
  timestamp: string;
  detail?: string;
}

export interface ChatTurnState {
  localId: string;
  sessionId: string;
  question: string;
  knowledgeBaseId: string;
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
}

export type ChatConnectionState = "idle" | "connecting" | "connected" | "retrying" | "unavailable";

export interface ChatSessionState extends LocalChatSession {
  connection: ChatConnectionState;
  history: "idle" | "loading" | "loaded" | "failed";
  connectionError?: string;
  historyError?: string;
}

export interface SafeRunEvent {
  eventId: string;
  runId: string;
  kind: string;
  timestamp: string;
  activity: ChatActivity;
  terminal?:
    | { kind: "committed"; text: string; citations: Citation[] }
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
  revision: number;
}

export interface SubmitTurnInput {
  localId: string;
  sessionId: string;
  question: string;
  knowledgeBaseId: string;
  topK: number;
  idempotencyKey: string;
  submittedAt: string;
}

export type ChatAction =
  | { type: "sessionAdded"; session: LocalChatSession }
  | { type: "sessionUpdated"; sessionId: string; knowledgeBaseId: string; updatedAt: string }
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
  | { type: "askRejected"; localId: string; error: string; runId?: string };

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
    case "sessionUpdated": {
      const current = state.sessions[action.sessionId];
      if (current === undefined) return state;
      return bump(state, {
        sessions: {
          ...state.sessions,
          [action.sessionId]: {
            ...current,
            knowledgeBaseId: action.knowledgeBaseId,
            updatedAt: action.updatedAt,
          },
        },
      });
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

export function reduceChatFrame(
  state: ChatState,
  sessionId: string,
  frame: SseFrame,
): FrameReduction {
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

function applySafeEvent(turn: ChatTurnState, event: SafeRunEvent): ChatTurnState {
  const activityIndex = turn.activities.findIndex((item) => item.key === event.activity.key);
  const activities = [...turn.activities];
  if (activityIndex < 0) activities.push(event.activity);
  else activities[activityIndex] = event.activity;

  let next: ChatTurnState = {
    ...turn,
    activities,
    phase: turn.phase === "submitting" ? "running" : turn.phase,
  };
  if (event.terminal !== undefined) next = finalizeTurn(next, event.terminal);
  else if (["RunFailed", "RunCancelled", "ChatTurnExpired"].includes(event.kind)) {
    next = { ...next, phase: "failed", error: event.activity.detail ?? event.activity.label };
  }
  return next;
}

function finalizeTurn(
  turn: ChatTurnState,
  terminal:
    | { kind: "committed"; text: string; citations: Citation[] }
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
  const next: ChatTurnState = {
    ...turn,
    phase: "committed",
    answer: terminal.text,
    citations: terminal.citations,
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

function activityFromEnvelope(envelope: EventEnvelope): ChatActivity {
  const payload = envelope.payload;
  const kind = envelope.event_type;
  const base = {
    eventId: envelope.event_id,
    kind,
    timestamp: envelope.timestamp,
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
    return {
      ...base,
      key: `tool:${callId}`,
      label: stringField(payload, "tool_name") ?? "工具调用",
      state: "running",
      detail: `${numberField(payload, "argument_bytes") ?? 0} B 参数`,
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
    return {
      ...base,
      key: `tool:${callId}`,
      label: failed ? "工具执行失败" : "工具执行完成",
      state: failed ? "failed" : "complete",
      detail: failed
        ? stringField(payload, "error_code") ?? "failed"
        : `${numberField(payload, "duration_ms") ?? 0} ms`,
    };
  }
  if (kind === "AnswerCommitted") {
    return { ...base, key: "answer", label: "答案已安全发布", state: "complete" };
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
  if (kind === "RunFailed" || kind === "RunCancelled" || kind === "ChatTurnExpired") {
    return {
      ...base,
      key: `run:${envelope.run_id}`,
      label: kind,
      state: "failed",
      ...(stringField(payload, "reason_code") === null
        ? {}
        : { detail: stringField(payload, "reason_code") ?? "" }),
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

function bump(state: ChatState, patch: Partial<ChatState>): ChatState {
  return { ...state, ...patch, revision: state.revision + 1 };
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
    const locator = objectField(item, "locator") ?? {};
    citations.push({
      chunk_id: chunkId,
      document_id: documentId,
      document_version: version,
      locator,
      ...(typeof item.quote === "string" || item.quote === null
        ? { quote: item.quote }
        : {}),
    });
  }
  return citations;
}
