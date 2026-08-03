import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  ApiError,
  createTask,
  decideApproval,
  getApproval,
  getTask,
  getTaskTimeline,
  listTasks,
  newIdempotencyKey,
} from "../../api/client";
import { IdentityProvider } from "../../app/IdentityContext";
import { WorkPage } from "./WorkPage";

vi.mock("../../api/client", async () => {
  const actual = await vi.importActual("../../api/client");
  return {
    ...actual,
    createTask: vi.fn(),
    decideApproval: vi.fn(),
    getApproval: vi.fn(),
    getTask: vi.fn(),
    getTaskTimeline: vi.fn(),
    listTasks: vi.fn(),
    newIdempotencyKey: vi.fn(),
  };
});

describe("WorkPage task submission", () => {
  beforeEach(() => {
    localStorage.clear();
    vi.mocked(createTask).mockReset();
    vi.mocked(decideApproval).mockReset();
    vi.mocked(getApproval).mockReset();
    vi.mocked(getTask).mockReset();
    vi.mocked(getTaskTimeline).mockReset();
    vi.mocked(listTasks).mockReset();
    vi.mocked(newIdempotencyKey).mockReset();
    let keyNumber = 0;
    vi.mocked(newIdempotencyKey).mockImplementation(
      () => `task:intent_${String(++keyNumber)}`,
    );
    vi.mocked(listTasks).mockResolvedValue({ tasks: [], cursor: null });
    vi.mocked(getApproval).mockResolvedValue(approval("pending", 0));
    vi.mocked(decideApproval).mockResolvedValue(approval("approved", 1));
    vi.mocked(getTask).mockResolvedValue({
      task_id: "task_created",
      status: "queued",
      status_detail: null,
      created_at: "2026-08-02T12:00:00Z",
      updated_at: "2026-08-02T12:00:00Z",
    });
    vi.mocked(getTaskTimeline).mockResolvedValue({
      task_id: "task_created",
      events: [],
      cursor: null,
    });
    vi.mocked(createTask)
      .mockRejectedValueOnce(new Error("network unavailable"))
      .mockRejectedValueOnce(new Error("network unavailable"))
      .mockResolvedValueOnce({
        task_id: "task_created",
        status: "queued",
        status_detail: null,
        created_at: "2026-08-02T12:00:00Z",
        updated_at: "2026-08-02T12:00:00Z",
      });
  });

  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  it("reuses the intent key on explicit retry and rotates it after editing and success", async () => {
    const user = userEvent.setup();
    renderWorkPage();

    await user.type(screen.getByLabelText("目标"), "same intent");
    await user.click(screen.getByRole("button", { name: "创建任务" }));
    await waitFor(() => expect(createTask).toHaveBeenCalledTimes(1));
    const firstKey = vi.mocked(createTask).mock.calls[0]?.[2];

    await user.click(screen.getByRole("button", { name: "创建任务" }));
    await waitFor(() => expect(createTask).toHaveBeenCalledTimes(2));
    const retryKey = vi.mocked(createTask).mock.calls[1]?.[2];
    expect(retryKey).toBe(firstKey);

    await user.type(screen.getByLabelText("目标"), " updated");
    const callsBeforeSuccess = vi.mocked(newIdempotencyKey).mock.calls.length;
    await user.click(screen.getByRole("button", { name: "创建任务" }));
    await waitFor(() => expect(createTask).toHaveBeenCalledTimes(3));
    const editedKey = vi.mocked(createTask).mock.calls[2]?.[2];
    expect(editedKey).not.toBe(firstKey);

    await waitFor(() =>
      expect(newIdempotencyKey).toHaveBeenCalledTimes(callsBeforeSuccess + 1),
    );
  });

  it("does not confirm an opposite decision returned for the same version", async () => {
    vi.mocked(getTask).mockResolvedValue(task("waiting_approval"));
    vi.mocked(getTaskTimeline).mockResolvedValue(approvalTimeline());
    vi.mocked(decideApproval).mockResolvedValue(approval("approved", 1));
    const user = userEvent.setup();
    renderWorkPage("/work/task_approval");

    await user.click(await screen.findByRole("button", { name: "拒绝" }));

    expect(
      await screen.findByText(
        "本次“已拒绝”未被应用；同一决定版本的服务端权威状态为“已批准”。",
      ),
    ).toBeInTheDocument();
    expect(screen.getByText("已批准")).toBeInTheDocument();
  });

  it("explains a cancelled-task 409 and removes decision controls", async () => {
    vi.mocked(getTask)
      .mockResolvedValueOnce(task("waiting_approval"))
      .mockResolvedValue(task("cancelled"));
    vi.mocked(getTaskTimeline).mockResolvedValue(approvalTimeline());
    vi.mocked(decideApproval).mockRejectedValue(
      new ApiError(409, "approval is not decidable"),
    );
    const user = userEvent.setup();
    renderWorkPage("/work/task_approval");

    await user.click(await screen.findByRole("button", { name: "批准" }));

    expect(
      await screen.findByText(
        "任务服务端状态已是“已取消”，审批不再可决定；已刷新权威记录。",
      ),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "批准" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "拒绝" })).not.toBeInTheDocument();
  });
});

function renderWorkPage(initialEntry = "/work") {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <IdentityProvider>
        <MemoryRouter initialEntries={[initialEntry]}>
          <Routes>
            <Route element={<WorkPage />} path="/work" />
            <Route element={<WorkPage />} path="/work/:taskId" />
          </Routes>
        </MemoryRouter>
      </IdentityProvider>
    </QueryClientProvider>,
  );
}

function task(status: "waiting_approval" | "cancelled") {
  return {
    task_id: "task_approval",
    status,
    status_detail: status === "cancelled" ? "cancelled elsewhere" : null,
    created_at: "2026-08-02T12:00:00Z",
    updated_at: "2026-08-02T12:01:00Z",
  } as const;
}

function approval(status: "pending" | "approved", decisionVersion: number) {
  return {
    approval_id: "approval_1",
    task_id: "task_approval",
    status,
    decision_version: decisionVersion,
    decided_at: status === "pending" ? null : "2026-08-02T12:01:00Z",
    created_at: "2026-08-02T12:00:30Z",
  } as const;
}

function approvalTimeline() {
  return {
    task_id: "task_approval",
    cursor: "cursor_approval",
    events: [
      {
        schema_version: 1,
        event_id: "event_approval",
        stream_id: "stream_approval",
        run_id: "run_approval",
        event_type: "TaskApprovalRequested",
        durability: "durable" as const,
        timestamp: "2026-08-02T12:00:30Z",
        payload: {
          kind: "TaskApprovalRequested",
          approval_id: "approval_1",
        },
        sequence: 2,
        task_id: "task_approval",
        graph_node_id: "node_review",
        parent_event_id: null,
      },
    ],
  };
}
