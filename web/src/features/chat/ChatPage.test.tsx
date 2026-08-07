import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { createChatSession, listKnowledgeBases } from "../../api/client";
import type { PrincipalIdentity } from "../../api/types";
import { useIdentity } from "../../app/IdentityContext";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ChatPage } from "./ChatPage";
import { initialChatState, type ChatTurnState } from "./model";
import type { ChatRuntime } from "./runtime";
import { useChatRuntime } from "./useChatRuntime";

vi.mock("../../api/client", () => ({
  createChatSession: vi.fn(),
  listKnowledgeBases: vi.fn(() => Promise.resolve({ knowledge_bases: [] })),
}));

vi.mock("../../app/IdentityContext", () => ({
  useIdentity: vi.fn(),
}));

vi.mock("./useChatRuntime", () => ({
  useChatRuntime: vi.fn(),
}));

const ALICE: PrincipalIdentity = {
  tenantId: "tenant_a",
  principalId: "alice",
  scopes: ["knowledge:read"],
};

const BOB: PrincipalIdentity = {
  tenantId: "tenant_a",
  principalId: "bob",
  scopes: ["knowledge:read"],
};

let currentIdentity = ALICE;
let aliceRuntime: ChatRuntime;
let bobRuntime: ChatRuntime;
let aliceAddLocalSession = vi.fn();
let aliceStartAsk = vi.fn();
let bobStartAsk = vi.fn();

beforeEach(() => {
  vi.clearAllMocks();
  currentIdentity = ALICE;
  aliceAddLocalSession = vi.fn();
  aliceStartAsk = vi.fn();
  bobStartAsk = vi.fn();
  aliceRuntime = fakeRuntime(aliceAddLocalSession, aliceStartAsk);
  bobRuntime = fakeRuntime(vi.fn(), bobStartAsk);
  vi.mocked(useIdentity).mockImplementation(() => ({
    identity: currentIdentity,
    updateIdentity: vi.fn(),
    editorOpen: false,
    setEditorOpen: vi.fn(),
  }));
  vi.mocked(useChatRuntime).mockImplementation((identity) => ({
    runtime: identity.principalId === ALICE.principalId ? aliceRuntime : bobRuntime,
    state: initialChatState(),
  }));
  vi.mocked(listKnowledgeBases).mockResolvedValue({ knowledge_bases: [] });
});

afterEach(() => cleanup());

describe("Chat identity boundary", () => {
  it("does not start an Ask in the old identity after Session creation resolves", async () => {
    let resolveCreate: ((response: { session_id: string }) => void) | undefined;
    vi.mocked(createChatSession).mockReturnValue(
      new Promise((resolve) => {
        resolveCreate = resolve;
      }),
    );

    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    const tree = (key: string) => (
      <QueryClientProvider client={queryClient}>
        <MemoryRouter>
          <ChatPage key={key} />
        </MemoryRouter>
      </QueryClientProvider>
    );
    const view = render(tree("alice"));
    fireEvent.change(screen.getByLabelText("问题"), {
      target: { value: "Which identity owns this request?" },
    });
    fireEvent.click(screen.getByRole("button", { name: "发送问题" }));
    await waitFor(() => expect(createChatSession).toHaveBeenCalledTimes(1));

    currentIdentity = BOB;
    view.rerender(tree("bob"));

    const finishCreate = resolveCreate;
    if (finishCreate === undefined) throw new Error("Session create mock did not start");
    act(() => {
      finishCreate({ session_id: "ses_created_as_alice" });
    });
    await waitFor(() =>
      expect(aliceAddLocalSession).toHaveBeenCalledTimes(1),
    );

    expect(aliceStartAsk).not.toHaveBeenCalled();
    expect(bobStartAsk).not.toHaveBeenCalled();
  });

  it("shows the current per-turn source instead of the Session creation mode", async () => {
    vi.mocked(listKnowledgeBases).mockResolvedValue({
      knowledge_bases: [knowledgeBase("kb_resume", "校招资料")],
    });
    const sessionRuntime = fakeRuntime(vi.fn(), vi.fn());
    vi.mocked(useChatRuntime).mockReturnValue({
      runtime: sessionRuntime,
      state: initialChatState([
        {
          sessionId: "ses_direct",
          title: "同一个会话",
          answerMode: "direct",
          knowledgeBaseId: null,
          createdAt: "2026-08-03T00:00:00Z",
          updatedAt: "2026-08-03T00:00:00Z",
        },
      ]),
    });
    const user = userEvent.setup();

    renderChatRoute("/chat/ses_direct");
    expect(
      await screen.findByText("当前：自由回答 · 可随时切换知识库"),
    ).toBeInTheDocument();

    await screen.findByRole("option", { name: "校招资料 · 2/2 可用" });
    await user.selectOptions(screen.getByLabelText("回答资料"), "kb_resume");
    expect(await screen.findByText("当前资料：校招资料")).toBeInTheDocument();
  });

  it.each([
    {
      answerMode: "direct" as const,
      expected: /未检索知识库/,
      forbidden: /已检索所选知识库/,
    },
    {
      answerMode: "rag" as const,
      expected: /已检索所选知识库，但没有找到足够相关的内容/,
      forbidden: /未检索知识库/,
    },
  ])(
    "says why an ungrounded $answerMode answer has no citations",
    async ({ answerMode, expected, forbidden }) => {
      vi.mocked(listKnowledgeBases).mockResolvedValue({
        knowledge_bases: [knowledgeBase("kb_resume", "校招资料")],
      });
      vi.mocked(useChatRuntime).mockReturnValue({
        runtime: fakeRuntime(vi.fn(), vi.fn()),
        state: stateWithUngroundedTurn(answerMode),
      });

      renderChatRoute("/chat/ses_answered");

      expect(await screen.findByText(expected)).toBeInTheDocument();
      expect(screen.queryByText(forbidden)).not.toBeInTheDocument();
    },
  );

  it("does not pass off a reloaded answer as one the server published no citations for", async () => {
    vi.mocked(listKnowledgeBases).mockResolvedValue({ knowledge_bases: [] });
    const state = stateWithUngroundedTurn("rag");
    // History carries no grounded flag at all, so the key is absent rather
    // than false -- that absence is exactly what this case is about.
    const replayed: ChatTurnState = { ...state.turns.turn_local, historical: true };
    delete replayed.grounded;
    vi.mocked(useChatRuntime).mockReturnValue({
      runtime: fakeRuntime(vi.fn(), vi.fn()),
      state: { ...state, turns: { turn_local: replayed } },
    });

    renderChatRoute("/chat/ses_answered");

    expect(
      await screen.findByText("历史记录只保存对话文本，不含引用与证据标记"),
    ).toBeInTheDocument();
    expect(screen.queryByText(/服务端没有为这段答案发布引用/)).not.toBeInTheDocument();
  });

  it("falls back to a direct Ask when a linked knowledge base no longer exists", async () => {
    vi.mocked(listKnowledgeBases).mockResolvedValue({ knowledge_bases: [] });
    vi.mocked(createChatSession).mockResolvedValue({ session_id: "ses_direct" });
    const user = userEvent.setup();

    renderChatRoute("/chat?kb=kb_deleted");
    await waitFor(() => expect(screen.getByLabelText("问题")).not.toBeDisabled());
    await user.type(screen.getByLabelText("问题"), "正常自由回答");
    await user.click(screen.getByRole("button", { name: "发送问题" }));

    await waitFor(() =>
      expect(aliceStartAsk).toHaveBeenCalledWith(
        expect.objectContaining({
          answerMode: "direct",
          knowledgeBaseId: null,
          question: "正常自由回答",
        }),
      ),
    );
  });
});

function fakeRuntime(addLocalSession: () => void, startAsk: () => void): ChatRuntime {
  return {
    addLocalSession,
    ensureHistory: vi.fn(),
    reconnectSessionStream: vi.fn(),
    retainSessionStream: vi.fn(() => () => undefined),
    startAsk,
  } as unknown as ChatRuntime;
}

function renderChatRoute(initialEntry: string) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[initialEntry]}>
        <Routes>
          <Route element={<ChatPage />} path="/chat" />
          <Route element={<ChatPage />} path="/chat/:sessionId" />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

// A committed turn the server answered without evidence. `rag` reaches this
// state through the routed shape: it retrieved, judged nothing relevant enough
// and fell back, which is a different fact from never having searched.
function stateWithUngroundedTurn(answerMode: "direct" | "rag") {
  const base = initialChatState([
    {
      sessionId: "ses_answered",
      title: "已回答的会话",
      answerMode,
      knowledgeBaseId: answerMode === "rag" ? "kb_resume" : null,
      createdAt: "2026-08-03T00:00:00Z",
      updatedAt: "2026-08-03T00:00:00Z",
    },
  ]);
  return {
    ...base,
    turns: {
      turn_local: {
        localId: "turn_local",
        sessionId: "ses_answered",
        question: "这些资料主要讲了什么？",
        answerMode,
        knowledgeBaseId: answerMode === "rag" ? "kb_resume" : null,
        topK: 8,
        idempotencyKey: "idem_local",
        submittedAt: "2026-08-03T00:00:00Z",
        phase: "committed" as const,
        activities: [],
        citations: [],
        historical: false,
        answer: "模型直接作答的内容。",
        grounded: false,
      },
    },
    turnOrderBySession: { ses_answered: ["turn_local"] },
  };
}

function knowledgeBase(knowledgeBaseId: string, name: string) {
  return {
    knowledge_base_id: knowledgeBaseId,
    name,
    description: null,
    document_count: 2,
    ready_document_count: 2,
    processing_document_count: 0,
    created_at: "2026-08-01T00:00:00Z",
    updated_at: "2026-08-02T00:00:00Z",
  };
}
