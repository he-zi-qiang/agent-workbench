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
import { useNavigate, useParams, useSearchParams } from "react-router-dom";
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
  AttachmentButton,
  AttachmentTray,
  useKnowledgeAttachments,
} from "../../components/AttachmentTray";
import {
  KnowledgeSourcePicker,
  useKnowledgeBases,
} from "../../components/KnowledgeSourcePicker";
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
  const [searchParams] = useSearchParams();
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
  const [knowledgeBaseDraftId, setKnowledgeBaseId] = useState<string | null>(
    searchParams.get("kb"),
  );
  const knowledgeBases = useKnowledgeBases(identity);
  const knowledgeBaseId =
    knowledgeBaseDraftId !== null &&
    knowledgeBases.data?.knowledge_bases.some(
      (item) => item.knowledge_base_id === knowledgeBaseDraftId,
    )
      ? knowledgeBaseDraftId
      : null;
  const sourceResolving =
    knowledgeBaseDraftId !== null && knowledgeBases.isPending;
  const attachments = useKnowledgeAttachments(identity, knowledgeBaseId);
  const [submissionKey, setSubmissionKey] = useState(() => newIdempotencyKey("task"));
  const createMutation = useMutation({
    mutationFn: ({ idempotencyKey, ...input }: CreateTaskIntent) =>
      createTask(identity, input, idempotencyKey),
    onSuccess: (task) => {
      setObjective("");
      attachments.clear();
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
    const parsedMaxRevisions = Number(maxRevisions);
    if (
      trimmedObjective === "" ||
      maxRevisions.trim() === "" ||
      !Number.isInteger(parsedMaxRevisions) ||
      parsedMaxRevisions < 0 ||
      parsedMaxRevisions > 20 ||
      sourceResolving ||
      attachments.hasBlockingItems
    ) {
      return;
    }
    createMutation.mutate({
      objective: trimmedObjective,
      maxRevisions: parsedMaxRevisions,
      idempotencyKey: submissionKey,
      ...(knowledgeBaseId === null ? {} : { knowledgeBaseId }),
    });
  };

  const changeKnowledgeBase = (nextId: string | null) => {
    if (nextId === knowledgeBaseId) return;
    if (
      attachments.items.length > 0 &&
      !window.confirm("切换资料会清空当前任务的待上传附件，是否继续？")
    ) {
      return;
    }
    attachments.clear();
    setKnowledgeBaseId(nextId);
    markTaskIntentEdited();
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
            placeholder="例如：整理这批资料，比较三个方案并输出一份建议报告"
            required
            rows={4}
            value={objective}
          />
          <KnowledgeSourcePicker
            disabled={createMutation.isPending}
            identity={identity}
            onChange={(knowledgeBase) =>
              changeKnowledgeBase(knowledgeBase?.knowledge_base_id ?? null)
            }
            value={knowledgeBaseId}
          />
          <div className="aw-work-attachment-row">
            <AttachmentButton
              disabled={createMutation.isPending}
              onFiles={attachments.addFiles}
            />
            <span>
              添加 PDF 或 Markdown
              {knowledgeBaseId === null ? "（选择知识库后上传）" : "（上传到所选知识库）"}
            </span>
          </div>
          <AttachmentTray
            items={attachments.items}
            onRemove={attachments.remove}
            onRetry={attachments.retry}
          />
          <details className="aw-work-advanced">
            <summary>高级设置</summary>
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
          </details>
          <p className="aw-create-task-hint">
            {attachments.hasBlockingItems
              ? "附件正在上传或索引，完成后才能创建任务。"
              : knowledgeBaseId === null
                ? "不使用知识库：Agent 将只按目标执行，不做内部资料检索。"
                : "任务会在执行期间检索所选知识库。"}
          </p>
          <button
            className="aw-button is-primary"
            disabled={
              createMutation.isPending ||
              sourceResolving ||
              objective.trim() === "" ||
              attachments.hasBlockingItems
            }
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
                {...(taskInputQuery.data === undefined
                  ? {}
                  : { objective: taskInputQuery.data.objective })}
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
                      : workflowStageTitle(group.graphNodeId)}
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
                        <details>
                          <summary>工程详情</summary>
                          <div className="aw-timeline-event-meta">
                            <code>{event.event_type}</code>
                            <span title={event.run_id}>run {shortId(event.run_id)}</span>
                            {event.sequence === null ? null : <span>#{event.sequence}</span>}
                          </div>
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
  objective,
  onDecide,
  pending,
  taskId,
  taskStatus,
}: {
  approval: ApprovalView | undefined;
  error: unknown;
  loading: boolean;
  notice: string | null;
  objective?: string;
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
        <ClipboardCheck aria-hidden="true" size={17} /> 是否允许生成并导出任务报告？
      </h2>
      <p>
        Agent 已完成主要工作，正在等待你的确认。批准后继续生成可下载报告；拒绝后停止任务且不导出。
      </p>
      {objective === undefined ? null : (
        <blockquote className="aw-approval-objective">{objective}</blockquote>
      )}
      {loading ? <LoadingLine label="正在读取审批记录" /> : null}
      {error !== null ? (
        <ErrorNotice message={errorMessage(error, "读取审批记录失败")} />
      ) : null}
      {approval !== undefined && !matchesTask ? (
        <ErrorNotice message="审批记录与当前任务不匹配，无法提交决定。" />
      ) : null}
      {approval !== undefined && matchesTask ? (
        <>
          <div className="aw-approval-summary">
            <StatusPill status={approval.status} />
            <span>请求时间：{formatDateTime(approval.created_at)}</span>
          </div>
          {approval.status === "pending" && !decidable ? (
            <InfoNotice>
              当前任务状态为“{formatStatus(taskStatus)}”，此审批已不再可决定。
            </InfoNotice>
          ) : null}
          {decidable ? (
            <div className="aw-button-row">
              <button
                aria-label="批准"
                className="aw-button is-primary"
                disabled={pending}
                onClick={() => {
                  if (window.confirm("批准后将继续生成并导出报告。确认批准吗？")) {
                    onDecide("approved");
                  }
                }}
                type="button"
              >
                批准并继续导出
              </button>
              <button
                aria-label="拒绝"
                className="aw-button is-danger"
                disabled={pending}
                onClick={() => {
                  if (window.confirm("拒绝后任务会停止且不导出报告。确认拒绝吗？")) {
                    onDecide("rejected");
                  }
                }}
                type="button"
              >
                拒绝并停止任务
              </button>
            </div>
          ) : null}
          <details className="aw-engineering-details">
            <summary>工程详情</summary>
            <div className="aw-task-metadata">
              <KeyValue label="审批 ID" value={shortId(approval.approval_id, 22)} />
              <KeyValue label="决定版本" value={approval.decision_version} />
              {approval.decided_at === null ? null : (
                <KeyValue label="决定时间" value={formatDateTime(approval.decided_at)} />
              )}
            </div>
          </details>
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

function workflowStageTitle(graphNodeId: string): string {
  const normalized = graphNodeId.toLowerCase();
  if (normalized.includes("plan")) return "规划任务";
  if (normalized.includes("research")) return "收集资料";
  if (normalized.includes("draft") || normalized.includes("write")) return "撰写结果";
  if (normalized.includes("critic") || normalized.includes("review")) return "检查与修订";
  if (normalized.includes("approval")) return "等待确认";
  if (normalized.includes("export")) return "生成报告";
  return "执行步骤";
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
