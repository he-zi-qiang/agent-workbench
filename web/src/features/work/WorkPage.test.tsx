import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  ApiError,
  createTask,
  decideApproval,
  getApproval,
  getArtifactJson,
  getArtifactText,
  getTask,
  getTaskTimeline,
  listKnowledgeBases,
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
    getArtifactJson: vi.fn(),
    getArtifactText: vi.fn(),
    getTask: vi.fn(),
    getTaskTimeline: vi.fn(),
    listKnowledgeBases: vi.fn(),
    listTasks: vi.fn(),
    newIdempotencyKey: vi.fn(),
  };
});

describe("WorkPage task submission", () => {
  beforeEach(() => {
    localStorage.clear();
    vi.spyOn(window, "confirm").mockReturnValue(true);
    vi.mocked(createTask).mockReset();
    vi.mocked(decideApproval).mockReset();
    vi.mocked(getApproval).mockReset();
    vi.mocked(getArtifactJson).mockReset();
    vi.mocked(getArtifactText).mockReset();
    vi.mocked(getTask).mockReset();
    vi.mocked(getTaskTimeline).mockReset();
    vi.mocked(listKnowledgeBases).mockReset();
    vi.mocked(listTasks).mockReset();
    vi.mocked(newIdempotencyKey).mockReset();
    let keyNumber = 0;
    vi.mocked(newIdempotencyKey).mockImplementation(
      () => `task:intent_${String(++keyNumber)}`,
    );
    vi.mocked(listTasks).mockResolvedValue({ tasks: [], cursor: null });
    vi.mocked(listKnowledgeBases).mockResolvedValue({ knowledge_bases: [] });
    vi.mocked(getApproval).mockResolvedValue(approval("pending", 0));
    vi.mocked(getArtifactJson).mockResolvedValue(taskInput(false));
    vi.mocked(getArtifactText).mockResolvedValue({
      text: "# 建议\n三个方案的比较结论。",
      truncated: false,
    });
    vi.mocked(decideApproval).mockResolvedValue(approval("approved", 1));
    vi.mocked(getTask).mockResolvedValue({
      task_id: "task_created",
      status: "queued",
      status_detail: null,
      objective_preview: null,
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
        objective_preview: null,
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

  it("does not submit a stale knowledge-base link as a Task source", async () => {
    vi.mocked(createTask).mockReset();
    vi.mocked(createTask).mockResolvedValue({
      task_id: "task_created",
      status: "queued",
      status_detail: null,
      objective_preview: null,
      created_at: "2026-08-02T12:00:00Z",
      updated_at: "2026-08-02T12:00:00Z",
    });
    const user = userEvent.setup();
    renderWorkPage("/work?kb=kb_deleted");

    await user.type(screen.getByLabelText("目标"), "整理现有信息");
    await user.click(screen.getByRole("button", { name: "创建任务" }));

    await waitFor(() => expect(createTask).toHaveBeenCalledTimes(1));
    const input = vi.mocked(createTask).mock.calls[0]?.[1];
    // No report asked for, so none is promised: this objective mentions no file.
    // The pipeline is the form's visible default, sent explicitly.
    expect(input).toEqual({
      objective: "整理现有信息",
      maxRevisions: 2,
      wantsReport: false,
      graph: "research",
    });
  });

  it("submits the pipeline the reader picked, not a guess from the objective", async () => {
    vi.mocked(createTask).mockReset();
    vi.mocked(createTask).mockResolvedValue({
      task_id: "task_created",
      status: "queued",
      status_detail: null,
      objective_preview: null,
      created_at: "2026-08-02T12:00:00Z",
      updated_at: "2026-08-02T12:00:00Z",
    });
    const user = userEvent.setup();
    renderWorkPage();

    // An objective that *sounds* like research, submitted as 通用执行: the
    // wording must not decide the pipeline (ADR-031 -- a wrong guess runs the
    // entire wrong graph), only the visible control does.
    await user.type(screen.getByLabelText("目标"), "调研并把这批 CSV 清洗合并");
    await user.click(screen.getByRole("radio", { name: /通用执行/ }));
    await user.click(screen.getByRole("button", { name: "创建任务" }));

    await waitFor(() => expect(createTask).toHaveBeenCalledTimes(1));
    expect(vi.mocked(createTask).mock.calls[0]?.[1].graph).toBe("general");
  });

  it("asks for a report only when the objective does, and lets that be overridden", async () => {
    vi.mocked(createTask).mockReset();
    vi.mocked(createTask).mockResolvedValue({
      task_id: "task_created",
      status: "queued",
      status_detail: null,
      objective_preview: null,
      created_at: "2026-08-02T12:00:00Z",
      updated_at: "2026-08-02T12:00:00Z",
    });
    const user = userEvent.setup();
    renderWorkPage();

    const toggle = screen.getByRole("checkbox", { name: /生成报告文件/ });
    expect(toggle).not.toBeChecked();

    await user.type(screen.getByLabelText("目标"), "比较三个方案并输出一份建议报告");
    expect(toggle).toBeChecked();

    // The reader's own choice outranks the guess, and keeps outranking it.
    await user.click(toggle);
    expect(toggle).not.toBeChecked();

    await user.click(screen.getByRole("button", { name: "创建任务" }));
    await waitFor(() => expect(createTask).toHaveBeenCalledTimes(1));
    expect(vi.mocked(createTask).mock.calls[0]?.[1].wantsReport).toBe(false);
  });

  it("lists tasks by what they were asked to do, not by id", async () => {
    vi.mocked(listTasks).mockResolvedValue({
      tasks: [
        {
          task_id: "task_969398ecc7b14fbd9f24a50f53fbad7e",
          status: "queued",
          status_detail: null,
          objective_preview: "整理这批资料，比较三个方案并输出一份建议报告",
          created_at: "2026-08-02T12:00:00Z",
          updated_at: "2026-08-02T12:00:00Z",
        },
      ],
      cursor: null,
    });

    renderWorkPage();

    expect(
      await screen.findByText("整理这批资料，比较三个方案并输出一份建议报告"),
    ).toBeInTheDocument();
  });

  it("still opens a task the server recorded no objective for", async () => {
    vi.mocked(listTasks).mockResolvedValue({
      tasks: [
        {
          task_id: "task_969398ecc7b14fbd9f24a50f53fbad7e",
          status: "queued",
          status_detail: null,
          objective_preview: null,
          created_at: "2026-08-02T12:00:00Z",
          updated_at: "2026-08-02T12:00:00Z",
        },
      ],
      cursor: null,
    });

    renderWorkPage();

    // Falls back to the id rather than rendering an unclickable blank row.
    expect(await screen.findByText(/task_969398ec/)).toBeInTheDocument();
  });

  it("shows each stage's real steps under it, and its files in the rail", async () => {
    vi.mocked(getTask).mockResolvedValue({
      task_id: "task_run",
      status: "succeeded",
      status_detail: null,
      objective_preview: "今天丹东天气怎么样",
      created_at: "2026-08-02T12:00:00Z",
      updated_at: "2026-08-02T12:01:00Z",
    });
    vi.mocked(getTaskTimeline).mockResolvedValue(runTimeline());
    const user = userEvent.setup();
    renderWorkPage("/work/task_run");

    // The question, then the stages of work under it.
    expect(await screen.findByRole("heading", { name: "今天丹东天气怎么样" })).
      toBeInTheDocument();
    const stage = await screen.findByText("收集资料");

    // Collapsed on a finished Task, so six lines of history rather than a
    // wall of events -- and open on demand.
    const step = screen.getAllByText(/工具调用已开始：external_search/)[0];
    expect(step).not.toBeVisible();
    await user.click(stage);
    expect(step).toBeVisible();

    // The file is in the rail, named for what it is, without hunting for the
    // step that wrote it.
    const rail = screen.getByRole("complementary", { name: "附件" });
    expect(within(rail).getByText("检索到的证据")).toBeInTheDocument();
  });

  it("renders the produced report under the run, behind one download control", async () => {
    vi.mocked(getTask).mockResolvedValue({
      task_id: "task_run",
      status: "succeeded",
      status_detail: null,
      objective_preview: "比较三个方案并输出一份建议报告",
      created_at: "2026-08-02T12:00:00Z",
      updated_at: "2026-08-02T12:01:00Z",
    });
    vi.mocked(getTaskTimeline).mockResolvedValue(reportTimeline());
    renderWorkPage("/work/task_run");

    const output = await screen.findByRole("region", { name: "任务产出" });
    expect(within(output).getByText("report.md")).toBeInTheDocument();
    // Under the steps, in the reading column -- not in the file rail.
    expect(output.closest(".aw-work-run")).not.toBeNull();
    expect(
      within(screen.getByRole("complementary", { name: "附件" })).queryByText(
        "report.md",
      ),
    ).not.toBeInTheDocument();

    // One control, and it is an icon: the filename beside it already says what
    // the file is, so a "下载" label would only repeat it.
    const downloads = screen.getAllByRole("button", { name: /^下载/ });
    expect(downloads).toHaveLength(1);
    expect(downloads[0]).toHaveTextContent("");
  });

  it("presents an answer with no file as the result, not as a missing file", async () => {
    vi.mocked(getTask).mockResolvedValue({
      task_id: "task_run",
      status: "succeeded",
      status_detail: null,
      objective_preview: "今天丹东天气怎么样",
      created_at: "2026-08-02T12:00:00Z",
      updated_at: "2026-08-02T12:01:00Z",
    });
    vi.mocked(getTaskTimeline).mockResolvedValue(answerTimeline());
    vi.mocked(getArtifactJson).mockResolvedValue(taskInput(false));
    renderWorkPage("/work/task_run");

    const result = await screen.findByRole("region", { name: "任务结果" });
    expect(within(result).getByText(/晴，23°C 至 36°C/)).toBeInTheDocument();
    // Nothing was asked for a file, so nothing is missing.
    await waitFor(() =>
      expect(screen.queryByText(/没有生成文件/)).not.toBeInTheDocument(),
    );
  });

  it("says a file is missing only when the task asked for one", async () => {
    vi.mocked(getTask).mockResolvedValue({
      task_id: "task_run",
      status: "succeeded",
      status_detail: null,
      objective_preview: "输出一份建议报告",
      created_at: "2026-08-02T12:00:00Z",
      updated_at: "2026-08-02T12:01:00Z",
    });
    vi.mocked(getTaskTimeline).mockResolvedValue(answerTimeline());
    vi.mocked(getArtifactJson).mockResolvedValue(taskInput(true));
    renderWorkPage("/work/task_run");

    expect(await screen.findByText("没有生成文件")).toBeInTheDocument();
  });

  it("does not confirm an opposite decision returned for the same version", async () => {
    vi.mocked(getTask).mockResolvedValue(task("waiting_approval"));
    vi.mocked(getTaskTimeline).mockResolvedValue(approvalTimeline());
    vi.mocked(decideApproval).mockResolvedValue(approval("approved", 1));
    const user = userEvent.setup();
    renderWorkPage("/work/task_approval");

    await user.click(await screen.findByRole("button", { name: "不用了" }));

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

    await user.click(await screen.findByRole("button", { name: "生成报告" }));

    expect(
      await screen.findByText(
        "任务服务端状态已是“已取消”，审批不再可决定；已刷新权威记录。",
      ),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "生成报告" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "不用了" })).not.toBeInTheDocument();
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
    objective_preview: null,
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

function runTimeline() {
  const base = {
    schema_version: 1,
    stream_id: "stream_run",
    run_id: "run_run",
    durability: "durable" as const,
    task_id: "task_run",
    parent_event_id: null,
  };
  return {
    task_id: "task_run",
    cursor: "cursor_run",
    events: [
      {
        ...base,
        event_id: "event_started",
        event_type: "ToolStarted",
        timestamp: "2026-08-02T12:00:10Z",
        payload: {
          kind: "ToolStarted",
          tool_name: "external_search",
          tool_call_id: "tc_1",
        },
        sequence: 1,
        graph_node_id: "research_external",
      },
      {
        ...base,
        event_id: "event_evidence",
        event_type: "ToolCompleted",
        timestamp: "2026-08-02T12:00:20Z",
        payload: {
          kind: "ToolCompleted",
          tool_call_id: "tc_1",
          artifact: {
            schema_version: 1,
            artifact_id: "art_evidence",
            tenant_id: "tenant_local",
            kind: "evidence_bundle",
            media_type: "application/json",
            size_bytes: 4794,
            sha256: "a".repeat(64),
            filename: "evidence-bundle.json",
          },
        },
        sequence: 2,
        graph_node_id: "research_external",
      },
      {
        ...base,
        event_id: "event_done",
        event_type: "TaskSucceeded",
        timestamp: "2026-08-02T12:01:00Z",
        payload: { kind: "TaskSucceeded" },
        sequence: 3,
        graph_node_id: null,
      },
    ],
  };
}

function taskInput(wantsReport: boolean) {
  return {
    schema_version: 1,
    objective: "今天丹东天气怎么样",
    max_revisions: 2,
    knowledge_base_id: null,
    wants_report: wantsReport,
  };
}

/** A finished Task whose whole output is the answer the model wrote. */
function answerTimeline() {
  const base = {
    schema_version: 1,
    stream_id: "stream_run",
    run_id: "run_run",
    durability: "durable" as const,
    task_id: "task_run",
    parent_event_id: null,
  };
  return {
    task_id: "task_run",
    cursor: "cursor_answer",
    events: [
      {
        ...base,
        event_id: "event_submitted",
        event_type: "TaskSubmitted",
        timestamp: "2026-08-02T12:00:00Z",
        payload: { kind: "TaskSubmitted", input_ref: "art_input" },
        sequence: 1,
        graph_node_id: null,
      },
      {
        ...base,
        event_id: "event_answer",
        event_type: "ModelCompleted",
        timestamp: "2026-08-02T12:00:50Z",
        payload: {
          kind: "ModelCompleted",
          text: "今天丹东天气为晴，23°C 至 36°C。",
        },
        sequence: 2,
        graph_node_id: "synthesize",
      },
      {
        ...base,
        event_id: "event_succeeded",
        event_type: "TaskSucceeded",
        timestamp: "2026-08-02T12:01:00Z",
        payload: { kind: "TaskSucceeded" },
        sequence: 3,
        graph_node_id: null,
      },
    ],
  };
}

function reportTimeline() {
  const base = {
    schema_version: 1,
    stream_id: "stream_run",
    run_id: "run_run",
    durability: "durable" as const,
    task_id: "task_run",
    parent_event_id: null,
    graph_node_id: "export",
  };
  return {
    task_id: "task_run",
    cursor: "cursor_report",
    events: [
      {
        ...base,
        event_id: "event_export_started",
        event_type: "ToolStarted",
        timestamp: "2026-08-02T12:00:40Z",
        payload: {
          kind: "ToolStarted",
          tool_name: "export_artifact",
          tool_call_id: "tc_export",
        },
        sequence: 1,
      },
      {
        ...base,
        event_id: "event_export_done",
        event_type: "ToolCompleted",
        timestamp: "2026-08-02T12:00:50Z",
        payload: {
          kind: "ToolCompleted",
          tool_call_id: "tc_export",
          artifact: {
            schema_version: 1,
            artifact_id: "art_report",
            tenant_id: "tenant_local",
            kind: "report",
            media_type: "text/markdown",
            size_bytes: 2048,
            sha256: "b".repeat(64),
            filename: "report.md",
          },
        },
        sequence: 2,
      },
      {
        ...base,
        event_id: "event_succeeded",
        event_type: "TaskSucceeded",
        timestamp: "2026-08-02T12:01:00Z",
        payload: { kind: "TaskSucceeded" },
        sequence: 3,
        graph_node_id: null,
      },
    ],
  };
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
