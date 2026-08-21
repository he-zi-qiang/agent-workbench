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
import {
  createChatSession,
  getCitedPassage,
  getChatSession,
  listChatSessions,
  listKnowledgeBases,
  listProjects,
  renameChatSession,
  setSessionProject,
} from "../../api/client";
import type { Citation, PrincipalIdentity, SourceLocator } from "../../api/types";
import { useIdentity } from "../../app/IdentityContext";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ChatPage } from "./ChatPage";
import { initialChatState, type ChatTurnState } from "./model";
import type { ChatRuntime } from "./runtime";
import { useChatRuntime } from "./useChatRuntime";

vi.mock("../../api/client", () => ({
  ApiError: class MockApiError extends Error {
    readonly status = 404;
  },
  createChatSession: vi.fn(),
  getCitedPassage: vi.fn(),
  getChatSession: vi.fn(() => Promise.reject(new Error("not found"))),
  listChatSessions: vi.fn(() => Promise.resolve({ sessions: [] })),
  listKnowledgeBases: vi.fn(() => Promise.resolve({ knowledge_bases: [] })),
  listProjects: vi.fn(() => Promise.resolve({ projects: [] })),
  renameChatSession: vi.fn(),
  setSessionProject: vi.fn(),
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
  vi.mocked(listChatSessions).mockResolvedValue({ sessions: [] });
  vi.mocked(getChatSession).mockRejectedValue(new Error("not found"));
  vi.mocked(renameChatSession).mockResolvedValue({
    session_id: "ses_direct",
    title: "新名字",
    last_activity_at: "2026-08-03T00:00:00Z",
    project_id: null,
  });
});

afterEach(() => cleanup());

describe("Chat identity boundary", () => {
  it("does not start an Ask in the old identity after Session creation resolves", async () => {
    let resolveCreate:
      | ((response: { session_id: string }) => void)
      | undefined;
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

  it("still asks the question when the reader opens another session mid-create", async () => {
    let settleCreate: ((response: { session_id: string }) => void) | undefined;
    vi.mocked(createChatSession).mockReturnValue(
      new Promise((resolve) => {
        settleCreate = resolve;
      }),
    );
    const startAsk = vi.fn();
    vi.mocked(useChatRuntime).mockReturnValue({
      runtime: fakeRuntime(vi.fn(), startAsk),
      state: initialChatState([localSession("ses_other", "另一个会话")]),
    });
    const user = userEvent.setup();

    renderChatRoute("/chat");
    await user.type(screen.getByLabelText("问题"), "这句话去哪了？");
    await user.click(screen.getByRole("button", { name: "发送问题" }));
    await waitFor(() => expect(createChatSession).toHaveBeenCalledTimes(1));

    // The sidebar is right there for the whole of the round trip.
    const other = await screen.findByRole("link", { name: /另一个会话/ });
    await user.click(other);
    expect(
      await screen.findByRole("heading", { name: "另一个会话" }),
    ).toBeInTheDocument();

    const finish = settleCreate;
    if (finish === undefined) throw new Error("Chat create mock did not start");
    await act(async () => {
      finish({ session_id: "ses_created" });
      await Promise.resolve();
    });

    // An Ask is addressed to the session the POST just created, not to the
    // address bar. Comparing against the submitted route dropped the question
    // outright: an empty orphan session in the list, the sentence still in the
    // composer, and nothing to say the send had done nothing.
    await waitFor(() => {
      expect(startAsk).toHaveBeenCalledTimes(1);
    });
    expect(startAsk.mock.calls[0]?.[0]).toMatchObject({
      sessionId: "ses_created",
      question: "这句话去哪了？",
    });
    // The navigation, unlike the Ask, does belong to the route: a reader who
    // has already moved is not moved again.
    expect(screen.getByRole("heading", { name: "另一个会话" })).toBeInTheDocument();
  });

  it("shows a source only when this turn actually uses one", async () => {
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
    await screen.findByRole("option", { name: "校招资料 · 2/2 可用" });
    expect(screen.queryByText("校招资料", { exact: true })).not.toBeInTheDocument();
    await user.selectOptions(screen.getByLabelText("回答资料"), "kb_resume");
    expect(
      await screen.findByText("校招资料", { exact: true }),
    ).toBeInTheDocument();
  });

  it("asks before deleting a session, and hands the id to the runtime", async () => {
    const removeSession = vi.fn(() => Promise.resolve());
    vi.mocked(useChatRuntime).mockReturnValue({
      runtime: fakeRuntime(vi.fn(), vi.fn(), removeSession),
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
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    const user = userEvent.setup();

    renderChatRoute("/chat/ses_direct");
    const remove = await screen.findByRole("button", { name: "删除对话 同一个会话" });

    // Declined first. Chat's list is the browser's, so a delete that ran
    // without asking would lose a transcript nothing else holds.
    await user.click(remove);
    expect(removeSession).not.toHaveBeenCalled();

    confirm.mockReturnValue(true);
    await user.click(remove);
    await waitFor(() => {
      expect(removeSession).toHaveBeenCalledWith("ses_direct");
    });
  });

  it("renames a Chat session inline and persists the title on the server", async () => {
    const localRename = vi.fn();
    vi.mocked(renameChatSession).mockResolvedValue({
      session_id: "ses_direct",
      // Deliberately differs from the request: the server owns normalization,
      // and this is the value the browser-local projection must accept.
      title: "服务端规范名",
      last_activity_at: "2026-08-03T00:00:00Z",
      project_id: null,
    });
    vi.mocked(useChatRuntime).mockReturnValue({
      runtime: fakeRuntime(vi.fn(), vi.fn(), undefined, localRename),
      state: initialChatState([
        {
          sessionId: "ses_direct",
          title: "旧名字",
          answerMode: "direct",
          knowledgeBaseId: null,
          createdAt: "2026-08-03T00:00:00Z",
          updatedAt: "2026-08-03T00:00:00Z",
        },
      ]),
    });
    const user = userEvent.setup();

    renderChatRoute("/chat/ses_direct");
    await user.click(
      await screen.findByRole("button", { name: "重命名对话 旧名字" }),
    );
    const field = screen.getByLabelText("对话名字");
    await user.clear(field);
    await user.type(field, "新名字{Enter}");

    await waitFor(() => {
      expect(vi.mocked(renameChatSession).mock.calls[0]?.slice(1)).toEqual([
        "ses_direct",
        "新名字",
      ]);
    });
    expect(localRename).toHaveBeenCalledWith("ses_direct", "服务端规范名");
    await waitFor(() => {
      expect(screen.getByRole("button", { name: "重命名对话 旧名字" })).toHaveFocus();
    });
  });

  it("returns focus to the Chat rename action when Escape cancels editing", async () => {
    vi.mocked(useChatRuntime).mockReturnValue({
      runtime: fakeRuntime(vi.fn(), vi.fn()),
      state: initialChatState([
        {
          sessionId: "ses_direct",
          title: "旧名字",
          answerMode: "direct",
          knowledgeBaseId: null,
          createdAt: "2026-08-03T00:00:00Z",
          updatedAt: "2026-08-03T00:00:00Z",
        },
      ]),
    });
    const user = userEvent.setup();

    renderChatRoute("/chat/ses_direct");
    const action = await screen.findByRole("button", { name: "重命名对话 旧名字" });
    await user.click(action);
    await user.type(screen.getByLabelText("对话名字"), "改了一半{Escape}");

    await waitFor(() => {
      expect(screen.getByRole("button", { name: "重命名对话 旧名字" })).toHaveFocus();
    });
    expect(screen.queryByLabelText("对话名字")).not.toBeInTheDocument();
    expect(renameChatSession).not.toHaveBeenCalled();
  });

  it("keeps a failed Chat rename inline, focused, and retryable", async () => {
    const localRename = vi.fn();
    vi.mocked(renameChatSession).mockRejectedValueOnce(new Error("名字没有保存"));
    vi.mocked(useChatRuntime).mockReturnValue({
      runtime: fakeRuntime(vi.fn(), vi.fn(), undefined, localRename),
      state: initialChatState([
        {
          sessionId: "ses_direct",
          title: "旧名字",
          answerMode: "direct",
          knowledgeBaseId: null,
          createdAt: "2026-08-03T00:00:00Z",
          updatedAt: "2026-08-03T00:00:00Z",
        },
      ]),
    });
    const user = userEvent.setup();

    renderChatRoute("/chat/ses_direct");
    await user.click(await screen.findByRole("button", { name: "重命名对话 旧名字" }));
    const field = screen.getByLabelText("对话名字");
    await user.clear(field);
    await user.type(field, "第一次{Enter}");

    expect(await screen.findByRole("alert")).toHaveTextContent("名字没有保存");
    expect(field).toHaveFocus();
    expect(field).not.toHaveAttribute("readonly");

    await user.clear(field);
    await user.type(field, "第二次{Enter}");
    await waitFor(() => {
      expect(renameChatSession).toHaveBeenCalledTimes(2);
      expect(localRename).toHaveBeenCalledWith("ses_direct", "新名字");
      expect(screen.getByRole("button", { name: "重命名对话 旧名字" })).toHaveFocus();
    });
  });

  it("finishes a Chat rename the reader walked away from, rather than locking the row", async () => {
    let settleRename:
      | ((accepted: {
          session_id: string;
          title: string;
          last_activity_at: string;
          project_id: string | null;
        }) => void)
      | undefined;
    vi.mocked(renameChatSession).mockReturnValue(
      new Promise((resolve) => {
        settleRename = resolve;
      }),
    );
    const localRename = vi.fn();
    vi.mocked(useChatRuntime).mockReturnValue({
      runtime: fakeRuntime(vi.fn(), vi.fn(), undefined, localRename),
      state: initialChatState([
        localSession("ses_direct", "旧名字"),
        localSession("ses_other", "另一个会话", "2026-08-04T00:00:00Z"),
      ]),
    });
    const user = userEvent.setup();

    renderChatRoute("/chat/ses_direct");
    await user.click(await screen.findByRole("button", { name: "重命名对话 旧名字" }));
    const field = screen.getByLabelText("对话名字");
    await user.clear(field);
    await user.type(field, "新名字{Enter}");
    await waitFor(() => expect(renameChatSession).toHaveBeenCalledTimes(1));

    // The PATCH is still open, and the reader opens a different session. The
    // row being renamed is in the sidebar, which /chat/ses_direct and
    // /chat/ses_other show alike -- so the route moving is not news to it.
    const other = screen.getByRole("link", { name: /另一个会话/ });
    await user.click(other);
    expect(
      await screen.findByRole("heading", { name: "另一个会话" }),
    ).toBeInTheDocument();

    const finish = settleRename;
    if (finish === undefined) throw new Error("Chat rename mock did not start");
    await act(async () => {
      finish({
        session_id: "ses_direct",
        title: "服务端规范名",
        last_activity_at: "2026-08-03T00:00:00Z",
        project_id: null,
      });
      await Promise.resolve();
    });

    expect(localRename).toHaveBeenCalledWith("ses_direct", "服务端规范名");
    // A row again. This is the regression: the continuation used to compare
    // against the detail route it was submitted under and return early, which
    // left `renamePending` set and the field `readOnly` until a reload.
    await waitFor(() => {
      expect(screen.queryByLabelText("对话名字")).not.toBeInTheDocument();
    });
    // And focus stayed where the reader put it, rather than being pulled back
    // to a row they had already left.
    await nextFrame();
    expect(other).toHaveFocus();
  });

  it("still reports a failed Chat rename after the reader opened another session", async () => {
    let failRename: ((cause: Error) => void) | undefined;
    vi.mocked(renameChatSession).mockReturnValue(
      new Promise((_resolve, reject) => {
        failRename = reject;
      }),
    );
    vi.mocked(useChatRuntime).mockReturnValue({
      runtime: fakeRuntime(vi.fn(), vi.fn()),
      state: initialChatState([
        localSession("ses_direct", "旧名字"),
        localSession("ses_other", "另一个会话", "2026-08-04T00:00:00Z"),
      ]),
    });
    const user = userEvent.setup();

    renderChatRoute("/chat/ses_direct");
    await user.click(await screen.findByRole("button", { name: "重命名对话 旧名字" }));
    const field = screen.getByLabelText("对话名字");
    await user.clear(field);
    await user.type(field, "新名字{Enter}");
    await waitFor(() => expect(renameChatSession).toHaveBeenCalledTimes(1));

    const other = screen.getByRole("link", { name: /另一个会话/ });
    await user.click(other);

    const fail = failRename;
    if (fail === undefined) throw new Error("Chat rename mock did not start");
    await act(async () => {
      fail(new Error("名字没有保存"));
      await Promise.resolve();
    });

    // The row is where the failure belongs, and the row is still on screen.
    expect(await screen.findByRole("alert")).toHaveTextContent("名字没有保存");
    expect(screen.getByLabelText("对话名字")).not.toHaveAttribute("readonly");
    // Retryable in place, without the caret being taken off what the reader
    // moved to.
    await nextFrame();
    expect(other).toHaveFocus();
  });

  it("offers a project only when there is one, and files the conversation into it", async () => {
    const user = userEvent.setup();
    vi.mocked(listProjects).mockResolvedValue({
      projects: [
        {
          project_id: "prj_1",
          name: "季度复盘",
          created_at: "2026-08-20T00:00:00Z",
          updated_at: "2026-08-20T00:00:00Z",
          archived_at: null,
        },
      ],
    });
    vi.mocked(setSessionProject).mockResolvedValue(undefined);
    vi.mocked(useChatRuntime).mockReturnValue({
      runtime: fakeRuntime(vi.fn(), vi.fn()),
      state: initialChatState([localSession("ses_direct", "当前会话")]),
    });

    renderChatRoute("/chat/ses_direct");
    const picker = await screen.findByLabelText("这段对话属于哪个项目");
    // 第一项是「不属于任何项目」，而且它是默认选中的那一项：归属可空，空是
    // 正常状态，不是一个需要被清除的异常。
    expect(picker).toHaveValue("");
    await user.selectOptions(picker, "prj_1");

    await waitFor(() => {
      expect(vi.mocked(setSessionProject).mock.calls[0]?.slice(1)).toEqual([
        "ses_direct",
        "prj_1",
      ]);
    });
  });

  it("does not show a picker that can only say no", async () => {
    vi.mocked(listProjects).mockResolvedValue({ projects: [] });
    vi.mocked(useChatRuntime).mockReturnValue({
      runtime: fakeRuntime(vi.fn(), vi.fn()),
      state: initialChatState([localSession("ses_direct", "当前会话")]),
    });

    renderChatRoute("/chat/ses_direct");
    await screen.findByRole("heading", { name: "当前会话" });

    // 一个只能选「无」的下拉框，是在提醒读者他缺了一个东西，而归属本来可选。
    expect(
      screen.queryByLabelText("这段对话属于哪个项目"),
    ).not.toBeInTheDocument();
  });

  it("uses double-click only to open another Chat session, not to rename it", async () => {
    vi.mocked(useChatRuntime).mockReturnValue({
      runtime: fakeRuntime(vi.fn(), vi.fn()),
      state: initialChatState([
        {
          sessionId: "ses_direct",
          title: "当前会话",
          answerMode: "direct",
          knowledgeBaseId: null,
          createdAt: "2026-08-03T00:00:00Z",
          updatedAt: "2026-08-03T00:00:00Z",
        },
        {
          sessionId: "ses_other",
          title: "另一个会话",
          answerMode: "direct",
          knowledgeBaseId: null,
          createdAt: "2026-08-04T00:00:00Z",
          updatedAt: "2026-08-04T00:00:00Z",
        },
      ]),
    });
    const user = userEvent.setup();

    renderChatRoute("/chat/ses_direct");
    await user.dblClick(await screen.findByRole("link", { name: /另一个会话/ }));

    expect(await screen.findByRole("heading", { name: "另一个会话" })).toBeInTheDocument();
    expect(screen.queryByLabelText("对话名字")).not.toBeInTheDocument();
    expect(renameChatSession).not.toHaveBeenCalled();
  });

  it("resolves a selected session outside the bounded recent list", async () => {
    const reconcileServerSessions = vi.fn();
    const runtime = fakeRuntime(
      vi.fn(),
      vi.fn(),
      undefined,
      undefined,
      reconcileServerSessions,
    );
    vi.mocked(useChatRuntime).mockReturnValue({
      runtime,
      state: initialChatState(),
    });
    vi.mocked(getChatSession).mockResolvedValue({
      session_id: "ses_older",
      title: "更早的会话",
      last_activity_at: "2026-07-01T00:00:00Z",
      project_id: null,
    });

    renderChatRoute("/chat/ses_older");

    await waitFor(() => {
      expect(getChatSession).toHaveBeenCalledWith(
        ALICE,
        "ses_older",
        expect.any(AbortSignal),
      );
      expect(reconcileServerSessions).toHaveBeenCalledWith([
        {
          session_id: "ses_older",
          title: "更早的会话",
          last_activity_at: "2026-07-01T00:00:00Z",
          project_id: null,
        },
      ]);
    });
  });

  it.each([
    {
      answerMode: "direct" as const,
      expected: /没有查资料/,
      forbidden: /已检索所选知识库/,
    },
    {
      answerMode: "rag" as const,
      expected: /已检索所选知识库，但没有找到足够相关的内容/,
      forbidden: /没有查资料/,
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

describe("What a citation tells the reader", () => {
  it("shows the page and the fragment a cited chunk sits at", async () => {
    vi.mocked(useChatRuntime).mockReturnValue({
      runtime: fakeRuntime(vi.fn(), vi.fn()),
      state: stateWithCitations([
        citation("chunk_paged", { page: 3, paragraph: 12 }),
        citation("chunk_markdown", { paragraph: 7 }),
        citation("chunk_bare", {}),
      ]),
    });

    const view = renderChatRoute("/chat/ses_answered");

    expect(await screen.findByText("第 3 页 · 片段 #12")).toBeInTheDocument();
    // No page in a Markdown source, and the chip says the fragment alone
    // rather than defaulting to 第 1 页.
    expect(screen.getByText("片段 #7")).toBeInTheDocument();
    // `paragraph` is the chunk's ordinal, not a paragraph number -- calling it
    // 第 12 段 would be the interface counting something the server never did.
    expect(screen.queryByText(/第 12 段/)).not.toBeInTheDocument();
    // A citation with no position renders the id and stops: an empty locator
    // is not a location, and an empty marker would read as one. The text query
    // alone cannot see that -- an empty <small> satisfies it too -- so the
    // element count is what holds the line. The rule carries a left margin, so
    // an empty one opens a gap in the chip that reads as a missing value.
    expect(screen.getAllByText(/片段 #/)).toHaveLength(2);
    expect(view.container.querySelectorAll(".aw-chat-citation-locator")).toHaveLength(2);
  });
});

describe("Opening the passage behind a citation", () => {
  it("fetches it through the turn, and shows the stored text", async () => {
    const user = userEvent.setup();
    vi.mocked(getCitedPassage).mockResolvedValue({
      chunk_id: "chunk_first",
      document_id: "doc_handbook",
      document_version: "rev_1",
      text: "手册第三页说的那句话。",
      ordinal: 7,
      page: 3,
    });
    vi.mocked(useChatRuntime).mockReturnValue({
      runtime: fakeRuntime(vi.fn(), vi.fn()),
      state: stateWithCitations([citation("chunk_first", { page: 3 })]),
    });

    renderChatRoute("/chat/ses_answered");
    // Nothing is fetched until asked. This is the property that decides the
    // row's shape: the text behind a citation is a fresh read that may refuse
    // (ADR-067), so a page of citations must not be a page of reads.
    expect(vi.mocked(getCitedPassage)).not.toHaveBeenCalled();

    // Found by the document, not the chunk: the row leads with what a reader
    // checking a claim actually needs to recognise. The chunk id is still on
    // the element's title, and still what goes to the clipboard.
    await user.click(await screen.findByRole("button", { name: /doc_handbook/ }));

    expect(await screen.findByText("手册第三页说的那句话。")).toBeInTheDocument();
    // Addressed through the turn, which is what supplies the document and
    // therefore the knowledge base the index read needs.
    expect(vi.mocked(getCitedPassage).mock.calls[0]?.slice(1)).toEqual([
      "ses_answered",
      "turn_answered",
      "chunk_first",
    ]);
  });

  it("says the passage cannot be read, not that the citation is broken", async () => {
    const user = userEvent.setup();
    // The commonest cause of a refusal here is a grant somebody revoked, and
    // reading a citation is a fresh authorization rather than a replay of the
    // one that published the answer. Reporting it as a fault would send the
    // reader looking for a bug in the transcript.
    vi.mocked(getCitedPassage).mockRejectedValue(new Error("404"));
    vi.mocked(useChatRuntime).mockReturnValue({
      runtime: fakeRuntime(vi.fn(), vi.fn()),
      state: stateWithCitations([citation("chunk_first", {})]),
    });

    renderChatRoute("/chat/ses_answered");
    await user.click(
      await screen.findByRole("button", { name: /doc_handbook/i }),
    );

    expect(await screen.findByText(/读不到这段原文了/)).toBeInTheDocument();
    expect(screen.queryByText(/引用坏了/)).not.toBeInTheDocument();
    // 不说是哪一种。三个原因都会落到这里，点名任何一个要么泄漏别人的授权状态，
    // 要么把读者支去自己的数据里找一个不存在的错。
    expect(screen.getByText(/这一次没有区分/)).toBeInTheDocument();
  });

  it("says a row can be opened before anybody opens it", async () => {
    const user = userEvent.setup();
    vi.mocked(getCitedPassage).mockResolvedValue({
      chunk_id: "chunk_first",
      document_id: "doc_handbook",
      document_version: "rev_1",
      text: "手册第三页说的那句话。",
      ordinal: 7,
      page: 3,
    });
    vi.mocked(useChatRuntime).mockReturnValue({
      runtime: fakeRuntime(vi.fn(), vi.fn()),
      state: stateWithCitations([citation("chunk_first", {})]),
    });

    renderChatRoute("/chat/ses_answered");

    // 未读的行和「原文恰好是空的」在此之前长得一模一样，于是 ADR-067 的那次
    // 点击无处可被发现。
    expect(await screen.findByText("点开取原文")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /doc_handbook/ }));
    await screen.findByText("手册第三页说的那句话。");

    // 取到之后换成另一句：同一个位置回答同一个问题——这一行现在是什么状态。
    expect(screen.queryByText("点开取原文")).not.toBeInTheDocument();
    expect(screen.getByText("刚刚重新读了一次")).toBeInTheDocument();
  });

  it("offers no such hint on a row that cannot address the route", async () => {
    vi.mocked(useChatRuntime).mockReturnValue({
      runtime: fakeRuntime(vi.fn(), vi.fn()),
      state: stateWithCitations([citation("chunk_first", {})], { bound: false }),
    });

    renderChatRoute("/chat/ses_answered");
    await screen.findByRole("button", { name: /doc_handbook/ });

    // 一个刷新之后的历史轮次没有 turn id，那次点击必然 404，而原因与读者的权限
    // 无关。对着一个不会打开的芯片写「点开取原文」，比原来的沉默更糟。
    expect(screen.queryByText("点开取原文")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /doc_handbook/ })).toBeDisabled();
  });
});

describe("Taking an answer somewhere else", () => {
  it("carries the full chunk id, which the page only ever shows shortened", async () => {
    const long = `chunk_${"a".repeat(40)}`;
    vi.mocked(useChatRuntime).mockReturnValue({
      runtime: fakeRuntime(vi.fn(), vi.fn()),
      state: stateWithCitations([citation(long, { paragraph: 2 })]),
    });

    const view = renderChatRoute("/chat/ses_answered");
    await screen.findByText(/片段 #2/);

    const chip = view.container.querySelector(".aw-chat-citation");
    // `shortId` cuts the middle out, so the identifier a reader would have to
    // quote to ask "where did this come from" was nowhere on the page.
    expect(chip?.textContent).not.toContain(long);
    expect(chip?.getAttribute("title")).toContain(long);
    // And the half that never existed is gone: `Citation.quote` is optional on
    // the wire and nothing in this repository ever sets it, so the conditional
    // that appended it rendered an empty string on every citation ever shown.
    // Its only effect was to make "读者能看到原文" look built.
    expect(chip?.getAttribute("title")).not.toContain("undefined");
  });

  it("puts the answer and every id in full onto the clipboard", async () => {
    const user = userEvent.setup();
    const writeText = vi.fn<(text: string) => Promise<void>>(() =>
      Promise.resolve(),
    );
    vi.stubGlobal("navigator", { ...navigator, clipboard: { writeText } });

    vi.mocked(useChatRuntime).mockReturnValue({
      runtime: fakeRuntime(vi.fn(), vi.fn()),
      state: stateWithCitations([citation("chunk_first", { page: 3 })]),
    });

    renderChatRoute("/chat/ses_answered");
    await user.click(
      await screen.findByRole("button", { name: /复制答案与引用/ }),
    );

    const written = writeText.mock.calls[0]?.[0] ?? "";
    expect(written).toContain("chunk_first");
    expect(written).toContain("doc_handbook");
    expect(await screen.findByText("已复制")).toBeInTheDocument();
    vi.unstubAllGlobals();
  });

  it("reports a clipboard the browser refused rather than looking like it worked", async () => {
    const user = userEvent.setup();
    // A copy button that does nothing and says nothing is worse than none: the
    // reader walks away believing they have the text.
    vi.stubGlobal("navigator", {
      ...navigator,
      clipboard: { writeText: vi.fn(() => Promise.reject(new Error("denied"))) },
    });

    vi.mocked(useChatRuntime).mockReturnValue({
      runtime: fakeRuntime(vi.fn(), vi.fn()),
      state: stateWithCitations([citation("chunk_first", {})]),
    });

    renderChatRoute("/chat/ses_answered");
    await user.click(
      await screen.findByRole("button", { name: /复制答案与引用/ }),
    );

    expect(await screen.findByText(/复制失败/)).toBeInTheDocument();
    vi.unstubAllGlobals();
  });
});

describe("What the transcript admits it is missing", () => {
  it("discloses the positions the stream could not decode", async () => {
    vi.mocked(useChatRuntime).mockReturnValue({
      runtime: fakeRuntime(vi.fn(), vi.fn()),
      state: { ...stateWithCitations([]), quarantinedSequences: { ses_answered: [2, 5] } },
    });

    renderChatRoute("/chat/ses_answered");

    expect(await screen.findByText("这次连接里有 2 个位置没能交给这个页面。")).toBeInTheDocument();
    expect(screen.getByText("#2")).toBeInTheDocument();
    expect(screen.getByText("#5")).toBeInTheDocument();
    // The rows are still in the log. Telling a reader their history was
    // destroyed would send them looking for the wrong thing.
    expect(screen.getByText(/没能解码/)).toBeInTheDocument();
    expect(screen.queryByText(/丢了|丢失/)).not.toBeInTheDocument();
  });

  it("control: a session with nothing quarantined says nothing", async () => {
    vi.mocked(useChatRuntime).mockReturnValue({
      runtime: fakeRuntime(vi.fn(), vi.fn()),
      state: stateWithCitations([]),
    });

    renderChatRoute("/chat/ses_answered");

    await screen.findByText("这些资料主要讲了什么？");
    expect(screen.queryByText(/个位置/)).not.toBeInTheDocument();
  });
});

function fakeRuntime(
  addLocalSession: () => void,
  startAsk: () => void,
  removeSession: () => Promise<void> = () => Promise.resolve(),
  renameSession: (sessionId: string, title: string) => void = vi.fn(),
  reconcileServerSessions: (sessions: unknown[]) => void = vi.fn(),
): ChatRuntime {
  return {
    addLocalSession,
    ensureHistory: vi.fn(),
    reconcileServerSessions,
    reconnectSessionStream: vi.fn(),
    retainSessionStream: vi.fn(() => () => undefined),
    removeSession,
    renameSession,
    startAsk,
  } as unknown as ChatRuntime;
}

/**
 * Let one animation frame pass.
 *
 * Focus is handed back from inside `requestAnimationFrame`, so an assertion
 * that focus was *not* taken proves nothing until a frame has run -- the
 * unguarded version of that code passes such a test simply by being late.
 */
async function nextFrame() {
  await act(async () => {
    await new Promise((resolve) => {
      window.requestAnimationFrame(() => {
        resolve(undefined);
      });
    });
  });
}

function localSession(
  sessionId: string,
  title: string,
  updatedAt = "2026-08-03T00:00:00Z",
) {
  return {
    sessionId,
    title,
    answerMode: "direct" as const,
    knowledgeBaseId: null,
    createdAt: "2026-08-03T00:00:00Z",
    updatedAt,
  };
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

function citation(chunkId: string, locator: SourceLocator): Citation {
  return {
    chunk_id: chunkId,
    document_id: "doc_handbook",
    document_version: "rev_1",
    locator,
  };
}

// A committed, grounded turn: the one state that renders the citation row at
// all, and so the only one these cases can be asked about.
function stateWithCitations(
  citations: Citation[],
  options: { bound?: boolean } = {},
) {
  const base = initialChatState([
    {
      sessionId: "ses_answered",
      title: "已回答的会话",
      answerMode: "rag",
      knowledgeBaseId: "kb_resume",
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
        answerMode: "rag" as const,
        knowledgeBaseId: "kb_resume",
        topK: 8,
        idempotencyKey: "idem_local",
        submittedAt: "2026-08-03T00:00:00Z",
        phase: "committed" as const,
        activities: [],
        citations,
        historical: false,
        // Bound, because opening a citation is addressed through the turn --
        // a state without one renders inert chips, which is its own case.
        ...(options.bound === false ? {} : { turnId: "turn_answered" }),
        answer: "根据资料的回答。",
        grounded: true,
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
    can_write: true,
    document_count: 2,
    ready_document_count: 2,
    processing_document_count: 0,
    failed_document_count: 0,
    created_at: "2026-08-01T00:00:00Z",
    updated_at: "2026-08-02T00:00:00Z",
  };
}
