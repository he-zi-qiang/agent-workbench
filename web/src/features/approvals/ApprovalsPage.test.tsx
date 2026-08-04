import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  decideApproval,
  getArtifactJson,
  getTaskTimeline,
  listApprovals,
} from "../../api/client";
import { IdentityProvider } from "../../app/IdentityContext";
import { ApprovalsPage } from "./ApprovalsPage";

vi.mock("../../api/client", async () => {
  const actual = await vi.importActual("../../api/client");
  return {
    ...actual,
    decideApproval: vi.fn(),
    getArtifactJson: vi.fn(),
    getTaskTimeline: vi.fn(),
    listApprovals: vi.fn(),
  };
});

describe("ApprovalsPage", () => {
  beforeEach(() => {
    localStorage.clear();
    vi.mocked(listApprovals).mockReset();
    vi.mocked(getTaskTimeline).mockReset();
    vi.mocked(getArtifactJson).mockReset();
    vi.mocked(decideApproval).mockReset();
    vi.mocked(listApprovals).mockResolvedValue({
      approvals: [pendingApproval],
      cursor: null,
    });
    vi.mocked(getTaskTimeline).mockResolvedValue(taskTimeline);
    vi.mocked(getArtifactJson).mockResolvedValue({
      schema_version: 1,
      objective: "整理校招 Agent 项目的架构报告",
      max_revisions: 2,
      knowledge_base_id: null,
    });
    vi.mocked(decideApproval).mockResolvedValue({
      ...pendingApproval,
      status: "approved",
      decision_version: 1,
      decided_at: "2026-08-03T12:05:00Z",
    });
  });

  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  it("shows the real task objective and requires confirmation before approving", async () => {
    const user = userEvent.setup();
    const { container } = renderPage();

    expect(
      await screen.findByText("整理校招 Agent 项目的架构报告"),
    ).toBeInTheDocument();
    expect(screen.getAllByText("允许生成并导出任务报告")).toHaveLength(2);
    expect(container.querySelector("details")).not.toHaveAttribute("open");

    await user.click(screen.getByRole("button", { name: "允许导出" }));

    expect(screen.getByRole("dialog", { name: "确认允许导出？" })).toBeInTheDocument();
    expect(decideApproval).not.toHaveBeenCalled();

    await user.click(screen.getByRole("button", { name: "确认允许" }));
    await waitFor(() =>
      expect(decideApproval).toHaveBeenCalledWith(
        expect.objectContaining({ tenantId: "tenant_local" }),
        pendingApproval,
        "approved",
      ),
    );
  });

  it("degrades honestly when the task objective cannot be read", async () => {
    vi.mocked(getTaskTimeline).mockRejectedValue(new Error("timeline unavailable"));
    const user = userEvent.setup();
    renderPage();

    expect(
      await screen.findByText(
        "暂时无法读取任务目标。请先打开关联任务核对内容，再决定是否导出。",
      ),
    ).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "拒绝导出" }));
    expect(
      screen.getByText("任务目标当前无法读取，请确认你已经在关联任务中核对过内容。"),
    ).toBeInTheDocument();
    expect(
      screen.getByText("确认后，任务会结束，并且不会导出报告。"),
    ).toBeInTheDocument();
    expect(decideApproval).not.toHaveBeenCalled();
  });
});

function renderPage() {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <IdentityProvider>
        <MemoryRouter>
          <ApprovalsPage />
        </MemoryRouter>
      </IdentityProvider>
    </QueryClientProvider>,
  );
}

const pendingApproval = {
  approval_id: "approval_1",
  task_id: "task_1",
  status: "pending" as const,
  decision_version: 0,
  decided_at: null,
  created_at: "2026-08-03T12:00:00Z",
};

const taskTimeline = {
  task_id: "task_1",
  cursor: null,
  events: [
    {
      schema_version: 1,
      event_id: "event_submitted",
      stream_id: "task_1",
      run_id: "run_1",
      event_type: "TaskSubmitted",
      durability: "durable" as const,
      timestamp: "2026-08-03T12:00:00Z",
      payload: {
        kind: "TaskSubmitted",
        input_ref: "artifact_task_input",
      },
      sequence: 1,
      task_id: "task_1",
      graph_node_id: null,
      parent_event_id: null,
    },
  ],
};
