import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  askCode,
  createCodeSession,
  decideCodeApproval,
  deleteCodeSession,
  downloadCodeWorkspaceFile,
  getCodeApprovals,
  getCodeHistory,
  getCodeWorkspace,
  getCodeWorkspaceFileText,
  listCodeSessions,
  putCodeWorkspaceFile,
  renameCodeSession,
} from "../../api/client";
import type { PrincipalIdentity } from "../../api/types";
import { useIdentity } from "../../app/IdentityContext";
import { CodePage } from "./CodePage";
import { DOCX_MEDIA_TYPE } from "../../components/media";
import { useCodeStream } from "./useCodeStream";

vi.mock("../../api/client", () => ({
  // A real constant, not a mock: HtmlPreview reads it to size-gate before
  // fetching, and a factory that omits it fails on first render.
  MAX_PREVIEW_BYTES: 512 * 1024,
  askCode: vi.fn(),
  createCodeSession: vi.fn(),
  decideCodeApproval: vi.fn(),
  deleteCodeSession: vi.fn(() => Promise.resolve({ session_id: "ses_code_1" })),
  getCodeApprovals: vi.fn(() => Promise.resolve({ approvals: [] })),
  getCodeHistory: vi.fn(() => Promise.resolve({ messages: [] })),
  getCodeWorkspace: vi.fn(() => Promise.resolve({ files: [] })),
  getCodeWorkspaceFileText: vi.fn(() =>
    Promise.resolve({ text: "", truncated: false }),
  ),
  downloadCodeWorkspaceFile: vi.fn(() => Promise.resolve()),
  listCodeSessions: vi.fn(() => Promise.resolve({ sessions: [] })),
  putCodeWorkspaceFile: vi.fn(() => Promise.resolve({ files: [] })),
  renameCodeSession: vi.fn(() =>
    Promise.resolve({ session_id: "ses_code_1", title: "x", last_activity_at: null }),
  ),
  newIdempotencyKey: vi.fn(() => "code-1"),
}));

// The stream opens a real `fetch` against an SSE endpoint. What it delivers is
// asserted through this seam instead, because a page test that waited on a
// network read would be testing the transport a second time.
vi.mock("./useCodeStream", () => ({
  useCodeStream: vi.fn(() => ({ steps: [], thinking: "" })),
}));

vi.mock("../../app/IdentityContext", () => ({
  useIdentity: vi.fn(),
}));

const ALICE: PrincipalIdentity = {
  tenantId: "tenant_a",
  principalId: "alice",
  scopes: ["workspace:write"],
};

const SESSION = "ses_code_1";

function mounted(entry: string = `/code/${SESSION}`) {
  // A client per render, with retries off: a shared one would carry one test's
  // session list into the next, and a retry would turn "the list failed" into
  // a test that hangs rather than one that fails.
  const queries = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queries}>
      <MemoryRouter initialEntries={[entry]}>
        <Routes>
          {/* The same optional-param route App.tsx mounts, so the first send's
              /code → /code/:id navigation stays inside one component instance
              here too. */}
          <Route element={<CodePage />} path="/code/:sessionId?" />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  // Call counts are what two of these tests assert on, and a mock is shared by
  // the whole file: without this, "was never called" means "was not called
  // since the last test that happened to reset it".
  vi.clearAllMocks();
  vi.mocked(useIdentity).mockReturnValue({
    identity: ALICE,
    setIdentity: vi.fn(),
    editorOpen: false,
    setEditorOpen: vi.fn(),
  } as unknown as ReturnType<typeof useIdentity>);
  vi.mocked(getCodeHistory).mockResolvedValue({ messages: [] });
  vi.mocked(getCodeApprovals).mockResolvedValue({ approvals: [] });
  vi.mocked(getCodeWorkspace).mockResolvedValue({ files: [] });
  vi.mocked(getCodeWorkspaceFileText).mockResolvedValue({
    text: "",
    truncated: false,
  });
  vi.mocked(downloadCodeWorkspaceFile).mockResolvedValue(undefined);
  vi.mocked(listCodeSessions).mockResolvedValue({ sessions: [] });
  vi.mocked(useCodeStream).mockReturnValue({ steps: [], thinking: "" });
});

describe("CodePage", () => {
  it("shows what the model is thinking while the turn is still running", async () => {
    const user = userEvent.setup();
    // Never resolves: the block is for a turn in flight, so the turn has to
    // still be in flight when the reasoning arrives.
    vi.mocked(askCode).mockImplementation(() => new Promise(() => undefined));
    vi.mocked(useCodeStream).mockReturnValue({
      steps: [],
      thinking: "先看 notes.md 里有什么。",
    });

    mounted();
    await user.type(screen.getByLabelText("要做的事"), "整理待办");
    await user.click(screen.getByRole("button", { name: "发送" }));

    await waitFor(() => {
      expect(screen.getByText("正在思考…")).toBeInTheDocument();
    });
    expect(screen.getByText("先看 notes.md 里有什么。")).toBeInTheDocument();
  });

  it("says nothing about thinking when no turn is running", () => {
    // The hook keeps its last value across a render; the block is gated on the
    // turn so a finished turn does not leave "正在思考…" over a written report.
    vi.mocked(useCodeStream).mockReturnValue({
      steps: [],
      thinking: "leftover reasoning",
    });

    mounted();

    expect(screen.queryByText("正在思考…")).not.toBeInTheDocument();
    expect(screen.queryByText("leftover reasoning")).not.toBeInTheDocument();
  });

  it("shows the report a turn came back with", async () => {
    const user = userEvent.setup();
    vi.mocked(askCode).mockResolvedValue({
      report: "Wrote notes.md.",
      workspace_version: "art_1",
      run_id: "run_1",
      status: "completed",
      stop_reason: "completed",
    });
    // The second read is what the page shows: the turn's own response is a
    // status, and the transcript comes from the server rather than from what
    // the client guessed it would say.
    vi.mocked(getCodeHistory)
      .mockResolvedValueOnce({ messages: [] })
      .mockResolvedValueOnce({
        messages: [
          { role: "user", text: "write notes.md" },
          { role: "assistant", text: "Wrote notes.md." },
        ],
      });

    mounted();
    await user.type(screen.getByLabelText("要做的事"), "write notes.md");
    await user.click(screen.getByRole("button", { name: "发送" }));

    await waitFor(() => {
      expect(screen.getByText("Wrote notes.md.")).toBeInTheDocument();
    });
    expect(vi.mocked(askCode).mock.calls[0]?.[2]).toBe("write notes.md");
  });

  it("offers three answers for a write, and two for an external tool", async () => {
    const user = userEvent.setup();
    // Never resolves: the approval poll only runs while a turn is in flight,
    // so the turn has to still be in flight when the question arrives.
    vi.mocked(askCode).mockImplementation(() => new Promise(() => undefined));
    vi.mocked(getCodeApprovals).mockResolvedValue({
      approvals: [
        {
          approval_id: "apr_write",
          tool_name: "workspace_write",
          argument_digest: "a".repeat(64),
          risk: "write",
        },
        {
          approval_id: "apr_shell",
          tool_name: "sandbox_run",
          argument_digest: "b".repeat(64),
          risk: "external",
        },
      ],
    });

    mounted();
    await user.type(screen.getByLabelText("要做的事"), "run the tests");
    await user.click(screen.getByRole("button", { name: "发送" }));

    const held = await screen.findByRole("region", { name: "待批准的调用" });
    const cards = within(held).getAllByRole("article");
    expect(within(cards[0] as HTMLElement).getAllByRole("button")).toHaveLength(3);
    // A standing yes to an irreversible effect is refused by the server, so it
    // is not offered here either -- a button whose only outcome is a 422 is a
    // button that teaches the reader the wrong rule.
    expect(within(cards[1] as HTMLElement).getAllByRole("button")).toHaveLength(2);
    expect(
      within(cards[1] as HTMLElement).queryByRole("button", {
        name: "本会话都允许",
      }),
    ).not.toBeInTheDocument();
  });

  it("sends the decision the button says, and stops showing the question", async () => {
    const user = userEvent.setup();
    vi.mocked(askCode).mockImplementation(() => new Promise(() => undefined));
    vi.mocked(decideCodeApproval).mockResolvedValue(undefined);
    vi.mocked(getCodeApprovals).mockResolvedValue({
      approvals: [
        {
          approval_id: "apr_write",
          tool_name: "workspace_write",
          argument_digest: "a".repeat(64),
          risk: "write",
        },
      ],
    });

    mounted();
    await user.type(screen.getByLabelText("要做的事"), "write it");
    await user.click(screen.getByRole("button", { name: "发送" }));

    const held = await screen.findByRole("region", { name: "待批准的调用" });
    await user.click(within(held).getByRole("button", { name: "拒绝" }));

    expect(vi.mocked(decideCodeApproval).mock.calls[0]?.slice(1)).toEqual([
      SESSION,
      "apr_write",
      "deny",
    ]);
  });

  it("says why a turn stopped when it produced no report", async () => {
    // A deadline-failed turn appends no assistant message at all -- the
    // server declines to invent one -- so without a notice the transcript
    // shows the instruction and then silence, which reads as a broken
    // session rather than a spent turn.
    const user = userEvent.setup();
    vi.mocked(askCode).mockResolvedValue({
      report: "",
      workspace_version: "art_1",
      run_id: "run_1",
      status: "failed",
      stop_reason: "deadline",
    });

    mounted();
    await user.type(screen.getByLabelText("要做的事"), "run the tests");
    await user.click(screen.getByRole("button", { name: "发送" }));

    await waitFor(() => {
      expect(screen.getByText(/这一轮到时间停下了/)).toBeInTheDocument();
    });
  });

  it("says what went wrong instead of losing the turn", async () => {
    const user = userEvent.setup();
    vi.mocked(askCode).mockRejectedValue(new Error("这个会话已经在跑一轮了"));

    mounted();
    await user.type(screen.getByLabelText("要做的事"), "write notes.md");
    await user.click(screen.getByRole("button", { name: "发送" }));

    await waitFor(() => {
      expect(screen.getByText("这个会话已经在跑一轮了")).toBeInTheDocument();
    });
  });

  it("asks nothing while no turn is running", async () => {
    mounted();
    await waitFor(() => {
      expect(vi.mocked(getCodeHistory)).toHaveBeenCalled();
    });

    // The control for the poll's condition. A poll that ran regardless would
    // ask this once a second for as long as the tab is open.
    expect(vi.mocked(getCodeApprovals)).not.toHaveBeenCalled();
  });

  it("shows nothing wrong when nothing is wrong", async () => {
    // The control that a mocked module makes necessary. Every other test here
    // asserts something present; a call this page makes on mount and the mock
    // does not define would throw into the same catch that renders errors, and
    // all of them would still pass.
    mounted();
    await waitFor(() => {
      expect(vi.mocked(getCodeWorkspace)).toHaveBeenCalled();
    });

    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("lists the files the session has produced", async () => {
    vi.mocked(getCodeWorkspace).mockResolvedValue({
      files: [
        { name: "notes.md", size_bytes: 2048, media_type: "text/markdown" },
      ],
    });

    mounted();

    const pane = await screen.findByRole("complementary", { name: "工作区文件" });
    expect(within(pane).getByText("notes.md")).toBeInTheDocument();
    expect(within(pane).getByText("2.0 KB")).toBeInTheDocument();
  });

  it("re-reads the workspace after a turn, including one that failed", async () => {
    const user = userEvent.setup();
    vi.mocked(askCode).mockRejectedValue(new Error("这一轮失败了"));

    mounted();
    await waitFor(() => {
      expect(vi.mocked(getCodeWorkspace)).toHaveBeenCalledTimes(1);
    });
    await user.type(screen.getByLabelText("要做的事"), "write notes.md");
    await user.click(screen.getByRole("button", { name: "发送" }));

    // The pointer moves per write, so a turn that failed may still have left
    // files behind. Not re-reading is how the pane starts lying.
    await waitFor(() => {
      expect(vi.mocked(getCodeWorkspace)).toHaveBeenCalledTimes(2);
    });
  });

  it("shows one line per action, not one per protocol event", async () => {
    const user = userEvent.setup();
    vi.mocked(askCode).mockImplementation(() => new Promise(() => undefined));
    // One tool call, as the server actually emits it: five events for the call
    // plus the model turn that proposed it.
    vi.mocked(useCodeStream).mockReturnValue({
      thinking: "",
      steps: [
        {
          event_id: "evt_m1",
          run_id: "run_1",
          timestamp: "2026-08-14T12:00:00Z",
          event_type: "ModelStarted",
          sequence: 1,
          payload: { kind: "ModelStarted", model_call_id: "mc_1" },
        },
        {
          event_id: "evt_m2",
          run_id: "run_1",
          timestamp: "2026-08-14T12:00:00Z",
          event_type: "ModelCompleted",
          sequence: 2,
          payload: {
            kind: "ModelCompleted",
            model_call_id: "mc_1",
            text: "",
            tool_call_ids: ["call_1"],
          },
        },
        {
          event_id: "evt_1",
          run_id: "run_1",
          timestamp: "2026-08-14T12:00:00Z",
          event_type: "ToolProposed",
          sequence: 3,
          payload: {
            kind: "ToolProposed",
            tool_call_id: "call_1",
            tool_name: "workspace_write",
            argument_preview: JSON.stringify({ name: "notes.md" }),
          },
        },
        {
          event_id: "evt_2",
          run_id: "run_1",
          timestamp: "2026-08-14T12:00:00Z",
          event_type: "PermissionResolved",
          sequence: 4,
          payload: {
            kind: "PermissionResolved",
            tool_call_id: "call_1",
            effect: "allow",
          },
        },
        {
          event_id: "evt_3",
          run_id: "run_1",
          timestamp: "2026-08-14T12:00:00Z",
          event_type: "ToolStarted",
          sequence: 5,
          payload: {
            kind: "ToolStarted",
            tool_call_id: "call_1",
            tool_name: "workspace_write",
          },
        },
        {
          event_id: "evt_4",
          run_id: "run_1",
          timestamp: "2026-08-14T12:00:00Z",
          event_type: "ToolCompleted",
          sequence: 6,
          payload: { kind: "ToolCompleted", tool_call_id: "call_1" },
        },
      ] as unknown as ReturnType<typeof useCodeStream>["steps"],
    });

    mounted();
    await user.type(screen.getByLabelText("要做的事"), "write notes.md");
    await user.click(screen.getByRole("button", { name: "发送" }));

    const steps = await screen.findByRole("region", { name: "执行过程" });
    // Six events, one action, inside one turn. Rendered one row per event this
    // read "模型调用已开始 / 模型调用已完成 / 工具调用已提出 / 权限检查已完成 /
    // 工具调用已开始 / 工具调用已完成" -- the log's vocabulary, leaving a reader
    // to work out that a file was written.
    //
    // Counted as the turn's own direct rows. `getAllByRole("listitem")` sees
    // every level at once -- the turn, the action, and the six folded events
    // that stay in the DOM inside a closed `details` -- so it answers "how deep
    // is the tree" rather than the question here, which is how many actions the
    // reader is offered.
    const actions = steps.querySelectorAll(
      ".aw-stream-step > .aw-stream-events > li",
    );
    expect(actions).toHaveLength(1);
    expect(within(steps).getByText("第 1 轮")).toBeInTheDocument();
    // Twice, and both are wanted: the collapsed turn carries a digest of what
    // it did, and the row inside it is the action itself. A closed turn that
    // said only "第 1 轮" would make the reader open every one to find the
    // interesting one.
    expect(within(steps).getAllByText("写入工作区")).toHaveLength(2);
    // And what it was aimed at, which is the half that says *which* file. Also
    // more than once: on the folded action row, and again inside it on the
    // proposal that names the argument.
    expect(within(steps).getAllByText("notes.md").length).toBeGreaterThan(0);
  });

  it("keeps a finished turn's steps on screen", async () => {
    const user = userEvent.setup();
    vi.mocked(askCode).mockResolvedValue({
      report: "Wrote notes.md.",
      workspace_version: "art_1",
      run_id: "run_1",
      status: "completed",
      stop_reason: "completed",
    });
    vi.mocked(useCodeStream).mockReturnValue({
      thinking: "",
      steps: [
        {
          event_id: "evt_1",
          run_id: "run_1",
          timestamp: "2026-08-14T12:00:00Z",
          event_type: "ToolStarted",
          sequence: 1,
          payload: {
            kind: "ToolStarted",
            tool_call_id: "call_1",
            tool_name: "workspace_write",
          },
        },
      ] as unknown as ReturnType<typeof useCodeStream>["steps"],
    });

    mounted();
    await user.type(screen.getByLabelText("要做的事"), "write notes.md");
    await user.click(screen.getByRole("button", { name: "发送" }));

    // The turn has settled -- the send button is back -- and the process it
    // went through is still readable. This pane used to be mounted only while
    // the request was in flight, over a list the hook emptied on the way out,
    // so the steps of the turn whose report is on screen were exactly the ones
    // that could never be looked at.
    // Waited on the transcript rather than on the send button: the composer
    // clears on submit, so the button stays disabled for an empty instruction
    // whether or not the turn is still running, and asserting on it would wait
    // for something that never becomes true.
    await waitFor(() => {
      expect(vi.mocked(getCodeHistory)).toHaveBeenCalledTimes(2);
    });
    const steps = screen.getByRole("region", { name: "执行过程" });
    expect(within(steps).getByText("第 1 轮")).toBeInTheDocument();
    expect(within(steps).getByText("写入工作区")).toBeInTheDocument();
  });

  it("lists sessions by the name their first instruction gave them", async () => {
    const user = userEvent.setup();
    vi.mocked(listCodeSessions).mockResolvedValue({
      sessions: [
        {
          session_id: "ses_code_older",
          title: "把 notes.md 整理成清单",
          last_activity_at: "2026-08-14T09:00:00Z",
        },
        { session_id: SESSION, title: null, last_activity_at: null },
      ],
    });

    mounted();

    const recent = await screen.findByRole("navigation", { name: "最近的编码会话" });
    // From the server, so it survives a cleared browser and a different
    // machine. The row with no title is one opened and never spoken in.
    await user.click(
      within(recent).getByRole("button", { name: "把 notes.md 整理成清单" }),
    );

    await waitFor(() => {
      expect(vi.mocked(getCodeHistory).mock.calls.at(-1)?.[1]).toBe("ses_code_older");
    });
  });

  it("uploads from beside the composer, and the workspace pane appears with it", async () => {
    const user = userEvent.setup();
    vi.mocked(putCodeWorkspaceFile).mockResolvedValue({
      files: [{ name: "notes.txt", size_bytes: 11, media_type: "text/plain" }],
    });

    mounted();
    // Before anything is uploaded this session has no files, so there is no
    // workspace pane at all -- the conversation keeps the whole width.
    await waitFor(() => {
      expect(vi.mocked(getCodeWorkspace)).toHaveBeenCalled();
    });
    expect(
      screen.queryByRole("complementary", { name: "工作区文件" }),
    ).not.toBeInTheDocument();

    // The control sits with the composer: attaching a file is part of asking,
    // not a property of the file pane it used to live in.
    const file = new File(["hello world"], "notes.txt", { type: "text/plain" });
    await user.upload(screen.getByLabelText(/上传/), file);

    await waitFor(() => {
      expect(vi.mocked(putCodeWorkspaceFile).mock.calls[0]?.[2]).toBe(file);
    });
    // The listing the write answered with, not a refetch: the endpoint returns
    // it precisely so the pane does not have to ask again for something it was
    // just told. And the pane exists now that there is a product to show.
    const pane = await screen.findByRole("complementary", { name: "工作区文件" });
    expect(await within(pane).findByText("notes.txt")).toBeInTheDocument();
  });

  it("opens on a centered start when there is no session", async () => {
    vi.mocked(listCodeSessions).mockResolvedValue({
      sessions: [
        {
          session_id: SESSION,
          title: "把 notes.md 整理成清单",
          last_activity_at: null,
        },
      ],
    });

    mounted("/code");

    expect(
      await screen.findByRole("heading", { name: "开始一段编码" }),
    ).toBeInTheDocument();
    // The recent list is unfolded here -- "where was I" is the likeliest
    // question a person with no session open is asking.
    const recent = await screen.findByRole("navigation", {
      name: "最近的编码会话",
    });
    expect(
      within(recent).getByRole("button", { name: "把 notes.md 整理成清单" }),
    ).toBeInTheDocument();
    // No panes about a session that does not exist.
    expect(
      screen.queryByRole("complementary", { name: "工作区文件" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("region", { name: "编码会话" }),
    ).not.toBeInTheDocument();
    // Upload waits for the first sentence; the session it would go into does
    // not exist yet.
    expect(screen.getByLabelText(/上传/)).toBeDisabled();
  });

  it("opens the session on the first sentence and runs the turn in it", async () => {
    const user = userEvent.setup();
    vi.mocked(createCodeSession).mockResolvedValue({
      session_id: SESSION,
      title: null,
    });
    vi.mocked(askCode).mockResolvedValue({
      report: "Done.",
      workspace_version: "art_1",
      run_id: "run_1",
      status: "completed",
      stop_reason: "completed",
    });

    mounted("/code");
    await user.type(screen.getByLabelText("要做的事"), "write notes.md");
    await user.click(screen.getByRole("button", { name: "发送" }));

    // The first sentence carries the POST the old splash screen demanded a
    // separate click for, and the turn runs against the session it opened.
    await waitFor(() => {
      expect(vi.mocked(askCode).mock.calls[0]?.[1]).toBe(SESSION);
    });
    expect(vi.mocked(createCodeSession)).toHaveBeenCalledTimes(1);
  });

  it("deletes a session only after the reader confirms", async () => {
    const user = userEvent.setup();
    vi.mocked(listCodeSessions).mockResolvedValue({
      sessions: [
        {
          session_id: "ses_code_older",
          title: "把 notes.md 整理成清单",
          last_activity_at: "2026-08-14T09:00:00Z",
        },
      ],
    });
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);

    mounted();
    const recent = await screen.findByRole("navigation", { name: "最近的编码会话" });
    const remove = within(recent).getByRole("button", {
      name: "删除会话 把 notes.md 整理成清单",
    });

    // Declined first. A delete that ran anyway would be irreversible, so the
    // refusal is the half worth asserting before the success.
    await user.click(remove);
    expect(vi.mocked(deleteCodeSession)).not.toHaveBeenCalled();

    confirm.mockReturnValue(true);
    await user.click(remove);
    await waitFor(() => {
      expect(vi.mocked(deleteCodeSession).mock.calls[0]?.[1]).toBe("ses_code_older");
    });
  });

  it("marks the session being viewed, even when it has no name yet", async () => {
    vi.mocked(listCodeSessions).mockResolvedValue({
      sessions: [{ session_id: SESSION, title: null, last_activity_at: null }],
    });

    mounted();

    const recent = await screen.findByRole("navigation", { name: "最近的编码会话" });
    expect(within(recent).getByRole("button", { current: "page" })).toBeInTheDocument();
  });

  it("renames a session to what a person called it", async () => {
    const user = userEvent.setup();
    // The server's answer changes after the rename, so the sidebar can only
    // show the new name by asking again.
    vi.mocked(listCodeSessions)
      .mockResolvedValueOnce({
        sessions: [
          { session_id: SESSION, title: "第一句指令", last_activity_at: null },
        ],
      })
      .mockResolvedValue({
        sessions: [
          { session_id: SESSION, title: "重构工作区", last_activity_at: null },
        ],
      });

    mounted();

    const recent = await screen.findByRole("navigation", { name: "最近的编码会话" });
    await user.dblClick(within(recent).getByRole("button", { name: "第一句指令" }));
    const field = within(recent).getByLabelText("会话名字");
    await user.clear(field);
    await user.type(field, "重构工作区{Enter}");

    // The first instruction is a decent name and a chosen one is better; this
    // is the only path that overwrites what the instruction set.
    await waitFor(() => {
      expect(vi.mocked(renameCodeSession).mock.calls[0]?.slice(1)).toEqual([
        SESSION,
        "重构工作区",
      ]);
    });
    // And the sidebar shows it. A rename that did not refetch would leave the
    // old name sitting there until something unrelated happened to refresh.
    expect(
      await within(recent).findByRole("button", { name: "重构工作区" }),
    ).toBeInTheDocument();
  });

  it("does not announce past sessions as files this turn produced", async () => {
    vi.mocked(getCodeWorkspace).mockResolvedValue({
      files: [{ name: "notes.md", size_bytes: 12, media_type: "text/markdown" }],
    });
    vi.mocked(listCodeSessions).mockResolvedValue({
      sessions: [
        {
          session_id: "ses_code_older",
          title: "上一个会话",
          last_activity_at: "2026-08-14T09:00:00Z",
        },
      ],
    });

    mounted();

    const pane = await screen.findByRole("complementary", { name: "工作区文件" });
    // The two lists sit in one column and looked fine either way. Nested, the
    // session rows were rows of the region labelled 工作区文件 -- so anything
    // reading that region by its label, a screen reader first among them, read
    // out session ids as files.
    expect(within(pane).getAllByRole("listitem")).toHaveLength(1);
    expect(within(pane).queryByText("上一个会话")).not.toBeInTheDocument();
  });

  it("shows what a text file holds, without spending a turn to ask", async () => {
    const user = userEvent.setup();
    vi.mocked(getCodeWorkspace).mockResolvedValue({
      files: [{ name: "notes.md", size_bytes: 12, media_type: "text/markdown" }],
    });
    vi.mocked(getCodeWorkspaceFileText).mockResolvedValue({
      text: "- ship it",
      truncated: false,
    });

    mounted();
    await user.click(await screen.findByRole("button", { name: /notes\.md/ }));

    // Before this the only way to see a file was to ask the agent to read it
    // back -- a model call to answer a question the store already knows.
    expect(await screen.findByText("- ship it")).toBeInTheDocument();
    expect(vi.mocked(getCodeWorkspaceFileText).mock.calls[0]?.slice(1)).toEqual([
      SESSION,
      "notes.md",
    ]);
  });

  it("offers a download for a type it cannot show, and does not fetch it", async () => {
    const user = userEvent.setup();
    vi.mocked(getCodeWorkspace).mockResolvedValue({
      files: [{ name: "report.docx", size_bytes: 4096, media_type: DOCX_MEDIA_TYPE }],
    });

    mounted();
    await user.click(await screen.findByRole("button", { name: /report\.docx/ }));

    expect(await screen.findByText("这个类型只能下载。")).toBeInTheDocument();
    // Not fetched at all: reading a zip as text would render mojibake, and
    // downloading the bytes only to decide not to show them wastes the transfer.
    expect(vi.mocked(getCodeWorkspaceFileText)).not.toHaveBeenCalled();

    await user.click(screen.getByRole("button", { name: "下载" }));
    expect(vi.mocked(downloadCodeWorkspaceFile).mock.calls[0]?.slice(1)).toEqual([
      SESSION,
      "report.docx",
    ]);
  });

  it("runs an html file in a sandboxed frame instead of showing its source", async () => {
    const user = userEvent.setup();
    vi.mocked(getCodeWorkspace).mockResolvedValue({
      files: [{ name: "demo.html", size_bytes: 64, media_type: "text/html" }],
    });
    vi.mocked(getCodeWorkspaceFileText).mockResolvedValue({
      text: "<html><body><h1>demo</h1></body></html>",
      truncated: false,
    });

    mounted();
    await user.click(await screen.findByRole("button", { name: /demo\.html/ }));

    // Rendered, not read: the page an agent builds only answers "did it
    // work?" by running. The sandbox value is pinned in HtmlPreview's own
    // tests; here the claim is that the workspace routes html into it.
    const frame = await screen.findByTitle("demo.html 预览");
    expect(frame.getAttribute("sandbox")).toBe("allow-scripts");
    // The source stays one toggle away rather than being the default view.
    expect(screen.getByRole("button", { name: "源码" })).toBeInTheDocument();
  });

  it("says a long file was cut rather than implying that is all of it", async () => {
    const user = userEvent.setup();
    vi.mocked(getCodeWorkspace).mockResolvedValue({
      files: [{ name: "big.txt", size_bytes: 999_999, media_type: "text/plain" }],
    });
    vi.mocked(getCodeWorkspaceFileText).mockResolvedValue({
      text: "head of it",
      truncated: true,
    });

    mounted();
    await user.click(await screen.findByRole("button", { name: /big\.txt/ }));

    // A truncated preview that said nothing would read as a complete file, and
    // the reader would go looking for content that is in the file but not here.
    expect(
      await screen.findByText("只显示了开头一部分，完整内容请下载。"),
    ).toBeInTheDocument();
  });
});
