import { askChat, getChatHistory } from "../../api/client";
import type { AskResponse, LocalChatSession, PrincipalIdentity } from "../../api/types";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ChatRuntime } from "./runtime";
import { streamChatSession } from "./sessionStream";

vi.mock("../../api/client", () => ({
  ApiError: class MockApiError extends Error {
    readonly status = 500;
    readonly detail: unknown = undefined;
  },
  askChat: vi.fn(),
  getChatHistory: vi.fn(),
  newIdempotencyKey: vi.fn(() => "chat:generated-key"),
}));

vi.mock("./sessionStream", () => ({
  streamChatSession: vi.fn(),
}));

const IDENTITY: PrincipalIdentity = {
  tenantId: "tenant_a",
  principalId: "alice",
  scopes: ["knowledge:read"],
};

const SESSION: LocalChatSession = {
  sessionId: "ses_1",
  title: "Local chat",
  knowledgeBaseId: "kb_main",
  createdAt: "2026-08-02T12:00:00Z",
  updatedAt: "2026-08-02T12:00:00Z",
};

const RELEASED_RESPONSE: AskResponse = {
  answer: "Safely released answer",
  citations: [],
  withheld: false,
  run_id: "run_1",
  turn_id: "turn_1",
};

beforeEach(() => {
  vi.clearAllMocks();
  window.localStorage.clear();
  vi.mocked(getChatHistory).mockResolvedValue({ messages: [] });
  vi.mocked(streamChatSession).mockResolvedValue();
});

describe("Chat runtime request ownership", () => {
  it("keeps the Ask POST alive when the routed view releases its stream lease", async () => {
    let resolveAsk: ((response: AskResponse) => void) | undefined;
    vi.mocked(askChat).mockReturnValue(
      new Promise<AskResponse>((resolve) => {
        resolveAsk = resolve;
      }),
    );

    let streamSignal: AbortSignal | undefined;
    vi.mocked(streamChatSession).mockImplementation((options) => {
      streamSignal = options.signal;
      return new Promise<void>((resolve) => {
        options.signal.addEventListener("abort", () => resolve(), { once: true });
      });
    });

    const runtime = runtimeWithSession();
    const releaseView = runtime.retainSessionStream(SESSION.sessionId);
    const localId = runtime.startAsk({
      sessionId: SESSION.sessionId,
      question: "What changed?",
      knowledgeBaseId: SESSION.knowledgeBaseId,
    });

    releaseView();

    expect(streamSignal?.aborted).toBe(false);
    expect(vi.mocked(askChat).mock.calls[0]).toHaveLength(4);

    if (resolveAsk === undefined) throw new Error("Ask mock did not start");
    resolveAsk(RELEASED_RESPONSE);
    await runtime.waitForAsk(localId);

    expect(runtime.getSnapshot().turns[localId]?.phase).toBe("committed");
    expect(streamSignal?.aborted).toBe(true);
  });

  it("reuses the logical turn's idempotency key after a failed Ask", async () => {
    vi.mocked(askChat)
      .mockRejectedValueOnce(new Error("connection reset"))
      .mockResolvedValueOnce(RELEASED_RESPONSE);

    const runtime = runtimeWithSession();
    const localId = runtime.startAsk({
      sessionId: SESSION.sessionId,
      question: "What changed?",
      knowledgeBaseId: SESSION.knowledgeBaseId,
    });
    await runtime.waitForAsk(localId);
    expect(runtime.getSnapshot().turns[localId]?.phase).toBe("failed");

    runtime.retryAsk(localId);
    await runtime.waitForAsk(localId);

    const firstKey = vi.mocked(askChat).mock.calls[0]?.[3];
    const retryKey = vi.mocked(askChat).mock.calls[1]?.[3];
    expect(firstKey).toBe("chat:generated-key");
    expect(retryKey).toBe(firstKey);
    expect(runtime.getSnapshot().turns[localId]?.phase).toBe("committed");
  });
});

function runtimeWithSession(): ChatRuntime {
  const runtime = new ChatRuntime(IDENTITY);
  runtime.addLocalSession(SESSION);
  return runtime;
}
