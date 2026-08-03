import {
  useInfiniteQuery,
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import {
  Ban,
  ChevronLeft,
  ClipboardCheck,
  FileDown,
  ListTodo,
  Plus,
  RefreshCw,
} from "lucide-react";
import { type FormEvent, useEffect, useMemo, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import {
  ApiError,
  cancelTask,
  createTask,
  decideApproval,
  downloadArtifact,
  getApproval,
  getArtifactJson,
  getTask,
  listTasks,
  newIdempotencyKey,
} from "../../api/client";
import type { ApprovalView, TaskStatus, TaskView } from "../../api/types";
import { useIdentity } from "../../app/IdentityContext";
import {
  EmptyState,
  ErrorNotice,
  InfoNotice,
  KeyValue,
  LoadingLine,
  StatusPill,
  formatDateTime,
  formatStatus,
  formatTime,
  shortId,
} from "../../components/ui";
import { useTaskTimeline } from "./useTaskTimeline";
import { workIdentityQueryKey } from "./workQueryKeys";
import {
  eventTitle,
  findFinalReport,
  findLatestApprovalId,
  findTaskInputRef,
  groupTimelineEvents,
  isKnownEventType,
  parseTaskInputArtifact,
} from "./workTimeline";

const CANCELLABLE_STATUSES = new Set<TaskStatus>([
  "queued",
  "running",
  "waiting_approval",
]);
const TERMINAL_STATUSES = new Set<TaskStatus>([
  "succeeded",
  "failed",
  "cancelled",
  "dead_letter",
]);

interface CreateTaskIntent {
  objective: string;
  maxRevisions: number;
  knowledgeBaseId?: string;
  idempotencyKey: string;
}

interface ApprovalDecisionIntent {
  approvalId: string;
  decision: "approved" | "rejected";
  decisionVersion: number;
}

interface ApprovalNotice {
  approvalId: string;
  message: string;
}

export function WorkPage() {
  const { taskId: selectedTaskId } = useParams<{ taskId: string }>();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { identity } = useIdentity();
  const identityKey = workIdentityQueryKey(identity);

  const tasksQuery = useInfiniteQuery({
    queryKey: ["work", "tasks", ...identityKey],
    initialPageParam: "",
    queryFn: ({ pageParam }) =>
      listTasks(identity, {
        limit: 25,
        ...(pageParam === "" ? {} : { cursor: pageParam }),
      }),
    getNextPageParam: (lastPage) => lastPage.cursor ?? undefined,
    refetchInterval: 5_000,
  });
  const tasks = useMemo(() => {
    const byId = new Map<string, TaskView>();
    for (const page of tasksQuery.data?.pages ?? []) {
      for (const task of page.tasks) byId.set(task.task_id, task);
    }
    return [...byId.values()];
  }, [tasksQuery.data]);

  const taskQueryKey = ["work", "task", ...identityKey, selectedTaskId] as const;
  const taskQuery = useQuery({
    queryKey: taskQueryKey,
    enabled: selectedTaskId !== undefined,
    queryFn: () => {
      if (selectedTaskId === undefined) throw new Error("缺少任务 ID");
      return getTask(identity, selectedTaskId);
    },
    refetchInterval: (query) =>
      isTerminalStatus(query.state.data?.status) ? false : 3_000,
  });

  const timeline = useTaskTimeline(
    identity,
    selectedTaskId,
    2_500,
    !isTerminalStatus(taskQuery.data?.status),
  );
  const refreshTimeline = timeline.refresh;
  const selectedTaskStatus = taskQuery.data?.status;
  useEffect(() => {
    if (isTerminalStatus(selectedTaskStatus)) void refreshTimeline();
  }, [refreshTimeline, selectedTaskStatus]);
  const taskInputRef = useMemo(
    () => findTaskInputRef(timeline.events),
    [timeline.events],
  );
  const taskInputQuery = useQuery({
    queryKey: ["work", "task-input", ...identityKey, taskInputRef],
    enabled:
      selectedTaskId !== undefined &&
      taskQuery.data !== undefined &&
      taskInputRef !== null,
    queryFn: async () => {
      if (taskInputRef === null) throw new Error("时间线中没有任务输入引用");
      const value = await getArtifactJson<unknown>(identity, taskInputRef);
      const parsed = parseTaskInputArtifact(value);
      if (parsed === null) throw new Error("任务输入 artifact 不符合 TaskInput 契约");
      return parsed;
    },
  });

  const approvalId = useMemo(
    () => findLatestApprovalId(timeline.events),
    [timeline.events],
  );
  const approvalQueryKey = [
    "work",
    "approval",
    ...identityKey,
    approvalId,
  ] as const;
  const approvalQuery = useQuery({
    queryKey: approvalQueryKey,
    enabled: approvalId !== null,
    queryFn: () => {
      if (approvalId === null) throw new Error("时间线中没有审批 ID");
      return getApproval(identity, approvalId);
    },
    refetchInterval: (query) =>
      query.state.data?.status === "pending" ? 3_000 : false,
  });

  const timelineGroups = useMemo(
    () => groupTimelineEvents(timeline.events),
    [timeline.events],
  );
  const finalReport = useMemo(
    () => findFinalReport(timeline.events),
    [timeline.events],
  );

  const [objective, setObjective] = useState("");
  const [maxRevisions, setMaxRevisions] = useState("2");
  const [knowledgeBaseId, setKnowledgeBaseId] = useState("");
  const [submissionKey, setSubmissionKey] = useState(() => newIdempotencyKey("task"));
  const createMutation = useMutation({
    mutationFn: ({ idempotencyKey, ...input }: CreateTaskIntent) =>
      createTask(identity, input, idempotencyKey),
    onSuccess: (task) => {
      setObjective("");
      setKnowledgeBaseId("");
      setMaxRevisions("2");
      setSubmissionKey(newIdempotencyKey("task"));
      void queryClient.invalidateQueries({
        queryKey: ["work", "tasks", ...identityKey],
      });
      void navigate(`/work/${encodeURIComponent(task.task_id)}`);
    },
  });
  const markTaskIntentEdited = () => {
    setSubmissionKey(newIdempotencyKey("task"));
    createMutation.reset();
  };

  const [cancelDraft, setCancelDraft] = useState({ taskId: "", reason: "" });
  const cancelReason =
    cancelDraft.taskId === selectedTaskId ? cancelDraft.reason : "";
  const cancelMutation = useMutation({
    mutationFn: ({ taskId, reason }: { taskId: string; reason: string }) =>
      cancelTask(identity, taskId, reason),
    onSuccess: (task) => {
      setCancelDraft({ taskId: task.task_id, reason: "" });
      queryClient.setQueryData(taskQueryKey, task);
      void queryClient.invalidateQueries({
        queryKey: ["work", "tasks", ...identityKey],
      });
      void timeline.refresh();
    },
  });

  const [approvalNotice, setApprovalNotice] = useState<ApprovalNotice | null>(null);
  const approvalMutation = useMutation({
    mutationFn: async ({
      approvalId: intendedApprovalId,
      decision,
      decisionVersion,
    }: ApprovalDecisionIntent) => {
      const approval = approvalQuery.data;
      if (approval === undefined) throw new Error("审批记录尚未加载");
      if (
        approval.approval_id !== intendedApprovalId ||
        approval.task_id !== selectedTaskId ||
        approval.decision_version !== decisionVersion
      ) {
        throw new Error("审批记录与当前任务不匹配");
      }
      return decideApproval(identity, approval, decision);
    },
    onSuccess: (approval, intent) => {
      queryClient.setQueryData(approvalQueryKey, approval);
      setApprovalNotice({
        approvalId: approval.approval_id,
        message:
          approval.status === intent.decision
            ? "服务端已记录此决定。"
            : `本次“${formatStatus(intent.decision)}”未被应用；同一决定版本的服务端权威状态为“${formatStatus(approval.status)}”。`,
      });
      void taskQuery.refetch();
      void timeline.refresh();
      void queryClient.invalidateQueries({
        queryKey: ["work", "tasks", ...identityKey],
      });
    },
    onError: async (error, intent) => {
      if (error instanceof ApiError && error.status === 409) {
        if (approvalId !== intent.approvalId) return;
        const [approvalResult, taskResult] = await Promise.all([
          approvalQuery.refetch(),
          taskQuery.refetch(),
          timeline.refresh(),
        ]);
        setApprovalNotice({
          approvalId: intent.approvalId,
          message: approvalConflictMessage(
            approvalResult.data,
            taskResult.data,
          ),
        });
      }
    },
  });

  const downloadMutation = useMutation({
    mutationFn: (artifactId: string) => downloadArtifact(identity, artifactId),
  });

  const handleCreate = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const trimmedObjective = objective.trim();
    const trimmedKnowledgeBaseId = knowledgeBaseId.trim();
    const parsedMaxRevisions = Number(maxRevisions);
    if (
      trimmedObjective === "" ||
      maxRevisions.trim() === "" ||
      !Number.isInteger(parsedMaxRevisions) ||
      parsedMaxRevisions < 0 ||
      parsedMaxRevisions > 20
    ) {
      return;
    }
    createMutation.mutate({
      objective: trimmedObjective,
      maxRevisions: parsedMaxRevisions,
      idempotencyKey: submissionKey,
      ...(trimmedKnowledgeBaseId === ""
        ? {}
        : { knowledgeBaseId: trimmedKnowledgeBaseId }),
    });
  };

  const selectedTask = taskQuery.data;
  const canCancel =
    selectedTask !== undefined && CANCELLABLE_STATUSES.has(selectedTask.status);

  return (
    <div className={`aw-work-page ${selectedTaskId === undefined ? "" : "has-selection"}`}>
      <aside className="aw-work-sidebar" aria-label="任务列表与新建任务">
        <header className="aw-pane-header">
          <div>
            <span className="aw-eyebrow">WORK</span>
            <h1>任务</h1>
          </div>
          <button
            aria-label="刷新任务列表"
            className="aw-icon-button"
            disabled={tasksQuery.isFetching}
            onClick={() => void tasksQuery.refetch()}
            type="button"
          >
            <RefreshCw aria-hidden="true" size={16} />
          </button>
        </header>

        <form className="aw-create-task" onSubmit={handleCreate}>
          <h2>
            <Plus aria-hidden="true" size={16} /> 新建任务
          </h2>
          <label htmlFor="work-objective">目标</label>
          <textarea
            id="work-objective"
            disabled={createMutation.isPending}
            maxLength={4096}
            onChange={(event) => {
              setObjective(event.target.value);
              markTaskIntentEdited();
            }}
            placeholder="描述任务要完成的目标"
            required
            rows={4}
            value={objective}
          />
          <div className="aw-form-row">
            <div>
              <label htmlFor="work-max-revisions">最大修订次数</label>
              <input
                id="work-max-revisions"
                disabled={createMutation.isPending}
                max={20}
                min={0}
                onChange={(event) => {
                  setMaxRevisions(event.target.value);
                  markTaskIntentEdited();
                }}
                required
                type="number"
                value={maxRevisions}
              />
            </div>
            <div>
              <label htmlFor="work-knowledge-base">知识库 ID（可选）</label>
              <input
                id="work-knowledge-base"
                disabled={createMutation.isPending}
                onChange={(event) => {
                  setKnowledgeBaseId(event.target.value);
                  markTaskIntentEdited();
                }}
                type="text"
                value={knowledgeBaseId}
              />
            </div>
          </div>
          <button
            className="aw-button is-primary"
            disabled={createMutation.isPending || objective.trim() === ""}
            type="submit"
          >
            {createMutation.isPending ? "正在创建…" : "创建任务"}
          </button>
          {createMutation.isError ? (
            <ErrorNotice message={errorMessage(createMutation.error, "创建任务失败")} />
          ) : null}
        </form>

        <nav className="aw-task-list" aria-label="任务">
          {tasksQuery.isPending ? <LoadingLine label="正在加载任务" /> : null}
          {tasksQuery.isError ? (
            <ErrorNotice message={errorMessage(tasksQuery.error, "加载任务列表失败")} />
          ) : null}
          {tasks.map((task) => (
            <button
              aria-current={task.task_id === selectedTaskId ? "page" : undefined}
              className={`aw-task-list-item ${
                task.task_id === selectedTaskId ? "is-active" : ""
              }`}
              key={task.task_id}
              onClick={() => void navigate(`/work/${encodeURIComponent(task.task_id)}`)}
              type="button"
            >
              <span>
                <strong title={task.task_id}>{shortId(task.task_id, 18)}</strong>
                <small>{formatDateTime(task.created_at)}</small>
              </span>
              <StatusPill status={task.status} />
            </button>
          ))}
          {!tasksQuery.isPending && !tasksQuery.isError && tasks.length === 0 ? (
            <p className="aw-muted">还没有任务。</p>
          ) : null}
          {tasksQuery.hasNextPage ? (
            <button
              className="aw-button"
              disabled={tasksQuery.isFetchingNextPage}
              onClick={() => void tasksQuery.fetchNextPage()}
              type="button"
            >
              {tasksQuery.isFetchingNextPage ? "正在加载…" : "加载更多"}
            </button>
          ) : null}
        </nav>
      </aside>

      <main className="aw-work-detail">
        {selectedTaskId === undefined ? (
          <EmptyState
            description="从左侧选择一项任务，或提交一个新任务。"
            icon={<ListTodo aria-hidden="true" size={24} />}
            title="选择任务查看执行记录"
          />
        ) : null}
        {selectedTaskId !== undefined && taskQuery.isPending ? (
          <LoadingLine label="正在加载任务详情" />
        ) : null}
        {selectedTaskId !== undefined && taskQuery.isError ? (
          <ErrorNotice message={errorMessage(taskQuery.error, "加载任务详情失败")} />
        ) : null}
        {selectedTask !== undefined ? (
          <>
            <header className="aw-work-detail-header">
              <div>
                <button
                  className="aw-button is-ghost aw-work-mobile-back"
                  onClick={() => void navigate("/work")}
                  type="button"
                >
                  <ChevronLeft aria-hidden="true" size={15} /> 任务列表
                </button>
                <span className="aw-eyebrow">TASK</span>
                <h1 title={selectedTask.task_id}>{shortId(selectedTask.task_id, 28)}</h1>
              </div>
              <StatusPill status={selectedTask.status} />
            </header>

            <section className="aw-work-section" aria-labelledby="task-overview-title">
              <h2 id="task-overview-title">任务输入</h2>
              {taskInputQuery.isPending && taskInputRef !== null ? (
                <LoadingLine label="正在读取任务输入 artifact" />
              ) : null}
              {taskInputQuery.isError ? (
                <ErrorNotice
                  message={errorMessage(taskInputQuery.error, "读取任务输入失败")}
                />
              ) : null}
              {taskInputQuery.data !== undefined ? (
                <div className="aw-task-objective">
                  <p>{taskInputQuery.data.objective}</p>
                  <div className="aw-task-input-facts">
                    <KeyValue
                      label="最大修订次数"
                      value={taskInputQuery.data.max_revisions}
                    />
                    <KeyValue
                      label="知识库 ID"
                      value={taskInputQuery.data.knowledge_base_id ?? "未指定"}
                    />
                    <KeyValue label="输入 artifact" value={shortId(taskInputRef ?? "")} />
                  </div>
                </div>
              ) : null}
              <div className="aw-task-metadata">
                <KeyValue label="创建时间" value={formatDateTime(selectedTask.created_at)} />
                <KeyValue label="更新时间" value={formatDateTime(selectedTask.updated_at)} />
                {selectedTask.status_detail !== null ? (
                  <KeyValue label="状态详情" value={selectedTask.status_detail} />
                ) : null}
              </div>
            </section>

            {canCancel ? (
              <section className="aw-work-section" aria-labelledby="cancel-task-title">
                <h2 id="cancel-task-title">
                  <Ban aria-hidden="true" size={17} /> 取消任务
                </h2>
                <label htmlFor="work-cancel-reason">取消原因</label>
                <div className="aw-inline-form">
                  <input
                    id="work-cancel-reason"
                    maxLength={1024}
                    onChange={(event) =>
                      setCancelDraft({
                        taskId: selectedTask.task_id,
                        reason: event.target.value,
                      })
                    }
                    placeholder="说明为什么取消此任务"
                    type="text"
                    value={cancelReason}
                  />
                  <button
                    className="aw-button is-danger"
                    disabled={cancelMutation.isPending || cancelReason.trim() === ""}
                    onClick={() =>
                      cancelMutation.mutate({
                        taskId: selectedTask.task_id,
                        reason: cancelReason.trim(),
                      })
                    }
                    type="button"
                  >
                    {cancelMutation.isPending ? "正在取消…" : "取消任务"}
                  </button>
                </div>
                {cancelMutation.isError ? (
                  <ErrorNotice message={errorMessage(cancelMutation.error, "取消任务失败")} />
                ) : null}
              </section>
            ) : null}

            {approvalId !== null ? (
              <ApprovalSection
                approval={approvalQuery.data}
                error={approvalQuery.error}
                loading={approvalQuery.isPending}
                notice={
                  approvalNotice?.approvalId === approvalId
                    ? approvalNotice.message
                    : null
                }
                onDecide={(decision) => {
                  const approval = approvalQuery.data;
                  if (approval === undefined) return;
                  setApprovalNotice(null);
                  approvalMutation.mutate({
                    approvalId,
                    decision,
                    decisionVersion: approval.decision_version,
                  });
                }}
                pending={
                  approvalMutation.isPending &&
                  approvalMutation.variables?.approvalId === approvalId
                }
                taskId={selectedTask.task_id}
                taskStatus={selectedTask.status}
              />
            ) : null}
            {approvalMutation.isError &&
            approvalMutation.variables?.approvalId === approvalId &&
            !(approvalMutation.error instanceof ApiError &&
              approvalMutation.error.status === 409) ? (
              <ErrorNotice
                message={errorMessage(approvalMutation.error, "提交审批决定失败")}
              />
            ) : null}

            {finalReport !== null ? (
              <section className="aw-work-section aw-final-report" aria-labelledby="report-title">
                <h2 id="report-title">
                  <FileDown aria-hidden="true" size={17} /> 最终报告
                </h2>
                <p>
                  此 artifact 来自精确关联的 <code>export_artifact</code> 工具调用，且其后已记录
                  <code>TaskSucceeded</code>。
                </p>
                <div className="aw-task-metadata">
                  <KeyValue
                    label="文件"
                    value={finalReport.artifact.filename ?? finalReport.artifact.artifact_id}
                  />
                  <KeyValue label="类型" value={finalReport.artifact.media_type} />
                  <KeyValue label="大小" value={`${finalReport.artifact.size_bytes} bytes`} />
                </div>
                <button
                  className="aw-button is-primary"
                  disabled={
                    downloadMutation.isPending &&
                    downloadMutation.variables === finalReport.artifact.artifact_id
                  }
                  onClick={() => downloadMutation.mutate(finalReport.artifact.artifact_id)}
                  type="button"
                >
                  {downloadMutation.isPending &&
                  downloadMutation.variables === finalReport.artifact.artifact_id
                    ? "正在下载…"
                    : "下载报告"}
                </button>
                {downloadMutation.isError &&
                downloadMutation.variables === finalReport.artifact.artifact_id ? (
                  <ErrorNotice
                    message={errorMessage(downloadMutation.error, "下载报告失败")}
                  />
                ) : null}
              </section>
            ) : null}

            <section className="aw-work-section aw-timeline" aria-labelledby="timeline-title">
              <div className="aw-section-heading">
                <h2 id="timeline-title">时间线</h2>
                <button
                  className="aw-button is-small"
                  disabled={timeline.loading}
                  onClick={() => void timeline.refresh()}
                  type="button"
                >
                  刷新
                </button>
              </div>
              {timeline.loading && timeline.events.length === 0 ? (
                <LoadingLine label="正在加载时间线" />
              ) : null}
              {timeline.error !== null ? (
                <ErrorNotice message={errorMessage(timeline.error, "读取时间线失败")} />
              ) : null}
              {timelineGroups.length === 0 && !timeline.loading ? (
                <InfoNotice>时间线暂时没有事件。</InfoNotice>
              ) : null}
              {timelineGroups.map((group) => (
                <section className="aw-timeline-group" key={group.id}>
                  <h3 title={group.graphNodeId ?? undefined}>
                    {group.graphNodeId === null
                      ? "任务生命周期"
                      : `节点 ${shortId(group.graphNodeId, 24)}`}
                  </h3>
                  <ol>
                    {group.events.map((event) => (
                      <li
                        className={isKnownEventType(event.event_type) ? "" : "is-unknown"}
                        key={event.event_id}
                      >
                        <div className="aw-timeline-event-heading">
                          <strong>{eventTitle(event)}</strong>
                          <time dateTime={event.timestamp}>{formatTime(event.timestamp)}</time>
                        </div>
                        <div className="aw-timeline-event-meta">
                          <code>{event.event_type}</code>
                          <span title={event.run_id}>run {shortId(event.run_id)}</span>
                          {event.sequence === null ? null : <span>#{event.sequence}</span>}
                        </div>
                        <details>
                          <summary>查看原始事件</summary>
                          <pre>{JSON.stringify(event.payload, null, 2)}</pre>
                        </details>
                      </li>
                    ))}
                  </ol>
                </section>
              ))}
            </section>
          </>
        ) : null}
      </main>
    </div>
  );
}

function ApprovalSection({
  approval,
  error,
  loading,
  notice,
  onDecide,
  pending,
  taskId,
  taskStatus,
}: {
  approval: ApprovalView | undefined;
  error: unknown;
  loading: boolean;
  notice: string | null;
  onDecide: (decision: "approved" | "rejected") => void;
  pending: boolean;
  taskId: string;
  taskStatus: TaskStatus;
}) {
  const matchesTask = approval?.task_id === taskId;
  const decidable = approval?.status === "pending" && taskStatus === "waiting_approval";
  return (
    <section className="aw-work-section aw-approval" aria-labelledby="approval-title">
      <h2 id="approval-title">
        <ClipboardCheck aria-hidden="true" size={17} /> 服务端审批记录
      </h2>
      <p className="aw-muted">下列状态与版本来自审批 GET 响应，是决定操作的权威记录。</p>
      {loading ? <LoadingLine label="正在读取审批记录" /> : null}
      {error !== null ? (
        <ErrorNotice message={errorMessage(error, "读取审批记录失败")} />
      ) : null}
      {approval !== undefined && !matchesTask ? (
        <ErrorNotice message="审批记录与当前任务不匹配，无法提交决定。" />
      ) : null}
      {approval !== undefined && matchesTask ? (
        <>
          <div className="aw-task-metadata">
            <KeyValue label="状态" value={<StatusPill status={approval.status} />} />
            <KeyValue label="决定版本" value={approval.decision_version} />
            <KeyValue label="审批 ID" value={shortId(approval.approval_id, 22)} />
            <KeyValue label="创建时间" value={formatDateTime(approval.created_at)} />
            {approval.decided_at === null ? null : (
              <KeyValue label="决定时间" value={formatDateTime(approval.decided_at)} />
            )}
          </div>
          {approval.status === "pending" && !decidable ? (
            <InfoNotice>
              当前任务状态为“{formatStatus(taskStatus)}”，此审批已不再可决定。
            </InfoNotice>
          ) : null}
          {decidable ? (
            <div className="aw-button-row">
              <button
                className="aw-button is-primary"
                disabled={pending}
                onClick={() => onDecide("approved")}
                type="button"
              >
                批准
              </button>
              <button
                className="aw-button is-danger"
                disabled={pending}
                onClick={() => onDecide("rejected")}
                type="button"
              >
                拒绝
              </button>
            </div>
          ) : null}
        </>
      ) : null}
      {notice === null ? null : <InfoNotice>{notice}</InfoNotice>}
    </section>
  );
}

function errorMessage(error: unknown, fallback: string): string {
  if (error instanceof Error && error.message.trim() !== "") return error.message;
  return fallback;
}

function isTerminalStatus(status: TaskStatus | undefined): boolean {
  return status !== undefined && TERMINAL_STATUSES.has(status);
}

function approvalConflictMessage(
  approval: ApprovalView | undefined,
  task: TaskView | undefined,
): string {
  if (task !== undefined && task.status !== "waiting_approval") {
    return `任务服务端状态已是“${formatStatus(task.status)}”，审批不再可决定；已刷新权威记录。`;
  }
  if (approval !== undefined && approval.status !== "pending") {
    return `审批服务端状态已是“${formatStatus(approval.status)}”；已刷新权威记录。`;
  }
  return "服务端拒绝当前版本的决定；已刷新权威记录，请重新确认。";
}
