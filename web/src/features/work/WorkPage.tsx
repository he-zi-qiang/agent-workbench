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
  Paperclip,
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
  getDocumentPreview,
  getTask,
  listTasks,
  newIdempotencyKey,
  triageTask,
} from "../../api/client";
import type {
  ApprovalView,
  ArtifactRef,
  EventEnvelope,
  PrincipalIdentity,
  TaskGraphChoice,
  TaskIntent,
  TaskStatus,
  TaskView,
  TriageOption,
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
import { StepStream, type StreamStage } from "../../components/StepStream";
import { explainFailure } from "./failure";
import { deriveLifecycle, type Lifecycle, stageOfNode } from "./lifecycle";
import { useTaskTimeline } from "./useTaskTimeline";
import { workIdentityQueryKey } from "./workQueryKeys";
import {
  artifactLabel,
  collectArtifacts,
  eventTitle,
  findDraftText,
  findFinalReport,
  findGraphChoice,
  findLatestApprovalId,
  findTaskInputRef,
  findTaskIntent,
  isKnownEventType,
  parseTaskInputArtifact,
  type TaskArtifact,
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
  graph?: TaskGraphChoice;
  intent?: TaskIntent;
  idempotencyKey: string;
}

/**
 * The explicit overrides, living in 高级设置 (ADR-036).
 *
 * The default is "auto": the form asks the server's triage to propose the
 * shape, and only a genuinely ambiguous objective comes back as a question.
 * The two explicit values remain for the reader who already knows -- an
 * explicit choice skips triage entirely and outranks whatever it would have
 * said. This replaces both the always-visible radio pair and the
 * `REPORT_WORDS` regex: the regex guessed silently, differently from the CLI,
 * and left no record of having guessed.
 */
const GRAPH_OVERRIDE_OPTIONS: ReadonlyArray<{
  value: TaskGraphChoice | "auto";
  label: string;
  hint: string;
}> = [
  {
    value: "auto",
    label: "自动判定",
    hint: "由模型判断走哪条流水线，判不准会先问你。",
  },
  {
    value: "research",
    label: "调研报告",
    hint: "检索资料、撰写并评审一份有依据的报告。",
  },
  {
    value: "general",
    label: "通用执行",
    hint: "一个带工具的执行者自定步骤把事做完，评审后交付。",
  },
];

/** What the ask card offers when the server's options are unusable. */
const FALLBACK_ASK_OPTIONS: readonly TriageOption[] = [
  { graph: "research", label: "调研报告" },
  { graph: "general", label: "通用执行" },
];

/** How long the form waits for a triage verdict before submitting anyway. */
const TRIAGE_TIMEOUT_MS = 10_000;

interface PendingAsk {
  question: string;
  options: readonly TriageOption[];
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

  // Events keyed by the lifecycle stage that owns them, so a stage in the
  // stream can show its own work instead of pointing at a separate log.
  const stageEvents = useMemo(() => {
    const byStage = new Map<string, EventEnvelope[]>();
    for (const event of timeline.events) {
      if (event.graph_node_id === null) continue;
      const id = stageOfNode(event.graph_node_id);
      const existing = byStage.get(id);
      if (existing === undefined) byStage.set(id, [event]);
      else existing.push(event);
    }
    return byStage;
  }, [timeline.events]);
  const taskEvents = useMemo(
    () => timeline.events.filter((event) => event.graph_node_id === null),
    [timeline.events],
  );
  const artifacts = useMemo(
    () => collectArtifacts(timeline.events),
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
  // The pipeline the original ran, read from its submission event: the choice
  // is deliberately not part of the input artifact, and a retry that dropped
  // it would silently move the Task onto another graph.
  const retryGraph = useMemo(
    () => findGraphChoice(timeline.events),
    [timeline.events],
  );
  // Who decided this Task's shape, for the detail fold (ADR-036). Provenance
  // only: nothing here feeds a submission.
  const taskIntent = useMemo(
    () => findTaskIntent(timeline.events),
    [timeline.events],
  );
  const resubmit = (input: NonNullable<typeof taskInputQuery.data>) => {
    createMutation.mutate({
      objective: input.objective,
      maxRevisions: input.max_revisions,
      wantsReport: input.wants_report,
      idempotencyKey: newIdempotencyKey("task"),
      ...(input.knowledge_base_id === null
        ? {}
        : { knowledgeBaseId: input.knowledge_base_id }),
      ...(retryGraph === null ? {} : { graph: retryGraph }),
    });
  };

  const [objective, setObjective] = useState("");
  const [maxRevisions, setMaxRevisions] = useState("2");
  // "auto" asks triage; an explicit value skips it and outranks it (ADR-036).
  const [graphOverride, setGraphOverride] = useState<TaskGraphChoice | "auto">(
    "auto",
  );
  const [reportOverride, setReportOverride] = useState<boolean | "auto">("auto");
  // The question triage sent back, awaiting the reader's chip. Cleared on any
  // edit: a question about the previous wording would be answered about the
  // wrong objective.
  const [pendingAsk, setPendingAsk] = useState<PendingAsk | null>(null);
  const [triaging, setTriaging] = useState(false);
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
      setReportOverride("auto");
      setGraphOverride("auto");
      setPendingAsk(null);
      setSubmissionKey(newIdempotencyKey("task"));
      void queryClient.invalidateQueries({
        queryKey: ["work", "tasks", ...identityKey],
      });
      void navigate(`/work/${encodeURIComponent(task.task_id)}`);
    },
  });
  const markTaskIntentEdited = () => {
    setSubmissionKey(newIdempotencyKey("task"));
    setPendingAsk(null);
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

  // Which file the reading column is showing, when the reader picked one from
  // the rail, and which Task they picked it in. `null` means the Task's own
  // final report, which is what the column shows on its own.
  //
  // Stored with its Task and narrowed on read, rather than cleared by an
  // effect. The selection is only meaningful for the Task it was made in --
  // an id carried across would either 404 or, since artifacts are per-tenant,
  // render one Task's document under another Task's heading -- and deriving
  // that during render is what keeps a stale value from ever being displayed,
  // which an effect that clears it afterwards cannot promise.
  const [opened, setOpened] = useState<{
    taskId: string;
    artifact: ArtifactRef;
  } | null>(null);
  const openedArtifact =
    opened !== null && opened.taskId === selectedTaskId ? opened.artifact : null;

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
    mutationFn: (artifact: ArtifactRef) => downloadArtifact(identity, artifact),
  });

  const validatedIntent = (): {
    objective: string;
    maxRevisions: number;
  } | null => {
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
      return null;
    }
    return { objective: trimmedObjective, maxRevisions: parsedMaxRevisions };
  };

  // The explicit report override resolves here; "auto" follows the triage
  // verdict when there is one and falls to false when there is not -- the
  // approval gate a wrong `true` would force is an approve-or-fail door
  // (ADR-036 §2.4), so absent any signal the form does not open it.
  const resolvedReport = (verdict: boolean | null): boolean =>
    reportOverride === "auto" ? (verdict ?? false) : reportOverride;

  const submitResolved = (resolved: {
    graph?: TaskGraphChoice;
    wantsReport: boolean;
    intent: TaskIntent;
  }) => {
    const valid = validatedIntent();
    if (valid === null) return;
    createMutation.mutate({
      objective: valid.objective,
      maxRevisions: valid.maxRevisions,
      wantsReport: resolved.wantsReport,
      intent: resolved.intent,
      idempotencyKey: submissionKey,
      ...(knowledgeBaseId === null ? {} : { knowledgeBaseId }),
      ...(resolved.graph === undefined ? {} : { graph: resolved.graph }),
    });
  };

  const runTriage = async (trimmedObjective: string) => {
    setTriaging(true);
    try {
      const verdict = await triageTask(
        identity,
        {
          objective: trimmedObjective,
          knowledgeBaseSelected: knowledgeBaseId !== null,
          attachmentNames: attachments.items.map((item) => item.file.name),
        },
        { signal: AbortSignal.timeout(TRIAGE_TIMEOUT_MS) },
      );
      if (verdict.status === "ask") {
        // No Task yet, and none until the reader answers -- uncertainty is a
        // question here, never a status (ADR-036 §2.1).
        setPendingAsk({
          question:
            verdict.question ??
            "这个任务是要一份有依据的调研报告，还是直接把事做完？",
          options:
            verdict.options.length > 0 ? verdict.options : FALLBACK_ASK_OPTIONS,
        });
        return;
      }
      if (verdict.status === "decided" && verdict.graph !== null) {
        submitResolved({
          graph: verdict.graph,
          wantsReport: resolvedReport(verdict.wants_report),
          intent: {
            graph_decided_by: "model",
            wants_report_decided_by:
              reportOverride === "auto" ? "model" : "user",
            reason: verdict.reason,
          },
        });
        return;
      }
      // "default": submit what this form submitted before triage existed --
      // no graph field, so the deployment decides.
      submitResolved({
        wantsReport: resolvedReport(null),
        intent: {
          graph_decided_by: "default",
          wants_report_decided_by:
            reportOverride === "auto" ? "default" : "user",
        },
      });
    } finally {
      setTriaging(false);
    }
  };

  const answerAsk = (graph: TaskGraphChoice) => {
    setPendingAsk(null);
    submitResolved({
      graph,
      wantsReport: resolvedReport(null),
      intent: {
        graph_decided_by: "user",
        wants_report_decided_by: reportOverride === "auto" ? "default" : "user",
      },
    });
  };

  const handleCreate = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (triaging || createMutation.isPending) return;
    const valid = validatedIntent();
    if (valid === null) return;
    setPendingAsk(null);
    if (graphOverride !== "auto") {
      // An explicit choice skips triage entirely: the reader already answered
      // the only question it would have asked.
      submitResolved({
        graph: graphOverride,
        wantsReport: resolvedReport(null),
        intent: {
          graph_decided_by: "user",
          wants_report_decided_by:
            reportOverride === "auto" ? "default" : "user",
        },
      });
      return;
    }
    void runTriage(valid.objective);
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
  const createBusy = createMutation.isPending || triaging;

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
            disabled={createBusy}
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
            disabled={createBusy}
            identity={identity}
            onChange={(knowledgeBase) =>
              changeKnowledgeBase(knowledgeBase?.knowledge_base_id ?? null)
            }
            value={knowledgeBaseId}
          />
          <div className="aw-work-attachment-row">
            <AttachmentButton
              disabled={createBusy}
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
            {/* The explicit overrides (ADR-036). The default is 自动: triage
                proposes, a genuinely ambiguous objective becomes a question,
                and the answer is submitted explicitly. An explicit value here
                skips triage and outranks it. */}
            <fieldset className="aw-graph-choice">
              <legend>执行方式</legend>
              {GRAPH_OVERRIDE_OPTIONS.map((option) => (
                <label className="aw-graph-option" key={option.value}>
                  <input
                    checked={graphOverride === option.value}
                    disabled={createBusy}
                    name="work-graph"
                    onChange={() => {
                      setGraphOverride(option.value);
                      markTaskIntentEdited();
                    }}
                    type="radio"
                    value={option.value}
                  />
                  <span>
                    <strong>{option.label}</strong>
                    <small>{option.hint}</small>
                  </span>
                </label>
              ))}
            </fieldset>
            <label htmlFor="work-report-mode">报告文件</label>
            <select
              id="work-report-mode"
              disabled={createBusy}
              onChange={(event) => {
                const value = event.target.value;
                setReportOverride(value === "auto" ? "auto" : value === "yes");
                markTaskIntentEdited();
              }}
              value={
                reportOverride === "auto" ? "auto" : reportOverride ? "yes" : "no"
              }
            >
              <option value="auto">自动判定（要文件会先请你确认导出）</option>
              <option value="yes">一定生成文件</option>
              <option value="no">不生成文件</option>
            </select>
            <label htmlFor="work-max-revisions">最大修订次数</label>
            <input
              id="work-max-revisions"
              disabled={createBusy}
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
          {pendingAsk !== null ? (
            <div className="aw-triage-ask" role="group" aria-label="选择执行方式">
              <p>{pendingAsk.question}</p>
              <div className="aw-triage-ask-options">
                {pendingAsk.options.map((option) => (
                  <button
                    className="aw-button"
                    disabled={createBusy}
                    key={option.graph}
                    onClick={() => answerAsk(option.graph)}
                    type="button"
                  >
                    {option.label}
                  </button>
                ))}
              </div>
            </div>
          ) : null}
          <button
            className="aw-button is-primary"
            disabled={
              createBusy ||
              sourceResolving ||
              objective.trim() === "" ||
              attachments.hasBlockingItems
            }
            type="submit"
          >
            {triaging
              ? "正在判定…"
              : createMutation.isPending
                ? "正在创建…"
                : "创建任务"}
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

            {/* One reading column, the way Chat reads: what was asked, the
                steps it took, then the answer directly under them. The side
                rail holds files, which are the one output worth finding again
                later without scrolling back through the run. */}
            <div className="aw-work-body">
            <div className="aw-work-run">

            <TaskStepStream
              lifecycle={lifecycle}
              loading={timeline.loading && timeline.events.length === 0}
              onOpenArtifact={(artifact) => downloadMutation.mutate(artifact)}
              running={!isTerminalStatus(selectedTask.status)}
              stageEvents={stageEvents}
              taskEvents={taskEvents}
            />
            {timeline.error !== null ? (
              <ErrorNotice message={errorMessage(timeline.error, "读取时间线失败")} />
            ) : null}

            <TaskResult
              artifact={openedArtifact ?? finalReport?.artifact ?? null}
              draftText={draftText}
              identity={identity}
              {...(openedArtifact === null
                ? {}
                : { onClose: () => setOpened(null) })}
              onDownload={(artifact) => downloadMutation.mutate(artifact)}
              onRetry={retryInput === null ? undefined : () => resubmit(retryInput)}
              status={selectedTask.status}
              wantsReport={taskInputQuery.data?.wants_report ?? null}
              {...(selectedTask.status_detail === null
                ? {}
                : { statusDetail: selectedTask.status_detail })}
            />

            {/* The decision, after the answer it is a decision about. */}
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
                  {retryGraph !== null ? (
                    <KeyValue
                      label="执行方式"
                      value={retryGraph === "research" ? "调研报告" : "通用执行"}
                    />
                  ) : null}
                  {taskIntent !== null ? (
                    <KeyValue
                      label="方式来源"
                      value={`${decidedByLabel(taskIntent.graph_decided_by)}${
                        taskIntent.reason ? `：${taskIntent.reason}` : ""
                      }`}
                    />
                  ) : null}
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

            </div>
            <div className="aw-work-output">
              <ArtifactRail
                artifacts={artifacts}
                // A file this page can show opens in the reading column; the
                // rest download, which is all a binary can do. Before this,
                // every entry downloaded -- including the .docx a Task was
                // asked to produce, which the page had just learned to render.
                // The rendered document is not the "final report" (that is the
                // exported draft), so it only ever appeared here.
                onOpen={(artifact) => {
                  if (!isPreviewable(artifact.media_type)) {
                    downloadMutation.mutate(artifact);
                  } else if (selectedTaskId !== undefined) {
                    setOpened({ taskId: selectedTaskId, artifact });
                  }
                }}
              />
            </div>
            </div>
          </>
        ) : null}
      </main>
    </div>
  );
}

/**
 * The Task thinking, one step at a time, under the question that started it.
 *
 * This replaces a lifecycle summary sitting above a separately folded event
 * log. Those were the same story told twice: the summary said a stage ran and
 * the log said what it did, and neither was readable without the other. Here a
 * stage *is* its steps -- open one and the real content is inside it.
 *
 * Only the stage that is moving is open. A finished Task collapses to six
 * lines, and expanding any of them shows the prompts, tool calls and outputs
 * that produced it.
 */
function TaskStepStream({
  lifecycle,
  loading,
  onOpenArtifact,
  running,
  stageEvents,
  taskEvents,
}: {
  lifecycle: Lifecycle;
  loading: boolean;
  onOpenArtifact: (artifact: ArtifactRef) => void;
  running: boolean;
  stageEvents: Map<string, EventEnvelope[]>;
  taskEvents: EventEnvelope[];
}) {
  if (loading) return <LoadingLine label="正在读取执行过程" />;

  const stages: StreamStage[] = lifecycle.stages.map((stage) => ({
    id: stage.id,
    title: stage.title,
    state: stage.state,
    note:
      stage.state === "skipped"
        ? "未执行"
        : stage.state === "pending"
          ? "等待中"
          : stage.state === "waiting"
            ? "等待你确认"
            : stage.state === "active"
              ? "进行中"
              : stage.endedAt === null
                ? ""
                : formatTime(stage.endedAt),
    events: stageEvents.get(stage.id) ?? [],
  }));

  return (
    <StepStream
      ariaLabel="执行过程"
      eventTitle={eventTitle}
      isKnownEvent={(event) => isKnownEventType(event.event_type)}
      meta={{ title: "任务生命周期", events: taskEvents }}
      onOpenArtifact={onOpenArtifact}
      running={running}
      stages={stages}
    />
  );
}

/**
 * Everything the Task wrote, in a rail beside the run rather than buried in
 * the step that produced it.
 *
 * An artifact is worth finding after the fact -- the evidence a claim rests on,
 * the file that was exported -- and hunting for the step that happened to write
 * it is not how anyone looks for a file.
 */
function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${String(bytes)} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function ArtifactRail({
  artifacts,
  onOpen,
}: {
  artifacts: TaskArtifact[];
  onOpen: (artifact: ArtifactRef) => void;
}) {
  return (
    <aside className="aw-artifacts" aria-label="附件">
      <div className="aw-artifacts-head">
        <Paperclip aria-hidden="true" size={14} />
        <span>附件</span>
        {artifacts.length === 0 ? null : <em>{artifacts.length}</em>}
      </div>
      {artifacts.length === 0 ? (
        <p className="aw-artifacts-empty">这个任务还没有产生文件。</p>
      ) : (
        <ul>
          {artifacts.map(({ artifact, graphNodeId, producedAt }) => (
            <li key={artifact.artifact_id}>
              <button
                onClick={() => onOpen(artifact)}
                title={artifact.filename ?? artifact.artifact_id}
                type="button"
              >
                <strong>{artifactLabel(artifact)}</strong>
                <small>
                  {graphNodeId === null ? "任务" : workflowStageTitle(graphNodeId)} ·{" "}
                  {formatTime(producedAt)} · {formatBytes(artifact.size_bytes)}
                </small>
              </button>
            </li>
          ))}
        </ul>
      )}
    </aside>
  );
}

/**
 * What the Task produced, shown rather than described.
 *
 * The old version listed the artifact's media type and byte count behind a
 * download button, which told the reader everything about the file except what
 * it said. Text is rendered inline; anything else keeps the download, because
 * that is what a binary is for.
 *
 * A file is one possible output, not the point. Most Tasks are asked a question
 * and the answer is the whole deliverable -- the same thing Chat returns -- so
 * an answer with no file attached is the ordinary case and is presented as one.
 * Only a Task that *was* asked for a file has anything missing when none exists.
 */
function TaskResult({
  artifact,
  draftText,
  identity,
  onClose,
  onDownload,
  onRetry,
  status,
  statusDetail,
  wantsReport,
}: {
  artifact: ArtifactRef | null;
  draftText: string | null;
  identity: PrincipalIdentity;
  /** Set only while showing a file the reader opened from the rail. */
  onClose?: (() => void) | undefined;
  onDownload: (artifact: ArtifactRef) => void;
  onRetry?: (() => void) | undefined;
  status: TaskStatus;
  statusDetail?: string;
  /** `null` while the submitted input is unread, or if it cannot be read. */
  wantsReport: boolean | null;
}) {
  const readable = artifact !== null && isReadableMedia(artifact.media_type);
  const isDocument = artifact !== null && artifact.media_type === DOCX_MEDIA_TYPE;
  const preview = useQuery({
    queryKey: ["work", "artifact-text", artifact?.artifact_id ?? ""],
    enabled: readable,
    staleTime: Number.POSITIVE_INFINITY,
    queryFn: () => {
      if (artifact === null) throw new Error("没有可预览的产物");
      return getArtifactText(identity, artifact.artifact_id);
    },
  });
  // A separate query rather than a branch inside the one above: this one hits a
  // different endpoint, returns a different shape, and is the only one that can
  // fail because a *stored file* will not parse. Sharing a key would also share
  // a cache entry between two unrelated payloads.
  const document = useQuery({
    queryKey: ["work", "artifact-document", artifact?.artifact_id ?? ""],
    enabled: isDocument,
    staleTime: Number.POSITIVE_INFINITY,
    queryFn: () => {
      if (artifact === null) throw new Error("没有可预览的产物");
      return getDocumentPreview(identity, artifact.artifact_id);
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
    // The answer, with no file attached. For a Task that was never asked for
    // one this is simply the result -- headlining it "这次没有生成文件" reported
    // a normal outcome as a shortfall, and named the one thing that did not
    // happen instead of the thing that did.
    if (status === "succeeded" && draftText !== null) {
      return (
        <section className="aw-answer" aria-label="任务结果">
          <header>
            <span className="aw-answer-mark" aria-hidden="true">A</span>
            <strong>回答</strong>
            {wantsReport === true ? <small>没有生成文件</small> : null}
          </header>
          {wantsReport === true ? (
            <p className="aw-page-note">
              这个任务要求生成文件，但没有产出；下面是它写出的内容。
            </p>
          ) : null}
          <MarkdownContent text={draftText} />
        </section>
      );
    }
    if (status === "succeeded") {
      return (
        <section className="aw-result">
          <div className="aw-notice">
            <span>任务已完成，没有产出内容。展开上面的执行过程可以看到每一步做了什么。</span>
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

  // A Task that exported a file reads the same way: the text flows under the
  // run like any other answer, and the file is what the header names.
  return (
    <section className="aw-answer" aria-label="任务产出">
      <header>
        <span className="aw-answer-mark" aria-hidden="true">A</span>
        <strong>{artifact.filename ?? artifact.kind}</strong>
        {/* Not a bare `small`: that one is the header's warning slot, coloured
            for "没有生成文件". A file's size is neutral information. */}
        <span className="aw-answer-size">{formatBytes(artifact.size_bytes)}</span>
        {onClose === undefined ? null : (
          <button
            className="aw-button is-ghost is-small"
            onClick={onClose}
            type="button"
          >
            返回任务结果
          </button>
        )}
        {/* A labelled button, not the bare icon it replaced. That icon sat at
            the end of a header row and was routinely missed -- the file is the
            thing the reader came for, and the way to keep it has to look like
            a way to keep it. */}
        <button
          className="aw-button is-ghost is-small aw-answer-download"
          onClick={() => onDownload(artifact)}
          type="button"
        >
          <FileDown aria-hidden="true" size={14} />
          下载
        </button>
      </header>
      {isDocument ? (
        document.isPending ? (
          <LoadingLine label="正在读取文档内容" />
        ) : document.isError ? (
          <>
            <ErrorNotice
              message={errorMessage(document.error, "无法预览这个文档")}
            />
            {/* The preview is the convenience; the file is the deliverable.
                Saying so keeps a failed extraction from reading as a lost
                document. */}
            <p className="aw-page-note">文件本身没有问题，可以直接下载打开。</p>
          </>
        ) : (
          <>
            <MarkdownContent text={document.data.text} />
            {document.data.truncated ? (
              <p className="aw-page-note">
                文档较长，这里只显示开头；完整内容请下载。
              </p>
            ) : null}
            <p className="aw-page-note">
              这是文档的文字预览，不含排版、图片与页眉页脚
              {document.data.table_count > 0
                ? `；共 ${document.data.table_count} 张表格`
                : ""}
              。需要原样查看请下载。
            </p>
          </>
        )
      ) : !readable ? (
        <p className="aw-page-note">
          {artifact.media_type} · {artifact.size_bytes} 字节，这个类型只能下载查看。
        </p>
      ) : preview.isPending ? (
        <LoadingLine label="正在读取产出内容" />
      ) : preview.isError ? (
        <ErrorNotice message={errorMessage(preview.error, "读取产出内容失败")} />
      ) : (
        <>
          <MarkdownContent text={preview.data.text} />
          {preview.data.truncated ? (
            <p className="aw-page-note">内容较长，这里只显示开头；完整内容请下载。</p>
          ) : null}
        </>
      )}
    </section>
  );
}

/** What a .docx is on the wire. Long enough to be worth naming once. */
const DOCX_MEDIA_TYPE =
  "application/vnd.openxmlformats-officedocument.wordprocessingml.document";

/** Whether opening this file shows it, or can only save it. */
function isPreviewable(mediaType: string): boolean {
  return mediaType === DOCX_MEDIA_TYPE || isReadableMedia(mediaType);
}

/**
 * Text this page can render *by fetching it*. A .docx is deliberately not here:
 * it is readable too, but only through the server's extraction endpoint, and
 * folding it in would send this page's blob fetch at a zip.
 */
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

function decidedByLabel(decidedBy: TaskIntent["graph_decided_by"]): string {
  if (decidedBy === "user") return "用户指定";
  if (decidedBy === "model") return "模型判定";
  return "部署默认";
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
  if (normalized === "work") return "动手做事";
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
