import {
  useInfiniteQuery,
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import {
  AlertTriangle,
  ChevronLeft,
  ChevronRight,
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
  getArtifactText,
  getTask,
  listTasks,
  newIdempotencyKey,
} from "../../api/client";
import type {
  ApprovalView,
  ArtifactRef,
  PrincipalIdentity,
  TaskStatus,
  TaskView,
} from "../../api/types";
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
import { MarkdownContent } from "../../components/MarkdownContent";
import { StepDisclosure } from "../../components/StepDisclosure";
import { explainFailure } from "./failure";
import { deriveLifecycle, type Lifecycle } from "./lifecycle";
import { useTaskTimeline } from "./useTaskTimeline";
import { workIdentityQueryKey } from "./workQueryKeys";
import {
  eventTitle,
  findDraftText,
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
  wantsReport: boolean;
  idempotencyKey: string;
}

/**
 * Whether an objective asks for a file.
 *
 * Only ever a *default* for the toggle beside the box -- the reader sees the
 * result and can flip it before submitting. Guessing silently would be worse
 * than not guessing: the difference decides whether the Task stops to ask a
 * human for permission to write something.
 */
const REPORT_WORDS =
  /报告|报表|文档|文件|导出|输出一份|写一份|report|document|export/i;

function mentionsReport(objective: string): boolean {
  return REPORT_WORDS.test(objective);
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
  const lifecycle = useMemo(
    () => deriveLifecycle(timeline.events, taskQuery.data?.status),
    [timeline.events, taskQuery.data?.status],
  );
  const draftText = useMemo(() => findDraftText(timeline.events), [timeline.events]);
  // A transient provider blip used to mean retyping the objective. Re-submitting
  // opens a *new* Task rather than reviving this one: the failed run already
  // spent its budget and wrote its events, and rewriting that history would
  // make a Task's record depend on how many times somebody retried it.
  const retryInput = taskInputQuery.data ?? null;
  const resubmit = (input: NonNullable<typeof taskInputQuery.data>) => {
    createMutation.mutate({
      objective: input.objective,
      maxRevisions: input.max_revisions,
      wantsReport: input.wants_report,
      idempotencyKey: newIdempotencyKey("task"),
      ...(input.knowledge_base_id === null
        ? {}
        : { knowledgeBaseId: input.knowledge_base_id }),
    });
  };

  const [objective, setObjective] = useState("");
  const [maxRevisions, setMaxRevisions] = useState("2");
  // null means "follow the objective". Set once the reader touches the toggle,
  // so their choice is not overwritten by the next keystroke.
  const [reportOverride, setReportOverride] = useState<boolean | null>(null);
  const wantsReport = reportOverride ?? mentionsReport(objective);
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
      setReportOverride(null);
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
      wantsReport,
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
          {/* Checked from the objective, and shown rather than inferred behind
              the reader's back: this is what decides whether the Task stops to
              ask permission to write a file. */}
          <label className="aw-report-toggle">
            <input
              checked={wantsReport}
              disabled={createMutation.isPending}
              onChange={(event) => setReportOverride(event.target.checked)}
              type="checkbox"
            />
            <span>
              <strong>生成报告文件</strong>
              <small>
                {wantsReport
                  ? "完成后会请你确认，再导出可下载的报告。"
                  : "只把结果写在任务页里，不生成文件，也不需要你确认。"}
              </small>
            </span>
          </label>
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
                {/* The objective when the server recorded one, because a list
                    of ids tells the reader nothing about which Task is which.
                    Older Tasks have no label and still have to be openable, so
                    they fall back to the id rather than to a blank row. */}
                <strong title={task.objective_preview ?? task.task_id}>
                  {task.objective_preview ?? shortId(task.task_id, 18)}
                </strong>
                <small>
                  {formatDateTime(task.created_at)}
                  {task.objective_preview === null ? "" : ` · ${shortId(task.task_id, 14)}`}
                </small>
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
                <h1 title={selectedTask.task_id}>
                  {selectedTask.objective_preview ?? shortId(selectedTask.task_id, 28)}
                </h1>
                {selectedTask.objective_preview === null ? null : (
                  <code className="aw-task-id">{shortId(selectedTask.task_id, 28)}</code>
                )}
              </div>
              <StatusPill status={selectedTask.status} />
            </header>

            {/* Result first. Whatever the Task produced is the reason the
                reader opened this page; the machinery that produced it is
                below, folded. */}
            <TaskResult
              artifact={finalReport?.artifact ?? null}
              draftText={draftText}
              identity={identity}
              onDownload={(id) => downloadMutation.mutate(id)}
              onRetry={retryInput === null ? undefined : () => resubmit(retryInput)}
              status={selectedTask.status}
              {...(selectedTask.status_detail === null
                ? {}
                : { statusDetail: selectedTask.status_detail })}
            />

            {/* The decision, in place. A Task waiting on a human is the one
                thing on this page that cannot wait, so it sits directly under
                the result and needs no other page to act on. */}
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

            <LifecyclePanel
              lifecycle={lifecycle}
              loading={timeline.loading && timeline.events.length === 0}
              running={!isTerminalStatus(selectedTask.status)}
            />

            <details className="aw-work-fold" open={!isTerminalStatus(selectedTask.status)}>
              <summary>
                <ChevronRight aria-hidden="true" className="aw-step-caret" size={14} />
                执行记录
                <span>{timeline.events.length} 条事件</span>
              </summary>
              <div className="aw-work-fold-body">
                <div className="aw-section-heading">
                  <span className="aw-page-note">按阶段分组，展开任意一步查看真实内容</span>
                  <button
                    className="aw-button is-small"
                    disabled={timeline.loading}
                    onClick={() => void timeline.refresh()}
                    type="button"
                  >
                    刷新
                  </button>
                </div>
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
                          <StepDisclosure
                            event={event}
                            onOpenArtifact={(id) => downloadMutation.mutate(id)}
                            title={eventTitle(event)}
                          />
                        </li>
                      ))}
                    </ol>
                  </section>
                ))}
              </div>
            </details>

            <details className="aw-work-fold">
              <summary>
                <ChevronRight aria-hidden="true" className="aw-step-caret" size={14} />
                任务详情
              </summary>
              <div className="aw-work-fold-body">
                {taskInputQuery.isPending && taskInputRef !== null ? (
                  <LoadingLine label="正在读取任务输入" />
                ) : null}
                {taskInputQuery.isError ? (
                  <ErrorNotice
                    message={errorMessage(taskInputQuery.error, "读取任务输入失败")}
                  />
                ) : null}
                {taskInputQuery.data === undefined ? null : (
                  <p className="aw-task-objective-full">{taskInputQuery.data.objective}</p>
                )}
                <div className="aw-task-metadata">
                  {taskInputQuery.data === undefined ? null : (
                    <>
                      <KeyValue
                        label="最大修订次数"
                        value={taskInputQuery.data.max_revisions}
                      />
                      <KeyValue
                        label="知识库"
                        value={taskInputQuery.data.knowledge_base_id ?? "未使用"}
                      />
                    </>
                  )}
                  <KeyValue label="任务 ID" value={selectedTask.task_id} />
                  <KeyValue label="创建时间" value={formatDateTime(selectedTask.created_at)} />
                  <KeyValue label="更新时间" value={formatDateTime(selectedTask.updated_at)} />
                </div>
                {canCancel ? (
                  <div className="aw-work-cancel">
                    <label htmlFor="work-cancel-reason">取消这个任务</label>
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
                        placeholder="说明为什么取消"
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
                      <ErrorNotice
                        message={errorMessage(cancelMutation.error, "取消任务失败")}
                      />
                    ) : null}
                  </div>
                ) : null}
              </div>
            </details>
          </>
        ) : null}
      </main>
    </div>
  );
}

/**
 * Where the Task is, as a strip rather than a section.
 *
 * While it runs this is the thing that moves, so it stays open. Once it stops
 * moving it collapses to one line -- a finished Task's stage list is history,
 * and history does not need to occupy the page above the result.
 */
function LifecyclePanel({
  lifecycle,
  loading,
  running,
}: {
  lifecycle: Lifecycle;
  loading: boolean;
  running: boolean;
}) {
  if (loading) return <LoadingLine label="正在读取执行阶段" />;
  const total = lifecycle.stages.filter((stage) => stage.state !== "skipped").length;

  return (
    <details className="aw-lifecycle" open={running}>
      <summary>
        <ChevronRight aria-hidden="true" className="aw-step-caret" size={14} />
        <strong>{lifecycle.currentTitle ?? "执行阶段"}</strong>
        <span className="aw-lifecycle-count">
          {lifecycle.doneCount}/{total}
        </span>
        <span className="aw-lifecycle-mini" aria-hidden="true">
          {lifecycle.stages.map((stage) => (
            <i className={`is-${stage.state}`} key={stage.id} />
          ))}
        </span>
      </summary>
      <ol className="aw-lifecycle-steps">
        {lifecycle.stages.map((stage) => (
          <li className={`is-${stage.state}`} key={stage.id}>
            <span className="aw-lifecycle-dot" aria-hidden="true" />
            <span className="aw-lifecycle-title">{stage.title}</span>
            <span className="aw-lifecycle-note">
              {stage.state === "skipped"
                ? "未执行"
                : stage.state === "pending"
                  ? "等待中"
                  : stage.state === "waiting"
                    ? "等待你确认"
                    : stage.endedAt === null
                      ? ""
                      : formatTime(stage.endedAt)}
            </span>
          </li>
        ))}
      </ol>
    </details>
  );
}

/**
 * What the Task produced, shown rather than described.
 *
 * The old version listed the artifact's media type and byte count behind a
 * download button, which told the reader everything about the file except what
 * it said. Text is rendered inline; anything else keeps the download, because
 * that is what a binary is for.
 */
function TaskResult({
  artifact,
  draftText,
  identity,
  onDownload,
  onRetry,
  status,
  statusDetail,
}: {
  artifact: ArtifactRef | null;
  draftText: string | null;
  identity: PrincipalIdentity;
  onDownload: (artifactId: string) => void;
  onRetry?: (() => void) | undefined;
  status: TaskStatus;
  statusDetail?: string;
}) {
  const readable = artifact !== null && isReadableMedia(artifact.media_type);
  const preview = useQuery({
    queryKey: ["work", "artifact-text", artifact?.artifact_id ?? ""],
    enabled: readable,
    staleTime: Number.POSITIVE_INFINITY,
    queryFn: () => {
      if (artifact === null) throw new Error("没有可预览的产物");
      return getArtifactText(identity, artifact.artifact_id);
    },
  });

  if (artifact === null) {
    if (!isTerminalStatus(status)) {
      return (
        <section className="aw-result is-running">
          <LoadingLine label="任务正在执行，完成后结果会显示在这里" />
        </section>
      );
    }
    // No file, but the Task still wrote something. Showing the draft is the
    // whole point of letting a Task finish without an export.
    if (status === "succeeded" && draftText !== null) {
      return (
        <section className="aw-result" aria-label="任务结果">
          <header>
            <div>
              <span className="aw-eyebrow">结果</span>
              <strong>这次没有生成文件</strong>
            </div>
          </header>
          <div className="aw-result-body">
            <MarkdownContent text={draftText} />
          </div>
        </section>
      );
    }
    if (status === "succeeded") {
      return (
        <section className="aw-result">
          <div className="aw-notice">
            <span>任务已完成，没有产出内容。展开下面的执行记录可以看到每一步做了什么。</span>
          </div>
        </section>
      );
    }
    // Failed, cancelled or dead-lettered. The server's detail is a stable
    // sentence over a closed error vocabulary; this says what it means, and
    // offers the retry only when the server called the cause retryable.
    const failure = explainFailure(statusDetail ?? null);
    return (
      <section className="aw-result">
        <div className="aw-notice is-warning">
          <AlertTriangle aria-hidden="true" size={16} />
          <span>
            <strong>任务{formatStatus(status)}</strong>
            {failure === null ? null : <small>{failure.text}</small>}
          </span>
        </div>
        {failure?.retryable === true && onRetry !== undefined ? (
          <div className="aw-result-retry">
            <button className="aw-button is-primary" onClick={onRetry} type="button">
              用同样的目标再试一次
            </button>
          </div>
        ) : null}
      </section>
    );
  }

  return (
    <section className="aw-result" aria-label="任务产出">
      <header>
        <div>
          <span className="aw-eyebrow">产出</span>
          <strong>{artifact.filename ?? artifact.kind}</strong>
        </div>
        <button
          className="aw-button is-ghost is-small"
          onClick={() => onDownload(artifact.artifact_id)}
          type="button"
        >
          <FileDown aria-hidden="true" size={14} />
          下载
        </button>
      </header>
      {!readable ? (
        <p className="aw-page-note">
          {artifact.media_type} · {artifact.size_bytes} 字节，这个类型只能下载查看。
        </p>
      ) : preview.isPending ? (
        <LoadingLine label="正在读取产出内容" />
      ) : preview.isError ? (
        <ErrorNotice message={errorMessage(preview.error, "读取产出内容失败")} />
      ) : (
        <>
          <div className="aw-result-body">
            <MarkdownContent text={preview.data.text} />
          </div>
          {preview.data.truncated ? (
            <p className="aw-page-note">内容较长，这里只显示开头；完整内容请下载。</p>
          ) : null}
        </>
      )}
    </section>
  );
}

/** Text this page can render. Anything else is a download, not a preview. */
function isReadableMedia(mediaType: string): boolean {
  return (
    mediaType.startsWith("text/") ||
    mediaType === "application/json" ||
    mediaType.endsWith("+json")
  );
}

/**
 * The decision, where the Task is.
 *
 * No second confirmation dialog: the two buttons say what they do, they sit
 * under the objective and the draft the reader just read, and a decision made
 * here is recoverable by the Task's own record. A modal that repeats the
 * button's label is a click, not a safeguard.
 */
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

  // Decided, and the Task moved on. The lifecycle strip already says so, and a
  // settled approval is not something the reader has to act on.
  if (!loading && !decidable && notice === null && error === null) return null;

  return (
    <section className="aw-approve" aria-labelledby="approval-title">
      <div className="aw-approve-copy">
        <ClipboardCheck aria-hidden="true" size={17} />
        <div>
          <strong id="approval-title">要生成并导出这份报告吗？</strong>
          <span>批准后继续导出；拒绝则到此为止，不生成文件。</span>
        </div>
      </div>
      {loading ? <LoadingLine label="正在读取审批记录" /> : null}
      {error !== null ? (
        <ErrorNotice message={errorMessage(error, "读取审批记录失败")} />
      ) : null}
      {approval !== undefined && !matchesTask ? (
        <ErrorNotice message="审批记录与当前任务不匹配，无法提交决定。" />
      ) : null}
      {decidable ? (
        <div className="aw-approve-actions">
          <button
            className="aw-button is-ghost"
            disabled={pending}
            onClick={() => onDecide("rejected")}
            type="button"
          >
            不用了
          </button>
          <button
            className="aw-button is-primary"
            disabled={pending}
            onClick={() => onDecide("approved")}
            type="button"
          >
            {pending ? "正在提交…" : "生成报告"}
          </button>
        </div>
      ) : null}
      {notice === null ? null : (
        <div className="aw-approve-settled">
          {/* The server's answer, not the button that was pressed. A decision
              that did not stick has to be visible as state, not only as a
              sentence the reader might skim. */}
          {approval !== undefined && matchesTask ? (
            <StatusPill status={approval.status} />
          ) : null}
          <InfoNotice>{notice}</InfoNotice>
        </div>
      )}
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
