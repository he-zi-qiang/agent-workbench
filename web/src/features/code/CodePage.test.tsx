import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  askCode,
  decideCodeApproval,
  getCodeApprovals,
  getCodeHistory,
  getCodeWorkspace,
} from "../../api/client";
import type { PrincipalIdentity } from "../../api/types";
import { useIdentity } from "../../app/IdentityContext";
import { CodePage } from "./CodePage";
import { rememberCodeSession } from "./storage";
import { useCodeStream } from "./useCodeStream";

vi.mock("../../api/client", () => ({
  askCode: vi.fn(),
  createCodeSession: vi.fn(),
  decideCodeApproval: vi.fn(),
  getCodeApprovals: vi.fn(() => Promise.resolve({ approvals: [] })),
  getCodeHistory: vi.fn(() => Promise.resolve({ messages: [] })),
  getCodeWorkspace: vi.fn(() => Promise.resolve({ files: [] })),
  newIdempotencyKey: vi.fn(() => "code-1"),
}));

// The stream opens a real `fetch` against an SSE endpoint. What it delivers is
// asserted through this seam instead, because a page test that waited on a
// network read would be testing the transport a second time.
vi.mock("./useCodeStream", () => ({
  useCodeStream: vi.fn(() => []),
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

function mounted() {
  return render(
    <MemoryRouter initialEntries={[`/code/${SESSION}`]}>
      <Routes>
        <Route element={<CodePage />} path="/code/:sessionId" />
      </Routes>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  // Call counts are what two of these tests assert on, and a mock is shared by
  // the whole file: without this, "was never called" means "was not called
  // since the last test that happened to reset it".
  // The session list lives in this browser, so it survives a mock reset
  // and would otherwise carry one test's sessions into the next.
  window.localStorage.clear();
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
  vi.mocked(useCodeStream).mockReturnValue([]);
});

describe("CodePage", () => {
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

  it("shows what the agent is doing while a turn runs", async () => {
    const user = userEvent.setup();
    vi.mocked(askCode).mockImplementation(() => new Promise(() => undefined));
    vi.mocked(useCodeStream).mockReturnValue([
      {
        event_id: "evt_1",
        event_type: "ToolStarted",
        sequence: 1,
        payload: { kind: "ToolStarted", tool_name: "workspace_write" },
      } as unknown as ReturnType<typeof useCodeStream>[number],
    ]);

    mounted();
    await user.type(screen.getByLabelText("要做的事"), "write notes.md");
    await user.click(screen.getByRole("button", { name: "发送" }));

    const steps = await screen.findByRole("region", { name: "正在进行的步骤" });
    // Titled from the shared event vocabulary, so an event type this console
    // does not know is visible as unknown rather than dropped.
    expect(
      within(steps).getByText("工具调用已开始：workspace_write"),
    ).toBeInTheDocument();
  });

  it("offers a way back to a session this browser has already opened", async () => {
    const user = userEvent.setup();
    rememberCodeSession(ALICE, {
      sessionId: "ses_code_older",
      seenAt: "2026-08-14T09:00:00Z",
    });

    mounted();

    const recent = await screen.findByRole("navigation", { name: "最近的编码会话" });
    // Nothing on the server lists a principal's sessions, so without this the
    // only way back to one is a link somebody kept outside the app.
    await user.click(within(recent).getByRole("button", { name: "code_old" }));

    await waitFor(() => {
      expect(vi.mocked(getCodeHistory).mock.calls.at(-1)?.[1]).toBe("ses_code_older");
    });
  });

  it("remembers the session it arrived at, and marks it as the open one", async () => {
    mounted();

    const recent = await screen.findByRole("navigation", { name: "最近的编码会话" });
    // Arrived at by URL, never created here. A list that only knew what this
    // tab made would lose every session reached by a pasted link.
    expect(within(recent).getByRole("button", { current: "page" })).toHaveTextContent(
      "code_1",
    );
  });

  it("does not announce past sessions as files this turn produced", async () => {
    vi.mocked(getCodeWorkspace).mockResolvedValue({
      files: [{ name: "notes.md", size_bytes: 12, media_type: "text/markdown" }],
    });
    rememberCodeSession(ALICE, {
      sessionId: "ses_code_older",
      seenAt: "2026-08-14T09:00:00Z",
    });

    mounted();

    const pane = await screen.findByRole("complementary", { name: "工作区文件" });
    // The two lists sit in one column and looked fine either way. Nested, the
    // session rows were rows of the region labelled 工作区文件 -- so anything
    // reading that region by its label, a screen reader first among them, read
    // out session ids as files.
    expect(within(pane).getAllByRole("listitem")).toHaveLength(1);
    expect(within(pane).queryByText("code_old")).not.toBeInTheDocument();
  });
});
