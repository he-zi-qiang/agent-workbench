import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  askCode,
  decideCodeApproval,
  getCodeApprovals,
  getCodeHistory,
} from "../../api/client";
import type { PrincipalIdentity } from "../../api/types";
import { useIdentity } from "../../app/IdentityContext";
import { CodePage } from "./CodePage";

vi.mock("../../api/client", () => ({
  askCode: vi.fn(),
  createCodeSession: vi.fn(),
  decideCodeApproval: vi.fn(),
  getCodeApprovals: vi.fn(() => Promise.resolve({ approvals: [] })),
  getCodeHistory: vi.fn(() => Promise.resolve({ messages: [] })),
  newIdempotencyKey: vi.fn(() => "code-1"),
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
  vi.clearAllMocks();
  vi.mocked(useIdentity).mockReturnValue({
    identity: ALICE,
    setIdentity: vi.fn(),
    editorOpen: false,
    setEditorOpen: vi.fn(),
  } as unknown as ReturnType<typeof useIdentity>);
  vi.mocked(getCodeHistory).mockResolvedValue({ messages: [] });
  vi.mocked(getCodeApprovals).mockResolvedValue({ approvals: [] });
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
});
