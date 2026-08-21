import {
  ApiError,
  askChat,
  deleteChatSession,
  getChatHistory,
  newIdempotencyKey,
} from "../../api/client";
import type {
  ChatAnswerMode,
  ChatSessionView,
  LocalChatSession,
  PrincipalIdentity,
} from "../../api/types";
import {
  chatReducer,
  hasUnfinishedTurn,
  initialChatState,
  reduceChatFrame,
  type ChatAction,
  type ChatState,
} from "./model";
import { streamSession, type FrameAcceptance } from "../../api/sessionStream";
import {
  forgetChatCursor,
  identityStorageKey,
  loadChatCursor,
  loadLocalSessions,
  saveChatCursor,
  saveLocalSessions,
} from "./storage";

interface ConnectionLease {
  controller: AbortController | null;
  retainedByViews: number;
  retainedByAsks: number;
}

interface StartAskInput {
  sessionId: string;
  question: string;
  answerMode: ChatAnswerMode;
  knowledgeBaseId: string | null;
  topK?: number;
}

type Listener = () => void;

export class ChatRuntime {
  readonly identity: PrincipalIdentity;
  private state: ChatState;
  private readonly listeners = new Set<Listener>();
  private readonly connections = new Map<string, ConnectionLease>();
  private readonly inFlightAsks = new Map<string, Promise<void>>();
  private readonly historyLoads = new Map<string, Promise<void>>();

  constructor(identity: PrincipalIdentity) {
    this.identity = {
      tenantId: identity.tenantId,
      principalId: identity.principalId,
      scopes: [...identity.scopes],
    };
    this.state = initialChatState(loadLocalSessions(this.identity));
  }

  readonly getSnapshot = (): ChatState => this.state;

  readonly subscribe = (listener: Listener): (() => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };

  addLocalSession(session: LocalChatSession): void {
    this.dispatch({ type: "sessionAdded", session });
    this.dispatch({ type: "historyLoaded", sessionId: session.sessionId, messages: [] });
    this.persistSessions();
  }

  updateSessionSource(
    sessionId: string,
    answerMode: ChatAnswerMode,
    knowledgeBaseId: string | null,
  ): void {
    this.dispatch({
      type: "sessionUpdated",
      sessionId,
      answerMode,
      knowledgeBaseId,
      updatedAt: new Date().toISOString(),
    });
    this.persistSessions();
  }

  /**
   * Apply a title the server has already accepted to this device's richer
   * projection (source choice, stream cursor and cached history stay local).
   */
  renameSession(sessionId: string, title: string): void {
    const trimmed = title.trim();
    if (trimmed === "") return;
    this.dispatch({ type: "sessionRenamed", sessionId, title: trimmed });
    this.persistSessions();
  }

  reconcileServerSessions(sessions: ChatSessionView[]): void {
    this.dispatch({ type: "sessionsReconciled", sessions });
    this.persistSessions();
  }

  retainSessionStream(sessionId: string): () => void {
    const lease = this.connectionLease(sessionId);
    lease.retainedByViews += 1;
    this.ensureConnection(sessionId, lease);
    let released = false;
    return () => {
      if (released) return;
      released = true;
      lease.retainedByViews = Math.max(0, lease.retainedByViews - 1);
      this.stopUnusedConnection(sessionId, lease);
    };
  }

  reconnectSessionStream(sessionId: string): void {
    const lease = this.connectionLease(sessionId);
    lease.controller?.abort();
    lease.controller = null;
    this.ensureConnection(sessionId, lease);
  }

  /**
   * Forget one conversation here, and on the server.
   *
   * The server is asked first and its refusal stops the whole thing. Keeping
   * the stream alive until that succeeds matters: otherwise a transient
   * DELETE failure would leave a visible session with a dead lease and no
   * effect capable of retaining it again. Once deletion is accepted we abort
   * the stream before dropping reducer state, so no later frame can recreate
   * activity for a session the UI has forgotten.
   *
   * The `404` is the exception. A session recorded in this browser but never
   * created on the server -- opened, never asked -- has nothing to delete, and
   * insisting on the server's agreement would make that row undeletable.
   */
  async removeSession(sessionId: string): Promise<void> {
    try {
      await deleteChatSession(this.identity, sessionId);
    } catch (error: unknown) {
      if (!(error instanceof ApiError) || error.status !== 404) throw error;
    }

    const lease = this.connections.get(sessionId);
    lease?.controller?.abort();
    this.connections.delete(sessionId);
    forgetChatCursor(this.identity, sessionId);
    this.dispatch({ type: "sessionRemoved", sessionId });
    this.persistSessions();
  }

  startAsk(input: StartAskInput): string {
    if (hasUnfinishedTurn(this.state, input.sessionId)) {
      throw new Error("这个会话已有一个未完成的问题");
    }
    const localId = `turn:${crypto.randomUUID()}`;
    const submittedAt = new Date().toISOString();
    this.dispatch({
      type: "turnSubmitted",
      input: {
        localId,
        sessionId: input.sessionId,
        question: input.question,
        answerMode: input.answerMode,
        knowledgeBaseId: input.knowledgeBaseId,
        topK: input.topK ?? 8,
        idempotencyKey: newIdempotencyKey("chat"),
        submittedAt,
      },
    });
    this.dispatch({
      type: "sessionUpdated",
      sessionId: input.sessionId,
      answerMode: input.answerMode,
      knowledgeBaseId: input.knowledgeBaseId,
      updatedAt: submittedAt,
    });
    this.persistSessions();
    this.launchAsk(localId);
    return localId;
  }

  retryAsk(localId: string): void {
    const turn = this.state.turns[localId];
    if (turn === undefined || turn.historical || turn.phase !== "failed") return;
    if (this.inFlightAsks.has(localId)) return;
    this.dispatch({ type: "turnRetrying", localId });
    this.launchAsk(localId);
  }

  async waitForAsk(localId: string): Promise<void> {
    await this.inFlightAsks.get(localId);
  }

  ensureHistory(sessionId: string): Promise<void> {
    const session = this.state.sessions[sessionId];
    if (session === undefined || session.history === "loaded") return Promise.resolve();
    const existing = this.historyLoads.get(sessionId);
    if (existing !== undefined) return existing;

    this.dispatch({ type: "historyLoading", sessionId });
    const request = getChatHistory(this.identity, sessionId)
      .then((history) => {
        this.dispatch({ type: "historyLoaded", sessionId, messages: history.messages });
      })
      .catch((error: unknown) => {
        this.dispatch({ type: "historyFailed", sessionId, error: errorMessage(error) });
      })
      .finally(() => {
        if (this.historyLoads.get(sessionId) === request) this.historyLoads.delete(sessionId);
      });
    this.historyLoads.set(sessionId, request);
    return request;
  }

  private launchAsk(localId: string): void {
    const turn = this.state.turns[localId];
    if (turn === undefined || this.inFlightAsks.has(localId)) return;

    const lease = this.connectionLease(turn.sessionId);
    lease.retainedByAsks += 1;
    // This call starts the authenticated SSE fetch synchronously before the Ask
    // fetch below. The stream belongs to the runtime, not to the routed page.
    this.ensureConnection(turn.sessionId, lease);

    const request = this.performAsk(localId).finally(() => {
      if (this.inFlightAsks.get(localId) === request) this.inFlightAsks.delete(localId);
      lease.retainedByAsks = Math.max(0, lease.retainedByAsks - 1);
      this.stopUnusedConnection(turn.sessionId, lease);
    });
    this.inFlightAsks.set(localId, request);
  }

  private async performAsk(localId: string): Promise<void> {
    const turn = this.state.turns[localId];
    if (turn === undefined) return;
    try {
      const response = await askChat(
        this.identity,
        turn.sessionId,
        {
          question: turn.question,
          answerMode: turn.answerMode,
          knowledgeBaseId: turn.knowledgeBaseId,
          topK: turn.topK,
        },
        // The key lives on the logical turn. Retry never generates another one.
        turn.idempotencyKey,
      );
      this.dispatch({ type: "askResolved", localId, response });
    } catch (error) {
      const runId = runIdFromError(error);
      this.dispatch({
        type: "askRejected",
        localId,
        error: errorMessage(error),
        ...(runId === undefined ? {} : { runId }),
      });
    }
  }

  private connectionLease(sessionId: string): ConnectionLease {
    const current = this.connections.get(sessionId);
    if (current !== undefined) return current;
    const lease: ConnectionLease = {
      controller: null,
      retainedByViews: 0,
      retainedByAsks: 0,
    };
    this.connections.set(sessionId, lease);
    return lease;
  }

  private ensureConnection(sessionId: string, lease: ConnectionLease): void {
    if (lease.controller !== null || this.state.sessions[sessionId] === undefined) return;
    const controller = new AbortController();
    lease.controller = controller;
    const initialCursor = loadChatCursor(this.identity, sessionId);
    void streamSession({
      eventsPath: "/v1/chat/sessions",
      identity: this.identity,
      sessionId,
      initialCursor,
      signal: controller.signal,
      onFrame: (frame): FrameAcceptance => {
        const result = reduceChatFrame(this.state, sessionId, frame);
        if (result.state !== this.state) {
          this.state = result.state;
          this.emit();
        }
        if (!result.accepted) return "rejected";
        return result.duplicate ? "duplicate" : "accepted";
      },
      onCursor: (cursor) => {
        saveChatCursor(this.identity, sessionId, cursor);
        // Also into state: the saved copy is what a reload resumes from, and
        // the console shows where the *live* stream is.
        this.dispatch({ type: "cursorAdvanced", sessionId, cursor });
      },
      onConnectionChange: (connection, error) => {
        this.dispatch({
          type: "connectionChanged",
          sessionId,
          connection,
          ...(error === undefined ? {} : { error }),
        });
      },
    }).finally(() => {
      if (lease.controller === controller) lease.controller = null;
    });
  }

  private stopUnusedConnection(sessionId: string, lease: ConnectionLease): void {
    if (lease.retainedByViews > 0 || lease.retainedByAsks > 0) return;
    lease.controller?.abort();
    lease.controller = null;
    this.connections.delete(sessionId);
    this.dispatch({ type: "connectionChanged", sessionId, connection: "idle" });
  }

  private dispatch(action: ChatAction): void {
    const next = chatReducer(this.state, action);
    if (next === this.state) return;
    this.state = next;
    this.emit();
  }

  private persistSessions(): void {
    const sessions = this.state.sessionOrder.flatMap((sessionId) => {
      const session = this.state.sessions[sessionId];
      if (session === undefined) return [];
      return [
        {
          sessionId: session.sessionId,
          title: session.title,
          answerMode: session.answerMode,
          knowledgeBaseId: session.knowledgeBaseId,
          createdAt: session.createdAt,
          updatedAt: session.updatedAt,
        },
      ];
    });
    saveLocalSessions(this.identity, sessions);
  }

  private emit(): void {
    this.listeners.forEach((listener) => listener());
  }
}

const runtimes = new Map<string, ChatRuntime>();

export function chatRuntimeFor(identity: PrincipalIdentity): ChatRuntime {
  const key = identityStorageKey(identity);
  const current = runtimes.get(key);
  if (current !== undefined) return current;
  const runtime = new ChatRuntime(identity);
  runtimes.set(key, runtime);
  return runtime;
}

function errorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    // 两种可能，读者能分辨的那一种放前面。
    if (error.status === 404) {
      return "打不开这个对话：可能它不属于你，也可能这套部署没开对话功能。";
    }
    return `${error.message}（HTTP ${error.status}）`;
  }
  return error instanceof Error ? error.message : "请求失败";
}

function runIdFromError(error: unknown): string | undefined {
  if (!(error instanceof ApiError)) return undefined;
  const detail = error.detail;
  if (typeof detail !== "object" || detail === null || Array.isArray(detail)) return undefined;
  const runId = (detail as Record<string, unknown>).run_id;
  return typeof runId === "string" && runId ? runId : undefined;
}
