import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  ApiError,
  createTask,
  decideApproval,
  deleteTask,
  downloadArtifact,
  getApproval,
  getArtifactBlob,
  getArtifactJson,
  getArtifactText,
  getDocumentPdf,
  getDocumentPreview,
  getTask,
  getTaskTimeline,
  listKnowledgeBases,
  listTasks,
  newIdempotencyKey,
  triageTask,
} from "../../api/client";
import type { DocumentPreview } from "../../api/types";
import { IdentityProvider } from "../../app/IdentityContext";
import { WorkPage } from "./WorkPage";

vi.mock("../../api/client", async () => {
  const actual = await vi.importActual("../../api/client");
  return {
    ...actual,
    createTask: vi.fn(),
    decideApproval: vi.fn(),
    deleteTask: vi.fn(),
    downloadArtifact: vi.fn(),
    getApproval: vi.fn(),
    getArtifactBlob: vi.fn(),
    getArtifactJson: vi.fn(),
    getArtifactText: vi.fn(),
    getDocumentPdf: vi.fn(),
    getDocumentPreview: vi.fn(),
    getTask: vi.fn(),
    getTaskTimeline: vi.fn(),
    listKnowledgeBases: vi.fn(),
    listTasks: vi.fn(),
    newIdempotencyKey: vi.fn(),
    triageTask: vi.fn(),
  };
});

/** What the endpoint answers when triage is disabled or failing: submit what
 * you always submitted. The deployment-default shape for most tests. */
const TRIAGE_DEFAULT = {
  status: "default" as const,
  graph: null,
  wants_report: null,
  reason: null,
  question: null,
  options: [],
};

describe("WorkPage task submission", () => {
  beforeEach(() => {
    localStorage.clear();
    vi.spyOn(window, "confirm").mockReturnValue(true);
    vi.mocked(createTask).mockReset();
    vi.mocked(decideApproval).mockReset();
    vi.mocked(downloadArtifact).mockReset();
    vi.mocked(getApproval).mockReset();
    vi.mocked(getArtifactBlob).mockReset();
    vi.mocked(getArtifactJson).mockReset();
    vi.mocked(getArtifactText).mockReset();
    // Left without a default, like `getDocumentPreview` beside it -- but the
    // reason moved: a .docx now converts the moment it opens, so every docx
    // fixture mocks this itself, and an unmocked call is a test discovering
    // the panel converting an artifact that is not a Word document.
    vi.mocked(getDocumentPdf).mockReset();
    vi.mocked(getDocumentPreview).mockReset();
    vi.mocked(getTask).mockReset();
    vi.mocked(getTaskTimeline).mockReset();
    vi.mocked(listKnowledgeBases).mockReset();
    vi.mocked(listTasks).mockReset();
    vi.mocked(newIdempotencyKey).mockReset();
    vi.mocked(triageTask).mockReset();
    vi.mocked(triageTask).mockResolvedValue(TRIAGE_DEFAULT);
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
      agent_invocation_count: 0,
      objective_preview: null,
      created_at: "2026-08-02T12:00:00Z",
      updated_at: "2026-08-02T12:00:00Z",
    });
    vi.mocked(getTaskTimeline).mockResolvedValue({
      task_id: "task_created",
      events: [],
      cursor: null,
      // The server's claim that this page is whole. Every fixture here carries
      // it, so a test that says nothing about damage is a test asserting the
      // page stays quiet about it.
      skipped_sequences: [],
    });
    vi.mocked(createTask)
      .mockRejectedValueOnce(new Error("network unavailable"))
      .mockRejectedValueOnce(new Error("network unavailable"))
      .mockResolvedValueOnce({
        task_id: "task_created",
        status: "queued",
        status_detail: null,
        agent_invocation_count: 0,
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
      agent_invocation_count: 0,
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
    // Triage answered "default", so the form submits exactly what it always
    // submitted: no graph field (the deployment decides), no report, and a
    // provenance block saying nobody decided (ADR-036).
    expect(input).toEqual({
      objective: "整理现有信息",
      maxRevisions: 2,
      wantsReport: false,
      intent: {
        graph_decided_by: "default",
        wants_report_decided_by: "default",
      },
    });
  });

  it("submits a decided triage verdict with its provenance", async () => {
    vi.mocked(createTask).mockReset();
    vi.mocked(createTask).mockResolvedValue({
      task_id: "task_created",
      status: "queued",
      status_detail: null,
      agent_invocation_count: 0,
      objective_preview: null,
      created_at: "2026-08-02T12:00:00Z",
      updated_at: "2026-08-02T12:00:00Z",
    });
    vi.mocked(triageTask).mockResolvedValue({
      status: "decided",
      graph: "general",
      wants_report: true,
      reason: "要把一批文件合并成一份交付物",
      question: null,
      options: [],
    });
    const user = userEvent.setup();
    renderWorkPage();

    await user.type(screen.getByLabelText("目标"), "把这批 CSV 清洗合并出一份对账表");
    await user.click(screen.getByRole("button", { name: "创建任务" }));

    await waitFor(() => expect(createTask).toHaveBeenCalledTimes(1));
    expect(vi.mocked(createTask).mock.calls[0]?.[1]).toEqual({
      objective: "把这批 CSV 清洗合并出一份对账表",
      maxRevisions: 2,
      wantsReport: true,
      graph: "general",
      intent: {
        graph_decided_by: "model",
        wants_report_decided_by: "model",
        reason: "要把一批文件合并成一份交付物",
      },
    });
  });

  it("turns an unsure triage into a question, and the answer into an explicit choice", async () => {
    vi.mocked(createTask).mockReset();
    vi.mocked(createTask).mockResolvedValue({
      task_id: "task_created",
      status: "queued",
      status_detail: null,
      agent_invocation_count: 0,
      objective_preview: null,
      created_at: "2026-08-02T12:00:00Z",
      updated_at: "2026-08-02T12:00:00Z",
    });
    vi.mocked(triageTask).mockResolvedValue({
      status: "ask",
      graph: null,
      wants_report: null,
      reason: null,
      question: "是要一份调研报告，还是直接把事做完？",
      options: [
        { graph: "research", label: "调研报告" },
        { graph: "general", label: "通用执行" },
      ],
    });
    const user = userEvent.setup();
    renderWorkPage();

    await user.type(screen.getByLabelText("目标"), "研究一下这批反馈");
    await user.click(screen.getByRole("button", { name: "创建任务" }));

    // No Task yet: uncertainty is a question, never a submission (ADR-036).
    await screen.findByText("是要一份调研报告，还是直接把事做完？");
    expect(createTask).not.toHaveBeenCalled();

    await user.click(screen.getByRole("button", { name: "通用执行" }));
    await waitFor(() => expect(createTask).toHaveBeenCalledTimes(1));
    const input = vi.mocked(createTask).mock.calls[0]?.[1];
    expect(input?.graph).toBe("general");
    // The chip is the reader answering, so the record says "user".
    expect(input?.intent?.graph_decided_by).toBe("user");
  });

  it("skips triage entirely for an explicit override, which outranks any guess", async () => {
    vi.mocked(createTask).mockReset();
    vi.mocked(createTask).mockResolvedValue({
      task_id: "task_created",
      status: "queued",
      status_detail: null,
      agent_invocation_count: 0,
      objective_preview: null,
      created_at: "2026-08-02T12:00:00Z",
      updated_at: "2026-08-02T12:00:00Z",
    });
    const user = userEvent.setup();
    renderWorkPage();

    // An objective that *sounds* like research, submitted as 通用执行 via the
    // advanced override: the wording must not decide the pipeline, and an
    // explicit choice must not even consult the model (the control group is
    // the decided-verdict test above, where triageTask is called once).
    await user.type(screen.getByLabelText("目标"), "调研并把这批 CSV 清洗合并");
    await user.click(screen.getByText("高级设置"));
    await user.click(screen.getByRole("radio", { name: /通用执行/ }));
    await user.click(screen.getByRole("button", { name: "创建任务" }));

    await waitFor(() => expect(createTask).toHaveBeenCalledTimes(1));
    expect(triageTask).not.toHaveBeenCalled();
    const input = vi.mocked(createTask).mock.calls[0]?.[1];
    expect(input?.graph).toBe("general");
    expect(input?.intent?.graph_decided_by).toBe("user");
  });

  it("an explicit report choice overrides the verdict's", async () => {
    vi.mocked(createTask).mockReset();
    vi.mocked(createTask).mockResolvedValue({
      task_id: "task_created",
      status: "queued",
      status_detail: null,
      agent_invocation_count: 0,
      objective_preview: null,
      created_at: "2026-08-02T12:00:00Z",
      updated_at: "2026-08-02T12:00:00Z",
    });
    vi.mocked(triageTask).mockResolvedValue({
      status: "decided",
      graph: "research",
      wants_report: true,
      reason: "像调研",
      question: null,
      options: [],
    });
    const user = userEvent.setup();
    renderWorkPage();

    await user.type(screen.getByLabelText("目标"), "比较三个方案");
    await user.click(screen.getByText("高级设置"));
    await user.selectOptions(screen.getByLabelText("报告文件"), "no");
    await user.click(screen.getByRole("button", { name: "创建任务" }));

    await waitFor(() => expect(createTask).toHaveBeenCalledTimes(1));
    const input = vi.mocked(createTask).mock.calls[0]?.[1];
    // The verdict said true; the reader said no. The reader wins and the
    // record says so.
    expect(input?.wantsReport).toBe(false);
    expect(input?.intent?.wants_report_decided_by).toBe("user");
    expect(input?.intent?.graph_decided_by).toBe("model");
  });

  it("lists tasks by what they were asked to do, not by id", async () => {
    vi.mocked(listTasks).mockResolvedValue({
      tasks: [
        {
          task_id: "task_969398ecc7b14fbd9f24a50f53fbad7e",
          status: "queued",
          status_detail: null,
          agent_invocation_count: 0,
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

  it("offers delete on a settled task and withholds it from a running one", async () => {
    vi.mocked(listTasks).mockResolvedValue({
      tasks: [
        {
          task_id: "task_settled",
          status: "succeeded",
          status_detail: null,
          agent_invocation_count: 1,
          objective_preview: "已经跑完的任务",
          created_at: "2026-08-02T12:00:00Z",
          updated_at: "2026-08-02T12:00:00Z",
        },
        {
          task_id: "task_running",
          status: "running",
          status_detail: null,
          agent_invocation_count: 1,
          objective_preview: "还在跑的任务",
          created_at: "2026-08-02T12:00:00Z",
          updated_at: "2026-08-02T12:00:00Z",
        },
      ],
      cursor: null,
    });
    const user = userEvent.setup();
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
    vi.mocked(deleteTask).mockResolvedValue({ task_id: "task_settled" });

    renderWorkPage();

    // The server answers 409 for anything that has not settled, so a control
    // on a running row would be a button whose only outcome is an error.
    expect(
      await screen.findByRole("button", { name: "删除任务 已经跑完的任务" }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "删除任务 还在跑的任务" }),
    ).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "删除任务 已经跑完的任务" }));
    await waitFor(() => {
      expect(vi.mocked(deleteTask).mock.calls[0]?.[1]).toBe("task_settled");
    });
    expect(confirm).toHaveBeenCalled();
  });

  it("still opens a task the server recorded no objective for", async () => {
    vi.mocked(listTasks).mockResolvedValue({
      tasks: [
        {
          task_id: "task_969398ecc7b14fbd9f24a50f53fbad7e",
          status: "queued",
          status_detail: null,
          agent_invocation_count: 0,
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
      agent_invocation_count: 0,
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

    // Opening the stage shows the *step*: one line naming what the agent did,
    // not the five lifecycle events it emitted doing it.
    //
    // Two elements carry these words now: the collapsed stage's digest, which
    // is a preview of what is inside, and the step itself. The step is the one
    // that opens, so it is the one this test wants -- `getAllByText` and the
    // last match rather than `getByText`, which now finds both.
    const matches = screen.getAllByText("搜索网络");
    const folded = matches[matches.length - 1] as HTMLElement;
    expect(folded).toBeVisible();

    // And the events are still underneath it, one click further in. The fold
    // decides what the line says; it never decides what may be seen.
    expect(step).not.toBeVisible();
    await user.click(folded);
    expect(step).toBeVisible();

    // The file is in the rail, named for what it is, without hunting for the
    // step that wrote it.
    const rail = screen.getByRole("complementary", { name: "附件" });
    expect(within(rail).getByText("检索到的证据")).toBeInTheDocument();
  });

  it("says the history is incomplete, and which two steps the hole fell between", async () => {
    vi.mocked(getTask).mockResolvedValue({
      task_id: "task_run",
      status: "succeeded",
      status_detail: null,
      agent_invocation_count: 0,
      objective_preview: "今天丹东天气怎么样",
      created_at: "2026-08-02T12:00:00Z",
      updated_at: "2026-08-02T12:01:00Z",
    });
    vi.mocked(getTaskTimeline).mockResolvedValue(damagedTimeline());
    renderWorkPage("/work/task_run");

    // The steps that did arrive are on screen, and shorter by one -- which on
    // its own is also what the end of a stream looks like. The page has to say
    // which of the two it is looking at.
    const notice = await screen.findByText(/这段历史不完整/);
    expect(notice).toHaveTextContent("1 个位置");

    // And say it with the position, placed between the steps either side of
    // it: that is what the server sends positions instead of a count for.
    const gap = screen.getByText(
      "#2：在「工具调用已开始：external_search」与「任务成功完成」之间",
    );
    // In the reading column with the run it is about, not in the file rail --
    // a reader has to meet this while looking at the steps it qualifies.
    expect(gap.closest(".aw-work-run")).not.toBeNull();
  });

  it("stays quiet about damage on a Task whose pages all came back whole", async () => {
    // The control group. A page that always warns would tell every reader
    // their history is broken, and would pass the test above unchanged.
    vi.mocked(getTask).mockResolvedValue({
      task_id: "task_run",
      status: "succeeded",
      status_detail: null,
      agent_invocation_count: 0,
      objective_preview: "今天丹东天气怎么样",
      created_at: "2026-08-02T12:00:00Z",
      updated_at: "2026-08-02T12:01:00Z",
    });
    vi.mocked(getTaskTimeline).mockResolvedValue(runTimeline());
    renderWorkPage("/work/task_run");

    // Waited for, so this is a rendered timeline saying nothing rather than an
    // empty page not having got there yet.
    expect(await screen.findByText("收集资料")).toBeInTheDocument();
    expect(screen.queryByText(/这段历史不完整/)).toBeNull();
  });

  it("renders the produced report under the run, behind one download control", async () => {
    vi.mocked(getTask).mockResolvedValue({
      task_id: "task_run",
      status: "succeeded",
      status_detail: null,
      agent_invocation_count: 0,
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

    // One control, and it is labelled. It used to be a bare icon on the theory
    // that the filename beside it said enough; in use it sat at the end of a
    // header row and was routinely missed. The file is what the reader came
    // for, so the way to keep it is spelled out.
    const downloads = screen.getAllByRole("button", { name: /^下载/ });
    expect(downloads).toHaveLength(1);
    expect(downloads[0]).toHaveTextContent("下载");
  });

  it("opens a Word document on its layout, without being asked", async () => {
    // What the Task was asked for is the document, so the document is what
    // opens. The text view used to be the default and the conversion sat
    // behind a control nothing pointed at -- a reader who asked for a Word
    // file was met with markdown-looking prose and concluded the Task had
    // produced plain text.
    vi.mocked(getTask).mockResolvedValue({
      task_id: "task_run",
      status: "succeeded",
      status_detail: null,
      agent_invocation_count: 0,
      objective_preview: "写一份季度报告",
      created_at: "2026-08-02T12:00:00Z",
      updated_at: "2026-08-02T12:01:00Z",
    });
    vi.mocked(getTaskTimeline).mockResolvedValue(docxTimeline());
    vi.mocked(getDocumentPreview).mockResolvedValue(documentPreview());
    vi.mocked(getDocumentPdf).mockResolvedValue({
      available: true,
      blob: new Blob(["%PDF-1.7"], { type: "application/pdf" }),
    });
    renderWorkPage("/work/task_run");

    const output = await screen.findByRole("region", { name: "任务产出" });
    // No click before this line: the layout arrives because the file did.
    const frame = await within(output).findByTitle("版面预览");
    expect(frame.getAttribute("src")).toMatch(/^blob:/);
    expect(vi.mocked(getDocumentPdf)).toHaveBeenCalledWith(
      expect.anything(),
      "art_report",
    );
    // The control tells the truth about which view is up.
    expect(within(output).getByRole("button", { name: "版面" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    // One view at a time: the laid-out page replaces the text rather than
    // stacking on top of it.
    expect(within(output).queryByText("这一段来自文档。")).not.toBeInTheDocument();
    // Still not the blob fetch, which would send a text reader at a zip.
    expect(vi.mocked(getArtifactText)).not.toHaveBeenCalled();
    // And still keepable.
    expect(
      within(output).getByRole("button", { name: /^下载/ }),
    ).toBeInTheDocument();
  });

  it("keeps the text view one click away, honest about what it drops", async () => {
    // The old default's guarantees do not lapse with the default: a .docx must
    // not fall through to "这个类型只能下载查看", and its text view still
    // counts what it dropped instead of posing as the document.
    vi.mocked(getTask).mockResolvedValue({
      task_id: "task_run",
      status: "succeeded",
      status_detail: null,
      agent_invocation_count: 0,
      objective_preview: "写一份季度报告",
      created_at: "2026-08-02T12:00:00Z",
      updated_at: "2026-08-02T12:01:00Z",
    });
    vi.mocked(getTaskTimeline).mockResolvedValue(docxTimeline());
    vi.mocked(getDocumentPreview).mockResolvedValue(documentPreview({ table_count: 2 }));
    vi.mocked(getDocumentPdf).mockResolvedValue({
      available: true,
      blob: new Blob(["%PDF-1.7"], { type: "application/pdf" }),
    });
    const user = userEvent.setup();
    renderWorkPage("/work/task_run");

    const output = await screen.findByRole("region", { name: "任务产出" });
    await within(output).findByTitle("版面预览");
    await user.click(within(output).getByRole("button", { name: "文字" }));

    expect(await within(output).findByText("这一段来自文档。")).toBeInTheDocument();
    expect(vi.mocked(getDocumentPreview)).toHaveBeenCalledWith(
      expect.anything(),
      "art_report",
    );
    const gaps = within(output).getByRole("list", { name: "预览没有还原的部分" });
    expect(within(gaps).getByText("表格只保留文字")).toBeInTheDocument();
    expect(within(gaps).getByText("2 张")).toBeInTheDocument();
    expect(vi.mocked(getArtifactText)).not.toHaveBeenCalled();
  });

  it("opens a document from the rail in the reading column", async () => {
    // The .docx a Task is asked for arrives as a tool result, not as the
    // exported report -- so it only ever appears in the rail, and the rail
    // only ever downloaded. The page could render the document and had no way
    // to be asked to.
    vi.mocked(getTask).mockResolvedValue({
      task_id: "task_run",
      status: "succeeded",
      status_detail: null,
      agent_invocation_count: 0,
      objective_preview: "写一份季度报告",
      created_at: "2026-08-02T12:00:00Z",
      updated_at: "2026-08-02T12:01:00Z",
    });
    vi.mocked(getTaskTimeline).mockResolvedValue(railDocxTimeline());
    vi.mocked(getDocumentPreview).mockResolvedValue(
      documentPreview({ text: "## 季度回顾\n\n这一段来自 Word 文档。", table_count: 1 }),
    );
    vi.mocked(getDocumentPdf).mockResolvedValue({
      available: true,
      blob: new Blob(["%PDF-1.7"], { type: "application/pdf" }),
    });
    const user = userEvent.setup();
    renderWorkPage("/work/task_run");

    const rail = await screen.findByRole("complementary", { name: "附件" });
    await user.click(within(rail).getByRole("button", { name: /季度总结\.docx/ }));

    const output = await screen.findByRole("region", { name: "任务产出" });
    // Laid out on arrival, and it is this file's conversion -- not one carried
    // over from whatever the column showed before.
    expect(await within(output).findByTitle("版面预览")).toBeInTheDocument();
    expect(vi.mocked(getDocumentPdf)).toHaveBeenCalledWith(
      expect.anything(),
      "art_docx",
    );
    // Shown, and still keepable.
    expect(
      within(output).getByRole("button", { name: /^下载/ }),
    ).toBeInTheDocument();

    // This .docx *is* the Task's product, so it is what the column leads with
    // and there is nothing to return to. The report the graph also exported is
    // still reachable from the rail; what changed is which of the two a reader
    // is shown without asking.
    expect(
      within(output).queryByRole("button", { name: "返回任务结果" }),
    ).not.toBeInTheDocument();
    expect(await within(rail).findByText(/report\.md|报告文件/)).toBeInTheDocument();
  });

  it("returns to the deliverable from a rail file that is not it", async () => {
    /**
     * The control for suppressing that button. It is hidden only when closing
     * would land on the very file already open; every other rail entry still
     * has a way back, and losing that would strand a reader on the evidence
     * bundle.
     */
    vi.mocked(getTask).mockResolvedValue({
      task_id: "task_run",
      status: "succeeded",
      status_detail: null,
      agent_invocation_count: 0,
      objective_preview: "写一份季度报告",
      created_at: "2026-08-02T12:00:00Z",
      updated_at: "2026-08-02T12:01:00Z",
    });
    vi.mocked(getTaskTimeline).mockResolvedValue(railDocxTimeline());
    vi.mocked(getDocumentPreview).mockResolvedValue(
      documentPreview({ text: "## 季度回顾", table_count: 1 }),
    );
    vi.mocked(getDocumentPdf).mockResolvedValue({
      available: true,
      blob: new Blob(["%PDF-1.7"], { type: "application/pdf" }),
    });
    const user = userEvent.setup();
    renderWorkPage("/work/task_run");

    const rail = await screen.findByRole("complementary", { name: "附件" });
    await user.click(within(rail).getByRole("button", { name: /报告文件|report\.md/ }));

    const output = await screen.findByRole("region", { name: "任务产出" });
    await user.click(within(output).getByRole("button", { name: "返回任务结果" }));

    expect(
      await within(await screen.findByRole("region", { name: "任务产出" })).findByText(
        /季度总结\.docx/,
      ),
    ).toBeInTheDocument();
  });

  it("says when a success shipped with the reviewer still unsatisfied", async () => {
    // ADR-060: an exhausted reviewer annotates rather than vetoes, and the
    // annotation lands on the Task row as `status_detail`. Hiding it would
    // present a disputed draft as a clean pass.
    vi.mocked(getTask).mockResolvedValue({
      task_id: "task_run",
      status: "succeeded",
      status_detail:
        "the reviewer still saw 1 unresolved issue(s) after 2 revision(s): thin",
      agent_invocation_count: 0,
      objective_preview: "比较三个方案并输出一份建议报告",
      created_at: "2026-08-02T12:00:00Z",
      updated_at: "2026-08-02T12:01:00Z",
    });
    vi.mocked(getTaskTimeline).mockResolvedValue(reportTimeline());
    renderWorkPage("/work/task_run");

    const output = await screen.findByRole("region", { name: "任务产出" });
    expect(
      within(output).getByText("评审仍有未解决的意见，产物按现状导出"),
    ).toBeInTheDocument();
    expect(
      within(output).getByText(/1 unresolved issue/),
    ).toBeInTheDocument();
    // The product itself is still the headline, not the caveat.
    expect(within(output).getByText("report.md")).toBeInTheDocument();
  });

  it("opens a picture from the rail as a picture, not a download", async () => {
    // A produced .png used to download on click with no feedback and show
    // nothing -- the rail pre-judged "not previewable" and its list was stale
    // the moment the column learned a new kind. Now the click opens, the
    // column shows the picture, and the one download button stays the only
    // way bytes leave the page.
    vi.mocked(getTask).mockResolvedValue({
      task_id: "task_run",
      status: "succeeded",
      status_detail: null,
      agent_invocation_count: 0,
      objective_preview: "画一张销售趋势图",
      created_at: "2026-08-02T12:00:00Z",
      updated_at: "2026-08-02T12:01:00Z",
    });
    vi.mocked(getTaskTimeline).mockResolvedValue(
      railFileTimeline({
        artifact_id: "art_plot",
        media_type: "image/png",
        filename: "图表.png",
        size_bytes: 4096,
      }),
    );
    vi.mocked(getArtifactBlob).mockResolvedValue(
      new Blob(["png-bytes"], { type: "image/png" }),
    );
    const user = userEvent.setup();
    renderWorkPage("/work/task_run");

    const rail = await screen.findByRole("complementary", { name: "附件" });
    await user.click(within(rail).getByRole("button", { name: /图表\.png/ }));

    const output = await screen.findByRole("region", { name: "任务产出" });
    const image = await within(output).findByRole("img", { name: "图表.png" });
    expect(image.getAttribute("src")).toMatch(/^blob:/);
    expect(vi.mocked(getArtifactBlob)).toHaveBeenCalledWith(
      expect.anything(),
      "art_plot",
    );
    expect(vi.mocked(downloadArtifact)).not.toHaveBeenCalled();
    // Still exactly one labelled download control on the page.
    expect(screen.getAllByRole("button", { name: /^下载/ })).toHaveLength(1);
  });

  it("opens an unshowable type in the reading column instead of silently downloading", async () => {
    // The honest leaf: no viewer exists for a zip, and the column says so.
    // What must not happen is the old behaviour -- bytes saved to disk as the
    // response to a click that asked to look.
    vi.mocked(getTask).mockResolvedValue({
      task_id: "task_run",
      status: "succeeded",
      status_detail: null,
      agent_invocation_count: 0,
      objective_preview: "打包结果数据",
      created_at: "2026-08-02T12:00:00Z",
      updated_at: "2026-08-02T12:01:00Z",
    });
    vi.mocked(getTaskTimeline).mockResolvedValue(
      railFileTimeline({
        artifact_id: "art_zip",
        media_type: "application/zip",
        filename: "数据包.zip",
        size_bytes: 10240,
      }),
    );
    const user = userEvent.setup();
    renderWorkPage("/work/task_run");

    const rail = await screen.findByRole("complementary", { name: "附件" });
    await user.click(within(rail).getByRole("button", { name: /数据包\.zip/ }));

    const output = await screen.findByRole("region", { name: "任务产出" });
    expect(
      await within(output).findByText(/这个类型只能下载查看/),
    ).toBeInTheDocument();
    expect(vi.mocked(downloadArtifact)).not.toHaveBeenCalled();
    expect(vi.mocked(getArtifactBlob)).not.toHaveBeenCalled();
  });

  it("keeps the document downloadable when its text cannot be extracted", async () => {
    // The control. A failed extraction is a failed *convenience*; reporting it
    // as a lost document would be worse than showing nothing.
    vi.mocked(getTask).mockResolvedValue({
      task_id: "task_run",
      status: "succeeded",
      status_detail: null,
      agent_invocation_count: 0,
      objective_preview: "写一份季度报告",
      created_at: "2026-08-02T12:00:00Z",
      updated_at: "2026-08-02T12:01:00Z",
    });
    vi.mocked(getTaskTimeline).mockResolvedValue(docxTimeline());
    vi.mocked(getDocumentPreview).mockRejectedValue(new Error("解析失败"));
    // Answered but never rendered: a failed extraction fronts the panel before
    // either view. The mock is here because opening a .docx now asks for the
    // conversion regardless.
    vi.mocked(getDocumentPdf).mockResolvedValue({
      available: false,
      reason: "converter_unavailable",
    });
    renderWorkPage("/work/task_run");

    const output = await screen.findByRole("region", { name: "任务产出" });
    expect(
      await within(output).findByText(/文件本身没有问题/),
    ).toBeInTheDocument();
    expect(
      within(output).getByRole("button", { name: /^下载/ }),
    ).toBeInTheDocument();
  });

  it("lets the reader leave the layout and come back without a second conversion", async () => {
    // The text preview answers "what does it say"; the layout answers "what
    // does it look like" and now opens first. This pins the round trip between
    // them, and the lifetime of the URL the frame reads.
    vi.mocked(getTask).mockResolvedValue({
      task_id: "task_run",
      status: "succeeded",
      status_detail: null,
      agent_invocation_count: 0,
      objective_preview: "写一份季度报告",
      created_at: "2026-08-02T12:00:00Z",
      updated_at: "2026-08-02T12:01:00Z",
    });
    vi.mocked(getTaskTimeline).mockResolvedValue(docxTimeline());
    vi.mocked(getDocumentPreview).mockResolvedValue(documentPreview());
    vi.mocked(getDocumentPdf).mockResolvedValue({
      available: true,
      blob: new Blob(["%PDF-1.7"], { type: "application/pdf" }),
    });
    const revoke = vi.spyOn(URL, "revokeObjectURL");
    const user = userEvent.setup();
    renderWorkPage("/work/task_run");

    const output = await screen.findByRole("region", { name: "任务产出" });
    const frame = await within(output).findByTitle("版面预览");
    // The frame reads a blob this page holds, not the endpoint: a frame issues
    // its own request and carries none of the identity headers, so pointing it
    // at /v1/artifacts/... would render a 404 inside the panel.
    const source = frame.getAttribute("src");
    expect(source).toMatch(/^blob:/);
    // Two views of one file, not two panels: the text is gone while the layout
    // is up, so a reader is never scrolling past the wrong one.
    expect(within(output).queryByText("这一段来自文档。")).not.toBeInTheDocument();
    // And the URL is alive for as long as the frame is reading it. Revoking on
    // the next line -- which is what the download path does, correctly, after a
    // click has consumed it -- blanks the panel.
    expect(revoke).not.toHaveBeenCalled();

    await user.click(within(output).getByRole("button", { name: "文字" }));
    expect(await within(output).findByText("这一段来自文档。")).toBeInTheDocument();
    // Handed back when the frame goes. One un-revoked URL per preview is a leak
    // the reader pays for by opening files.
    expect(revoke).toHaveBeenCalledWith(source);

    await user.click(within(output).getByRole("button", { name: "版面" }));
    expect(await within(output).findByTitle("版面预览")).toBeInTheDocument();
    // Served from the cache the first request filled. A converter run costs
    // seconds on the server; toggling views must not buy another one.
    expect(vi.mocked(getDocumentPdf)).toHaveBeenCalledTimes(1);
  });

  it("falls back to the text preview, saying why, when the deployment cannot lay a document out", async () => {
    // The control for the case above, and the one that decides whether the
    // default is safe: converting .docx to PDF needs a program on the server,
    // a deployment without one is correctly configured for everything except
    // this panel -- and now every opened document walks into that fact rather
    // than the readers who pressed a button. The fallback must arrive on its
    // own and say what happened; a silent landing on text would read as "the
    // task produced plain text", which is the misreading this page just spent
    // a default to avoid.
    vi.mocked(getTask).mockResolvedValue({
      task_id: "task_run",
      status: "succeeded",
      status_detail: null,
      agent_invocation_count: 0,
      objective_preview: "写一份季度报告",
      created_at: "2026-08-02T12:00:00Z",
      updated_at: "2026-08-02T12:01:00Z",
    });
    vi.mocked(getTaskTimeline).mockResolvedValue(docxTimeline());
    vi.mocked(getDocumentPreview).mockResolvedValue(documentPreview());
    vi.mocked(getDocumentPdf).mockResolvedValue({
      available: false,
      reason: "converter_unavailable",
    });
    renderWorkPage("/work/task_run");

    const output = await screen.findByRole("region", { name: "任务产出" });
    // No click anywhere in this test: the decline and its explanation land
    // unprompted, because the request they answer was unprompted too.
    expect(
      await within(output).findByText(/服务器上没有可用的文档转换器/),
    ).toBeInTheDocument();
    // Nothing failed for the reader: the text is intact and the file is
    // unchanged. An alert here would report the shape of a deployment as a
    // fault, and would cast doubt on a preview that is fine.
    expect(within(output).queryByRole("alert")).not.toBeInTheDocument();
    expect(within(output).getByText("这一段来自文档。")).toBeInTheDocument();
    expect(within(output).queryByTitle("版面预览")).not.toBeInTheDocument();
    expect(
      within(output).getByRole("button", { name: /^下载/ }),
    ).toBeInTheDocument();
    // The control that already refused stops offering, and the one showing the
    // view the reader is actually looking at is the one lit.
    expect(within(output).getByRole("button", { name: "版面" })).toBeDisabled();
    expect(within(output).getByRole("button", { name: "文字" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
  });

  it("does not ask for a layout no browser of this kind can paint", async () => {
    // A browser without a PDF viewer leaves the frame showing its own empty
    // backdrop -- a flat dark rectangle, no error, no event. Reported as a
    // black panel over a Word document that had rendered perfectly well, which
    // reads as a broken deliverable rather than as a missing viewer.
    // Defined rather than spied on: jsdom's navigator does not carry this
    // property at all, which is also why every other test in this file runs
    // through the "absent, so try the frame" branch -- they are the control
    // for this one.
    Object.defineProperty(navigator, "pdfViewerEnabled", {
      configurable: true,
      get: () => false,
    });
    try {
      vi.mocked(getTask).mockResolvedValue({
        task_id: "task_run",
        status: "succeeded",
        status_detail: null,
        agent_invocation_count: 0,
        objective_preview: "写一份季度报告",
        created_at: "2026-08-02T12:00:00Z",
        updated_at: "2026-08-02T12:01:00Z",
      });
      vi.mocked(getTaskTimeline).mockResolvedValue(docxTimeline());
      vi.mocked(getDocumentPreview).mockResolvedValue(documentPreview());
      vi.mocked(getDocumentPdf).mockResolvedValue({
        available: true,
        blob: new Blob(["%PDF-1.7"], { type: "application/pdf" }),
      });
      renderWorkPage("/work/task_run");

      const output = await screen.findByRole("region", { name: "任务产出" });
      expect(
        await within(output).findByText(/这个浏览器不显示内嵌 PDF/),
      ).toBeInTheDocument();
      // The frame is the whole failure mode, so its absence is the assertion.
      expect(within(output).queryByTitle("版面预览")).not.toBeInTheDocument();
      // Not a fault: the document converted, the text is intact, the file
      // downloads unchanged. Only this browser cannot show one of the views.
      expect(within(output).queryByRole("alert")).not.toBeInTheDocument();
      expect(within(output).getByText("这一段来自文档。")).toBeInTheDocument();
      expect(
        within(output).getByRole("button", { name: /^下载/ }),
      ).toBeInTheDocument();
      expect(
        within(output).getByRole("button", { name: "版面" }),
      ).toBeDisabled();
      // And the conversion is never requested. The server would have started an
      // external converter for a frame that shows nobody anything.
      expect(vi.mocked(getDocumentPdf)).not.toHaveBeenCalled();
    } finally {
      Reflect.deleteProperty(navigator, "pdfViewerEnabled");
    }
  });

  it("counts what the text preview dropped, one line per kind", async () => {
    vi.mocked(getTask).mockResolvedValue({
      task_id: "task_run",
      status: "succeeded",
      status_detail: null,
      agent_invocation_count: 0,
      objective_preview: "写一份季度报告",
      created_at: "2026-08-02T12:00:00Z",
      updated_at: "2026-08-02T12:01:00Z",
    });
    vi.mocked(getTaskTimeline).mockResolvedValue(docxTimeline());
    vi.mocked(getDocumentPreview).mockResolvedValue(
      documentPreview({
        image_count: 4,
        header_count: 1,
        flattened_paragraph_count: 3,
      }),
    );
    const output = await openDocumentTextView();

    const gaps = await within(output).findByRole("list", {
      name: "预览没有还原的部分",
    });
    // A picture is the sharpest case for counting anything: the prose around a
    // figure reads as a finished argument, so its absence is the one omission a
    // reader cannot infer from what is on screen.
    expect(within(gaps).getByText("图片没有显示")).toBeInTheDocument();
    expect(within(gaps).getByText("4 张")).toBeInTheDocument();
    expect(within(gaps).getByText("页眉没有显示")).toBeInTheDocument();
    expect(within(gaps).getByText("段落样式没有保留")).toBeInTheDocument();
    expect(within(gaps).getByText("3 段")).toBeInTheDocument();
    // Zeros are not rows. A document with no footnotes is missing nothing on
    // that axis, and a row saying so competes with the rows that mean
    // something.
    expect(within(gaps).queryByText("脚注没有显示")).not.toBeInTheDocument();
    expect(within(gaps).queryByText("表格只保留文字")).not.toBeInTheDocument();
    expect(within(gaps).queryByText(/^0 /)).not.toBeInTheDocument();
    // The text is whole here, and the row about the cut is conditional on that
    // being false. Without this the row could render for every document -- a
    // list of real losses is exactly where an invented one would be believed.
    expect(within(gaps).queryByText(/正文只显示到这里/)).not.toBeInTheDocument();
  });

  it("does not present a truncated preview as one that lost nothing", async () => {
    // The counts are all zero here and all seven are truthful: this document
    // holds no picture, no footnote, no table. What it does hold is more prose
    // than the preview read, and none of the seven moves for that -- they are
    // of the whole document (`adapters/documents/docx.py`), so a plain document
    // cut in half scores zero on every axis. Rendering nothing is how this page
    // says the preview is faithful, so nothing is the one thing it must not
    // render here.
    vi.mocked(getTask).mockResolvedValue({
      task_id: "task_run",
      status: "succeeded",
      status_detail: null,
      agent_invocation_count: 0,
      objective_preview: "写一份季度报告",
      created_at: "2026-08-02T12:00:00Z",
      updated_at: "2026-08-02T12:01:00Z",
    });
    vi.mocked(getTaskTimeline).mockResolvedValue(docxTimeline());
    vi.mocked(getDocumentPreview).mockResolvedValue(
      documentPreview({ truncated: true }),
    );
    const output = await openDocumentTextView();

    const gaps = await within(output).findByRole("list", {
      name: "预览没有还原的部分",
    });
    expect(within(gaps).getByText(/正文只显示到这里/)).toBeInTheDocument();
    // No invented rows to carry it. The cut is the one loss this document has,
    // and it is the one row with no number, because what is missing is the part
    // that was never read.
    expect(within(gaps).queryByText(/^0 /)).not.toBeInTheDocument();
    expect(within(gaps).queryByText("图片没有显示")).not.toBeInTheDocument();
    // And nothing to read the numbers against, so the clause about them stays
    // out of the sentence.
    expect(within(gaps).queryByText(/整份文档/)).not.toBeInTheDocument();
    // Once. The cut used to be a note above this list, and a fact stated in two
    // adjacent places is how one of them drifts.
    expect(within(output).getAllByText(/完整内容请下载/)).toHaveLength(1);
  });

  it("says which document the counts are of when the text stops early", async () => {
    // The counts are of the whole file and the text is not, which is the one
    // place those two can be read as the same scope: "4 张" under a preview
    // that stops early invites "four so far", and a reader who takes that
    // reading concludes the rest of the document is prose.
    vi.mocked(getTask).mockResolvedValue({
      task_id: "task_run",
      status: "succeeded",
      status_detail: null,
      agent_invocation_count: 0,
      objective_preview: "写一份季度报告",
      created_at: "2026-08-02T12:00:00Z",
      updated_at: "2026-08-02T12:01:00Z",
    });
    vi.mocked(getTaskTimeline).mockResolvedValue(docxTimeline());
    vi.mocked(getDocumentPreview).mockResolvedValue(
      documentPreview({ truncated: true, image_count: 4 }),
    );
    const output = await openDocumentTextView();

    const gaps = await within(output).findByRole("list", {
      name: "预览没有还原的部分",
    });
    expect(within(gaps).getByText(/按整份文档数的/)).toBeInTheDocument();
    expect(within(gaps).getByText("图片没有显示")).toBeInTheDocument();
    expect(within(gaps).getByText("4 张")).toBeInTheDocument();
  });

  it("says nothing about losses when the preview lost nothing", async () => {
    // The control for the counts, and now for the cut as well: the same fixture
    // as the truncated test above with `truncated: false`, so the only thing
    // deciding whether this page claims faithfulness is whether it read the
    // whole document. Without it, a list that rendered every kind
    // unconditionally -- or an empty box with a heading over it -- would pass
    // the test above while telling every reader of a plain document that
    // something is missing from it.
    vi.mocked(getTask).mockResolvedValue({
      task_id: "task_run",
      status: "succeeded",
      status_detail: null,
      agent_invocation_count: 0,
      objective_preview: "写一份季度报告",
      created_at: "2026-08-02T12:00:00Z",
      updated_at: "2026-08-02T12:01:00Z",
    });
    vi.mocked(getTaskTimeline).mockResolvedValue(docxTimeline());
    vi.mocked(getDocumentPreview).mockResolvedValue(documentPreview());
    const output = await openDocumentTextView();

    expect(await within(output).findByText("这一段来自文档。")).toBeInTheDocument();
    expect(
      within(output).queryByRole("list", { name: "预览没有还原的部分" }),
    ).not.toBeInTheDocument();
    expect(within(output).queryByText(/没有显示/)).not.toBeInTheDocument();
    // The one sentence that is still true of every text preview stays.
    expect(within(output).getByText(/不含排版/)).toBeInTheDocument();
  });

  it("presents an answer with no file as the result, not as a missing file", async () => {
    vi.mocked(getTask).mockResolvedValue({
      task_id: "task_run",
      status: "succeeded",
      status_detail: null,
      agent_invocation_count: 0,
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
      agent_invocation_count: 0,
      objective_preview: "输出一份建议报告",
      created_at: "2026-08-02T12:00:00Z",
      updated_at: "2026-08-02T12:01:00Z",
    });
    vi.mocked(getTaskTimeline).mockResolvedValue(answerTimeline());
    vi.mocked(getArtifactJson).mockResolvedValue(taskInput(true));
    renderWorkPage("/work/task_run");

    expect(await screen.findByText("没有生成文件")).toBeInTheDocument();
  });

  it("shows the draft the approval is a decision about, before deciding", async () => {
    vi.mocked(getTask).mockResolvedValue(task("waiting_approval"));
    vi.mocked(getTaskTimeline).mockResolvedValue(approvalTimelineWithDraft());
    renderWorkPage("/work/task_approval");

    // The decision controls are worth nothing without the text they are about;
    // `waiting_approval` used to render as a spinner beside a live 生成报告
    // button.
    const pending = await screen.findByRole("region", { name: "待确认的内容" });
    expect(within(pending).getByText(/建议先扩产/)).toBeInTheDocument();
    expect(
      await screen.findByRole("button", { name: "生成报告" }),
    ).toBeInTheDocument();
    // The export has not run yet, so nothing is missing a file.
    expect(screen.queryByText(/没有生成文件/)).not.toBeInTheDocument();
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

describe("WorkPage 停住的任务与已用预算", () => {
  beforeEach(() => {
    localStorage.clear();
    vi.mocked(getArtifactJson).mockReset();
    vi.mocked(getTask).mockReset();
    vi.mocked(getTaskTimeline).mockReset();
    vi.mocked(listKnowledgeBases).mockReset();
    vi.mocked(listTasks).mockReset();
    vi.mocked(newIdempotencyKey).mockReset();
    vi.mocked(triageTask).mockReset();
    vi.mocked(newIdempotencyKey).mockReturnValue("task:intent_1");
    vi.mocked(triageTask).mockResolvedValue(TRIAGE_DEFAULT);
    vi.mocked(listTasks).mockResolvedValue({ tasks: [], cursor: null });
    vi.mocked(listKnowledgeBases).mockResolvedValue({ knowledge_bases: [] });
    vi.mocked(getArtifactJson).mockResolvedValue(taskInput(false));
    vi.mocked(getTaskTimeline).mockResolvedValue(parkedTimeline());
  });

  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  it("says a parked task is waiting for a migration, not running", async () => {
    // `waiting_migration` is not terminal, so it used to fall into the same
    // branch as queued and running: a spinner, "任务正在执行", and a poll every
    // three seconds. The status has no outgoing transition at all -- nothing
    // this page can do, and nothing the reader can wait for, will move it.
    vi.mocked(getTask).mockResolvedValue(parkedTask());
    renderWorkPage("/work/task_parked");

    expect(
      await screen.findByText("任务在等待迁移，没有在执行"),
    ).toBeInTheDocument();
    expect(screen.queryByText("任务正在执行，完成后结果会显示在这里")).toBeNull();
    // The server's reason, verbatim -- it is what an operator is handed.
    expect(
      screen.getByText("graph version v1_research is not registered in this process"),
    ).toBeInTheDocument();
    // And the run itself stopped moving too: the stage it reached is waiting,
    // not 进行中, and not 等待你确认 either -- nobody is being asked to decide.
    const stream = screen.getByRole("region", { name: "执行过程" });
    expect(within(stream).getByText("等待迁移")).toBeInTheDocument();
    expect(within(stream).queryByText("进行中")).toBeNull();
    expect(within(stream).queryByText("等待你确认")).toBeNull();
  });

  it("stops polling a parked task", async () => {
    vi.mocked(getTask).mockResolvedValue(parkedTask());
    renderWorkPage("/work/task_parked");
    // Anchored on the heading rather than on the parked notice, so this test
    // fails for its own reason: it is about the fetching, not the wording.
    await screen.findByRole("heading", { name: "整理这批资料，比较三个方案" });
    const polls = vi.mocked(getTask).mock.calls.length;

    // Longer than the 3s status interval, so a page that still treats this as
    // a running Task fetches at least once more inside the wait.
    await new Promise((resolve) => setTimeout(resolve, 4_000));

    expect(vi.mocked(getTask).mock.calls.length).toBe(polls);
  });

  it("shows what a task has spent on agent invocations", async () => {
    // Reported before it is enforced, which is the whole point of ADR-040's
    // middle step: the number has to be visible while it climbs, not first
    // become visible as the reason a Task died.
    vi.mocked(getTask).mockResolvedValue(parkedTask({ agent_invocation_count: 7 }));
    renderWorkPage("/work/task_parked");

    expect(await screen.findByText(/已调用智能体\s*7\s*次/)).toBeInTheDocument();
  });

  it("says nothing about invocations a task never made", async () => {
    vi.mocked(getTask).mockResolvedValue(parkedTask());
    renderWorkPage("/work/task_parked");

    await screen.findByRole("heading", { name: "整理这批资料，比较三个方案" });
    expect(screen.queryByText(/已调用智能体/)).toBeNull();
  });
});

/** A Task the Registry stopped because this deployment cannot run its graph. */
function parkedTask(overrides: { agent_invocation_count?: number } = {}) {
  return {
    task_id: "task_parked",
    status: "waiting_migration" as const,
    // `waiting_migration` is one of the statuses the domain requires a detail
    // on, so a fixture without one would be a Task the server cannot produce.
    status_detail: "graph version v1_research is not registered in this process",
    objective_preview: "整理这批资料，比较三个方案",
    agent_invocation_count: 0,
    created_at: "2026-08-02T12:00:00Z",
    updated_at: "2026-08-02T12:00:30Z",
    ...overrides,
  };
}

/** One stage entered, then nothing -- the shape a parked run leaves behind. */
function parkedTimeline() {
  const base = {
    schema_version: 1,
    stream_id: "stream_parked",
    run_id: "run_parked",
    durability: "durable" as const,
    task_id: "task_parked",
    parent_event_id: null,
  };
  return {
    task_id: "task_parked",
    cursor: "cursor_parked",
    skipped_sequences: [],
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
        event_id: "event_understood",
        event_type: "ModelCompleted",
        timestamp: "2026-08-02T12:00:10Z",
        payload: { kind: "ModelCompleted", text: "先看资料。" },
        sequence: 2,
        graph_node_id: "understand",
      },
    ],
  };
}

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

/**
 * Render the Task and land its document on the text view, the way a reader
 * reaches that view now: the layout opens first, so text is a choice.
 *
 * The tests built on this are about what the *text* preview says of itself --
 * its counts, its cut, its silence -- so they make the choice rather than
 * arrive there by a declined conversion, which would put a deployment's
 * failure note into fixtures that are not about deployments.
 */
async function openDocumentTextView() {
  vi.mocked(getDocumentPdf).mockResolvedValue({
    available: true,
    blob: new Blob(["%PDF-1.7"], { type: "application/pdf" }),
  });
  const user = userEvent.setup();
  renderWorkPage("/work/task_run");
  const output = await screen.findByRole("region", { name: "任务产出" });
  await within(output).findByTitle("版面预览");
  await user.click(within(output).getByRole("button", { name: "文字" }));
  return output;
}

function task(status: "waiting_approval" | "cancelled") {
  return {
    task_id: "task_approval",
    status,
    status_detail: status === "cancelled" ? "cancelled elsewhere" : null,
    agent_invocation_count: 0,
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

/**
 * A preview of a document that lost nothing, so a test states only the loss it
 * is about.
 *
 * Spelling every count out rather than spreading a partial: the wire model
 * requires all of them, so a fixture that forgot one would not compile -- which
 * is the whole reason they are required, and a fixture that quietly defaulted
 * them would be the first place that promise stopped being kept.
 */
function documentPreview(fields: Partial<DocumentPreview> = {}): DocumentPreview {
  return {
    text: "## 背景\n\n这一段来自文档。",
    truncated: false,
    table_count: 0,
    image_count: 0,
    header_count: 0,
    footer_count: 0,
    numbered_paragraph_count: 0,
    footnote_count: 0,
    flattened_paragraph_count: 0,
    ...fields,
  };
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
    skipped_sequences: [],
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

/**
 * `runTimeline` as a stream with one stored row this server could not decode.
 *
 * The same shape the API's own contract test pins from the other side
 * (tests/api/test_task_timeline_skips.py): positions 1 and 3 delivered, 2
 * named as skipped, and a cursor that still moved past it.
 */
function damagedTimeline() {
  const timeline = runTimeline();
  return {
    ...timeline,
    events: timeline.events.filter((event) => event.sequence !== 2),
    skipped_sequences: [2],
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
    skipped_sequences: [],
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
    skipped_sequences: [],
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

/** `reportTimeline`, with the exported file being a Word document. */
function docxTimeline() {
  const timeline = reportTimeline();
  const exported = timeline.events[1];
  if (exported === undefined) throw new Error("fixture lost its export event");
  const payload = exported.payload as { artifact?: Record<string, unknown> };
  if (payload.artifact === undefined) throw new Error("fixture lost its artifact");
  payload.artifact = {
    ...payload.artifact,
    media_type:
      "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    filename: "季度报告.docx",
  };
  return timeline;
}

/**
 * The real shape a Word Task produces: the .docx arrives as a *tool result*
 * beside the exported markdown report, so it lives only in the rail.
 */
function railDocxTimeline() {
  const timeline = reportTimeline();
  const exported = timeline.events[1];
  if (exported === undefined) throw new Error("fixture lost its export event");
  timeline.events.splice(1, 0, {
    ...exported,
    event_id: "event_docx",
    graph_node_id: "work",
    payload: {
      kind: "ToolCompleted",
      tool_call_id: "tc_word",
      artifact: {
        schema_version: 1,
        artifact_id: "art_docx",
        tenant_id: "tenant_local",
        kind: "tool_result",
        media_type:
          "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        size_bytes: 36741,
        sha256: "c".repeat(64),
        filename: "季度总结.docx",
      },
    },
  });
  return timeline;
}

/**
 * `railDocxTimeline`'s shape with an arbitrary tool-result file -- the
 * fixture for the kinds whose rail click used to download silently.
 */
function railFileTimeline(artifact: {
  artifact_id: string;
  media_type: string;
  filename: string;
  size_bytes: number;
}) {
  const timeline = reportTimeline();
  const exported = timeline.events[1];
  if (exported === undefined) throw new Error("fixture lost its export event");
  timeline.events.splice(1, 0, {
    ...exported,
    event_id: "event_rail_file",
    graph_node_id: "work",
    payload: {
      kind: "ToolCompleted",
      tool_call_id: "tc_file",
      artifact: {
        schema_version: 1,
        tenant_id: "tenant_local",
        kind: "tool_result",
        sha256: "d".repeat(64),
        ...artifact,
      },
    },
  });
  return timeline;
}

function approvalTimeline() {
  return {
    task_id: "task_approval",
    cursor: "cursor_approval",
    skipped_sequences: [],
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

/** The same gate, with the draft that reached it still in the timeline. */
function approvalTimelineWithDraft() {
  const base = approvalTimeline();
  return {
    ...base,
    events: [
      {
        schema_version: 1,
        event_id: "event_draft",
        stream_id: "stream_approval",
        run_id: "run_approval",
        event_type: "ModelCompleted",
        durability: "durable" as const,
        timestamp: "2026-08-02T12:00:20Z",
        payload: {
          kind: "ModelCompleted",
          text: "建议先扩产二号线，再谈渠道。",
        },
        sequence: 1,
        task_id: "task_approval",
        graph_node_id: "synthesize",
        parent_event_id: null,
      },
      ...base.events,
    ],
  };
}
