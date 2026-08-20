import {
  useInfiniteQuery,
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import {
  AlertTriangle,
  ChevronRight,
  ClipboardCheck,
  FileDown,
  PanelLeft,
  Paperclip,
  Plus,
  RefreshCw,
  Trash2,
  X,
} from "lucide-react";
import { type FormEvent, useCallback, useEffect, useMemo, useState } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
import {
  ApiError,
  cancelTask,
  deleteTask,
  createTask,
  decideApproval,
  type DocumentLayoutDecline,
  downloadArtifact,
  getApproval,
  getArtifactBlob,
  getArtifactJson,
  getArtifactText,
  getDocumentPdf,
  getDocumentPreview,
  getTask,
  listTasks,
  newIdempotencyKey,
  triageTask,
} from "../../api/client";
import type {
  ApprovalView,
  ArtifactRef,
  DocumentPreview,
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
  useWorkspaceSidebar,
  WorkspaceSidebarPortal,
} from "../../app/WorkspaceSidebar";
import {
  browserShowsPdfInline,
  mediaLabel,
  previewKind,
} from "../../components/media";
import { BlobPreview } from "../../components/BlobPreview";
import { HtmlPreview } from "../../components/HtmlPreview";
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
  ErrorNotice,
  InfoNotice,
  IconButton,
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
  collectWorkspaceWrites,
  eventTitle,
  findDeliverable,
  findDraftText,
  findGraphChoice,
  findLatestApprovalId,
  findTaskInputRef,
  findTaskIntent,
  isKnownEventType,
  locateTimelineGaps,
  parseTaskInputArtifact,
  type TaskArtifact,
  type WorkspaceWriteGroup,
  type TimelineGap,
} from "./workTimeline";

const CANCELLABLE_STATUSES = new Set<TaskStatus>([
  "queued",
  "running",
  "waiting_approval",
]);
/**
 * Statuses nothing will move on its own.
 *
 * The four the domain calls terminal, plus `waiting_migration` -- which it is
 * careful *not* to call terminal, because the Task is neither finished nor
 * abandoned. For everything this page does, though, it belongs with them:
 * `ALLOWED_TRANSITIONS` gives `waiting_migration` no outgoing edge at all
 * (`domain/task_registry.py`), so a Worker cannot pick it up, a retry cannot
 * move it, and a person has to decide what happens to it. Polling one is asking
 * a question whose answer cannot change, and the spinner over it claims work is
 * happening that is not.
 */
const SETTLED_STATUSES = new Set<TaskStatus>([
  "succeeded",
  "failed",
  "cancelled",
  "dead_letter",
  "waiting_migration",
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
  const workspaceSidebar = useWorkspaceSidebar();

  const [taskFilter, setTaskFilter] = useState<TaskFilterId>("all");
  const filterStatuses = TASK_FILTERS.find(
    (entry) => entry.id === taskFilter,
  )?.statuses;

  const tasksQuery = useInfiniteQuery({
    // The filter is part of the key: without it the three views share one
    // cache entry and switching filters shows the previous one's pages until
    // the refetch lands.
    queryKey: ["work", "tasks", taskFilter, ...identityKey],
    initialPageParam: "",
    queryFn: ({ pageParam }) =>
      listTasks(identity, {
        limit: 25,
        // Sent to the server rather than applied to `tasks` below. A client
        // filter only ever sees the pages already loaded, so "失败" on a
        // long history would answer "none" while naming a page it never
        // fetched -- and the emptier the filter, the more pages it would
        // have to be wrong about.
        ...(filterStatuses === undefined ? {} : { statuses: [...filterStatuses] }),
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
      isSettledStatus(query.state.data?.status) ? false : 3_000,
  });

  const timeline = useTaskTimeline(
    identity,
    selectedTaskId,
    2_500,
    !isSettledStatus(taskQuery.data?.status),
  );
  const refreshTimeline = timeline.refresh;
  const selectedTaskStatus = taskQuery.data?.status;
  useEffect(() => {
    if (isSettledStatus(selectedTaskStatus)) void refreshTimeline();
  }, [refreshTimeline, selectedTaskStatus]);

  // And the same handshake in the other direction, because either side can be
  // the one that finds out first. React Query pauses `refetchInterval` while
  // the tab is hidden and the timeline's own `setInterval` does not, so a Task
  // that finishes while the reader is in another tab leaves a settled timeline
  // under a header still reading 排队中 -- the page contradicting itself, until
  // a reload. Asking once, when the timeline says the Task is over, is enough:
  // `taskQuery` stops polling on a settled status, so this cannot loop.
  const timelineSettled = timeline.settled;
  const refetchTask = taskQuery.refetch;
  useEffect(() => {
    if (timelineSettled && !isSettledStatus(selectedTaskStatus)) {
      void refetchTask();
    }
  }, [refetchTask, selectedTaskStatus, timelineSettled]);
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
      // 这一句会走到界面上：`errorMessage` 优先用 message 而不是兜底文案，
      // 所以它必须是说给读者听的，不是说给写这段代码的人听的。
      if (parsed === null) {
        throw new Error("读不到这个任务的提交内容，格式和这个版本对不上。");
      }
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
  // The stream above shows what arrived; this is what the server said did not.
  // An empty `skippedSequences` is its claim that the pages were complete, so
  // silence here is not neutral -- it is the one way this page can present a
  // partial history as a whole one, which is what the field exists to prevent
  // (application/tasks.py). Anchored to the events either side of each hole,
  // because that is what positions buy over a count.
  const timelineGaps = useMemo(
    () => locateTimelineGaps(timeline.events, timeline.skippedSequences),
    [timeline.events, timeline.skippedSequences],
  );
  // Same events, a different question, so a second memo rather than one pass
  // returning both: the rail's two groups have nothing in common except the
  // sidebar they sit in, and merging them would put "is this openable" back
  // inside a single list.
  const workspaceWrites = useMemo(
    () => collectWorkspaceWrites(timeline.events),
    [timeline.events],
  );
  const artifacts = useMemo(
    () => collectArtifacts(timeline.events),
    [timeline.events],
  );
  const hasOutputRail = artifacts.length > 0 || workspaceWrites.length > 0;
  // What the reading column leads with: the document the Task was asked for
  // when it rendered one, and the exported report otherwise. `findFinalReport`
  // is still the thing being chosen *between* -- it is now consulted inside
  // `findDeliverable` rather than here, so there is one answer to "what is this
  // Task's output" instead of two that could disagree.
  const deliverable = useMemo(
    () => findDeliverable(timeline.events),
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
  const [triageError, setTriageError] = useState<string | null>(null);
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
    setTriageError(null);
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
      // Keyed off the response, not off `taskQueryKey`. A pending mutation
      // takes the *latest* render's options -- `MutationObserver.setOptions`
      // pushes them in, and `Mutation.execute` reads `onSuccess` only after
      // the request resolves -- so a reader who opens another task while the
      // cancel is in flight makes this callback close over that task's key.
      // The write then filed A's TaskView under B, and /work/B rendered A's
      // objective, A's id and a 已取消 pill over B's own timeline. It stuck,
      // too: the poll stops on a settled status, and the timeline repair
      // effect is gated on the status not being settled. The response already
      // carries the only id this write is entitled to use.
      queryClient.setQueryData(
        ["work", "task", ...identityKey, task.task_id],
        task,
      );
      void queryClient.invalidateQueries({
        queryKey: ["work", "tasks", ...identityKey],
      });
      void timeline.refresh();
    },
  });

  const deleteMutation = useMutation({
    mutationFn: (taskId: string) => deleteTask(identity, taskId),
    onSuccess: (deleted) => {
      void queryClient.invalidateQueries({
        queryKey: ["work", "tasks", ...identityKey],
      });
      // Only when it was the one on screen. A reader who deleted a different
      // row from the list is still reading what they were reading.
      if (deleted.task_id === selectedTaskId) void navigate("/work");
    },
  });

  const removeTask = useCallback(
    async (taskId: string) => {
      // Irreversible, and it takes the timeline and the report record with it.
      // The artifacts survive (ADR-056 §5) but nothing links to them any more,
      // which is worth saying before rather than explaining afterwards.
      if (!window.confirm("删除这个任务？它的执行记录会一起消失，产出文件不再可达。")) {
        return;
      }
      try {
        await deleteMutation.mutateAsync(taskId);
      } catch {
        // Reported by `deleteMutation.error` below, next to the list it
        // failed on -- rather than thrown out of a click handler.
      }
    },
    [deleteMutation],
  );

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
      // Same reason as the cancel above: the response's own id, not the key
      // this render happens to hold. Filed under another task's approval key
      // the render-time `matchesTask` guard did catch it -- by showing that
      // task a spurious 对不上 notice and hiding its decision buttons, while
      // the poll stopped because the poisoned record was no longer pending.
      // A caught poisoning that leaves the gate undecidable is still a bug.
      queryClient.setQueryData(
        ["work", "approval", ...identityKey, approval.approval_id],
        approval,
      );
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
    setTriageError(null);
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
    } catch (error: unknown) {
      // Triage happens before the Task mutation exists, so its failures do not
      // reach `createMutation.error`. Catch them here instead of leaving a
      // rejected promise behind a button that merely becomes enabled again.
      setTriageError(
        errorMessage(error, "无法判定执行方式，请稍后重试。"),
      );
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
  const createTaskForm = (
    <form className="aw-create-task" id="aw-create-task-form" onSubmit={handleCreate}>
      <header className="aw-create-task-head">
        <h2>想完成什么？</h2>
        <p>描述结果，Agent 会选择合适的执行方式并持续保存进度。</p>
      </header>
      <label className="aw-sr-only" htmlFor="work-objective">目标</label>
      <textarea
        id="work-objective"
        disabled={createBusy}
        maxLength={4096}
        onChange={(event) => {
          setObjective(event.target.value);
          markTaskIntentEdited();
        }}
        placeholder="例如：整理项目资料，比较三个方案并输出建议报告"
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
          disabled={createBusy || attachments.readOnlyReason !== null}
          {...(attachments.readOnlyReason === null
            ? {}
            : { disabledReason: attachments.readOnlyReason })}
          onFiles={attachments.addFiles}
        />
        <span>
          {attachments.readOnlyReason ?? (
            <>
              添加 PDF、Word 或 Markdown
              {knowledgeBaseId === null
                ? "（选择知识库后上传）"
                : "（上传到所选知识库）"}
            </>
          )}
        </span>
      </div>
      <AttachmentTray
        items={attachments.items}
        onRemove={attachments.remove}
        onRetry={attachments.retry}
      />
      <details className="aw-work-advanced">
        <summary>高级设置</summary>
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
          <option value="auto">自动判定（由模型决定要不要文件）</option>
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
      {attachments.hasBlockingItems ? (
        <p className="aw-create-task-hint">
          附件正在上传或索引，完成后才能创建任务。
        </p>
      ) : null}
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
      {triageError === null ? null : <ErrorNotice message={triageError} />}
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
  );

  return (
    <div className={`aw-work-page ${selectedTaskId === undefined ? "" : "has-selection"}`}>
      <WorkspaceSidebarPortal>
        <aside className="aw-work-sidebar" aria-label="任务列表与新建任务">
        <header className="aw-pane-header">
          <div>
            <strong>最近任务</strong>
          </div>
          <div className="aw-pane-header-actions">
            <button
              // 不叫「刷新任务列表」：那个名字里整整齐齐含着「新任务」，
              // 而「新任务」是它上面两行的另一个按钮。两个控件的无障碍名
              // 互为子串，对按名字找控件的人和工具都是一次歧义。
              aria-label="重新加载列表"
              className="aw-icon-button"
              disabled={tasksQuery.isFetching}
              onClick={() => void tasksQuery.refetch()}
              type="button"
            >
              <RefreshCw aria-hidden="true" size={16} />
            </button>
            <IconButton
              className="aw-work-sessions-close"
              label="关闭任务列表"
              onClick={workspaceSidebar.close}
            >
              <X aria-hidden="true" size={17} />
            </IconButton>
          </div>
        </header>

        <button
          aria-current={selectedTaskId === undefined ? "page" : undefined}
          className={`aw-create-task-toggle ${
            selectedTaskId === undefined ? "is-active" : ""
          }`}
          onClick={() => {
            workspaceSidebar.close();
            if (selectedTaskId === undefined) {
              window.requestAnimationFrame(() =>
                document.getElementById("work-objective")?.focus(),
              );
            } else {
              void navigate("/work");
            }
          }}
          type="button"
        >
          <Plus aria-hidden="true" size={16} />
          新任务
        </button>

        <div className="aw-task-filters" role="group" aria-label="任务筛选">
          {TASK_FILTERS.map((entry) => (
            <button
              aria-pressed={taskFilter === entry.id}
              className={taskFilter === entry.id ? "is-active" : ""}
              key={entry.id}
              onClick={() => setTaskFilter(entry.id)}
              type="button"
            >
              {entry.label}
            </button>
          ))}
        </div>

        <nav className="aw-task-list" aria-label="任务">
          {tasksQuery.isPending ? <LoadingLine label="正在加载任务" /> : null}
          {tasksQuery.isError ? (
            <ErrorNotice message={errorMessage(tasksQuery.error, "加载任务列表失败")} />
          ) : null}
          {tasks.map((task) => (
            <div className="aw-task-list-row" key={task.task_id}>
              <Link
                aria-current={task.task_id === selectedTaskId ? "page" : undefined}
                className={`aw-task-list-item ${
                  task.task_id === selectedTaskId ? "is-active" : ""
                }`}
                onClick={workspaceSidebar.close}
                to={`/work/${encodeURIComponent(task.task_id)}`}
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
                  </small>
                </span>
                <span
                  aria-label={`状态：${formatStatus(task.status)}`}
                  className="aw-task-status-dot"
                  data-status={task.status}
                  role="img"
                  title={formatStatus(task.status)}
                />
              </Link>
              {/* Offered only on a settled Task, because the server refuses
                  anything else with a 409 -- and a button whose only outcome is
                  an error teaches the reader the wrong rule. Cancelling is how
                  a running Task becomes deletable, and it already has its own
                  control in the detail pane. */}
              {isSettledStatus(task.status) ? (
                <button
                  aria-label={`删除任务 ${task.objective_preview ?? task.task_id}`}
                  className="aw-task-list-delete"
                  disabled={deleteMutation.isPending}
                  onClick={() => void removeTask(task.task_id)}
                  title="删除"
                  type="button"
                >
                  <Trash2 aria-hidden size={13} />
                </button>
              ) : null}
            </div>
          ))}
          {deleteMutation.isError ? (
            <ErrorNotice
              message={errorMessage(deleteMutation.error, "删除任务失败")}
            />
          ) : null}
          {!tasksQuery.isPending && !tasksQuery.isError && tasks.length === 0 && taskFilter !== "all" ? (
            // A filtered empty list is not an empty account. Saying "还没有任务"
            // here would contradict the list the reader was looking at one
            // click ago.
            <p className="aw-page-note">
              这个筛选下没有任务。
              <button
                className="aw-link-button"
                onClick={() => setTaskFilter("all")}
                type="button"
              >
                看全部
              </button>
            </p>
          ) : null}
          {!tasksQuery.isPending && !tasksQuery.isError && tasks.length === 0 && taskFilter === "all" ? (
            <p className="aw-muted">还没有任务。说一件要做的事，就能开一个。</p>
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
      </WorkspaceSidebarPortal>

      <main className="aw-work-detail">
        {selectedTaskId !== undefined && selectedTask === undefined ? (
          <button
            aria-controls="workspace-sidebar-context"
            aria-expanded={workspaceSidebar.drawerOpen}
            aria-label="打开任务列表"
            className="aw-icon-button aw-work-mobile-back"
            onClick={workspaceSidebar.open}
            type="button"
          >
            <PanelLeft aria-hidden="true" size={18} />
          </button>
        ) : null}
        {selectedTaskId === undefined ? (
          <section className="aw-work-start">
            <button
              aria-controls="workspace-sidebar-context"
              aria-expanded={workspaceSidebar.drawerOpen}
              aria-label="打开任务列表"
              className="aw-icon-button aw-work-mobile-back"
              onClick={workspaceSidebar.open}
              type="button"
            >
              <PanelLeft aria-hidden="true" size={18} />
            </button>
            {createTaskForm}
          </section>
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
                  aria-controls="workspace-sidebar-context"
                  aria-expanded={workspaceSidebar.drawerOpen}
                  className="aw-button is-ghost aw-work-mobile-back"
                  onClick={workspaceSidebar.open}
                  type="button"
                >
                  <PanelLeft aria-hidden="true" size={15} /> 任务列表
                </button>
                <h1 title={selectedTask.task_id}>
                  {selectedTask.objective_preview ?? shortId(selectedTask.task_id, 28)}
                </h1>
              </div>
              <StatusPill status={selectedTask.status} />
            </header>

            {/* The one thing this page says out loud, and it is one
                sentence rather than the run.

                Nothing on this page was announced before: the pill above
                flips 运行中 → 等待批准 → 已完成 in silence, and the
                approval gate below mounts in silence. A reader using a
                screen reader submitted a task and then heard nothing
                again -- including the moment the run stopped and asked
                them a question, which is the moment the whole Task shape
                exists for. That is not an awkward page; it is a task that
                cannot be finished.

                Deliberately the *status*, not the trace. The timeline
                below streams dozens of events per run and announcing
                them is the same as announcing nothing -- see the live
                regions on the two transcripts. `aria-atomic` because the
                sentence only means anything whole. */}
            <p aria-atomic="true" className="aw-sr-only" role="status">
              任务{formatStatus(selectedTask.status)}
            </p>

            {/* Lead with the outcome. The execution trace remains directly
                below it for inspection, while the side rail appears only when
                there are additional files worth finding again later. */}
            <div className={`aw-work-body ${hasOutputRail ? "has-output" : ""}`}>
            <div className="aw-work-run">

            <TaskResult
              artifact={openedArtifact ?? deliverable}
              draftText={draftText}
              identity={identity}
              {...(
                // Only when closing would actually show something else.
                // Opening the deliverable from the rail lands on what the
                // column was already showing, and a "返回任务结果" that returns
                // to the file you are looking at is a button that does nothing.
                openedArtifact === null ||
                openedArtifact.artifact_id === deliverable?.artifact_id
                  ? {}
                  : { onClose: () => setOpened(null) }
              )}
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

            <TaskStepStream
              lifecycle={lifecycle}
              loading={timeline.loading && timeline.events.length === 0}
              // Previewable files open in the reading column, same as a rail
              // click; only the kinds no viewer exists for still download.
              // "打开产物" that saved a file it could have shown was the bug.
              // No gate. The rail dropped its own version of this check and
              // wrote down why ("栏位不再预判"): sending an unpreviewable file
              // straight to a download was zero feedback for a click, and the
              // reading column already says what it cannot show. The step
              // stream kept the old behaviour, so the same .zip answered a
              // sentence in one place and a silent save in the other.
              onOpenArtifact={(artifact) => {
                if (selectedTaskId === undefined) return;
                setOpened({ taskId: selectedTaskId, artifact });
              }}
              stageEvents={stageEvents}
              status={selectedTask.status}
              taskEvents={taskEvents}
            />
            <TimelineGapNotice gaps={timelineGaps} />
            {timeline.error !== null ? (
              <ErrorNotice message={errorMessage(timeline.error, "读取执行过程失败")} />
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
                  {selectedTask.agent_invocation_count > 0 ? (
                    <KeyValue
                      label="智能体调用"
                      value={`${selectedTask.agent_invocation_count} 次`}
                    />
                  ) : null}
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
            {hasOutputRail ? (
              <div className="aw-work-output">
                <ArtifactRail
                artifacts={artifacts}
                // Every entry opens in the reading column, whatever its type.
                // The gate that used to be here sent "not previewable" to a
                // silent download -- clicking a produced .png saved a file with
                // no feedback and showed nothing -- and its list of what *is*
                // previewable went stale the moment the column learned a new
                // kind. The column already answers per type, including "this
                // one you can only download", so the rail stopped pre-judging.
                onOpen={(artifact) => {
                  if (selectedTaskId !== undefined) {
                    setOpened({ taskId: selectedTaskId, artifact });
                  }
                }}
                workspaceWrites={workspaceWrites}
                />
              </div>
            ) : null}
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
  status,
  stageEvents,
  taskEvents,
}: {
  lifecycle: Lifecycle;
  loading: boolean;
  onOpenArtifact: (artifact: ArtifactRef) => void;
  // The status itself rather than a `running` boolean derived at the call site.
  // Two things here need it and they need different answers from it: whether
  // the stream is live, and what a stopped stage is stopped *on*. A boolean
  // could only carry the first, which is how a parked Task ended up with a
  // stage that said 等待你确认 about a decision nobody is being asked to make.
  status: TaskStatus;
  stageEvents: Map<string, EventEnvelope[]>;
  taskEvents: EventEnvelope[];
}) {
  if (loading) return <LoadingLine label="正在读取执行过程" />;

  const stages: StreamStage[] = lifecycle.stages.map((stage) => ({
    id: stage.id,
    title: stage.title,
    state: stage.state,
    // 一段可能对应两个节点（plan · route、approval · export），所以是连起来的
    // 一串而不是一个。表里没有的节点不画：那种情况下标题本身就是节点 id。
    ...(stage.nodes.length === 0 ? {} : { nodes: stage.nodes.join(" · ") }),
    ...(stageDuration(stage) === null ? {} : { duration: stageDuration(stage) as string }),
    note:
      stage.state === "skipped"
        ? "未执行"
        : stage.state === "pending"
          ? "等待中"
          : stage.state === "waiting"
            ? status === "waiting_migration"
              ? "等待迁移"
              : "等待你确认"
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
      meta={{ title: "运行记录", events: taskEvents }}
      onOpenArtifact={onOpenArtifact}
      running={!isSettledStatus(status)}
      stages={stages}
    />
  );
}

/**
 * The part of the run this page was told it did not receive.
 *
 * Under the stream rather than over it, because it is a statement *about* the
 * steps above: a reader has to have seen them to know what is missing from
 * them. Each hole names the two steps it fell between, so the incompleteness
 * is attached to a stretch of the run instead of floating over the whole
 * thing -- an operator can also take the position straight to the log row.
 *
 * The wording says "没有交给这个页面" and not "丢了": the rows are still in the
 * log. What failed is decoding them here, and telling a user their history was
 * destroyed when it was not would send them looking for the wrong thing.
 */
function TimelineGapNotice({ gaps }: { gaps: TimelineGap[] }) {
  if (gaps.length === 0) return null;

  return (
    <div className="aw-notice is-warning aw-timeline-gaps">
      <AlertTriangle aria-hidden="true" size={16} />
      {/* A div rather than the `span` the other notices wrap their text in,
          because this one carries a list and a span may only hold phrasing. */}
      <div>
        <strong>
          这段历史不完整：上面的步骤中缺了 {gaps.length} 个位置。
        </strong>
        <small>这些事件仍在日志里，只是这次没能解码、没有交给这个页面。</small>
        <ul>
          {gaps.map((gap) => (
            <li key={gap.sequence}>{describeTimelineGap(gap)}</li>
          ))}
        </ul>
      </div>
    </div>
  );
}

/** Where one hole sits, in terms of the events the reader can actually see. */
function describeTimelineGap(gap: TimelineGap): string {
  const position = `#${String(gap.sequence)}`;
  if (gap.before !== null && gap.after !== null) {
    return `${position}：在「${eventTitle(gap.before)}」与「${eventTitle(gap.after)}」之间`;
  }
  if (gap.before !== null) return `${position}：在「${eventTitle(gap.before)}」之后`;
  if (gap.after !== null) return `${position}：在「${eventTitle(gap.after)}」之前`;
  // Nothing readable came back around it, so there is no step to hang it on.
  // Said plainly rather than dressed up as a location this page does not have.
  return `${position}：前后都没有读出来的事件`;
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
  workspaceWrites,
}: {
  artifacts: TaskArtifact[];
  onOpen: (artifact: ArtifactRef) => void;
  /** Files in the Task's working set: named here, openable nowhere (F-14). */
  workspaceWrites: WorkspaceWriteGroup[];
}) {
  return (
    <aside className="aw-artifacts" aria-label="产出文件">
      <div className="aw-artifacts-head">
        <Paperclip aria-hidden="true" size={14} />
        <span>产出文件</span>
        {artifacts.length === 0 ? null : <em>{artifacts.length}</em>}
      </div>
      {artifacts.length === 0 ? (
        // Narrowed, because it is no longer the only thing this rail knows
        // about. "这个任务还没有产生文件" would be false on a Task whose stages
        // wrote three of them into the working set, listed directly below.
        <p className="aw-artifacts-empty">
          {workspaceWrites.length === 0
            ? "这个任务还没有产生文件。"
            : "这个任务没有产生可以下载的文件。"}
        </p>
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

      {/* The second group, and it is deliberately not buttons. These names come
          from `ToolCompleted.workspace_writes`, which ADR-063 publishes
          unconditionally -- outside the `record_step_inputs` gate, so they
          survive a default deployment -- and which this page read none of until
          now: a Task that rendered three files into its working set showed the
          reader nothing, not even a count.

          They cannot be opened from here and the heading says so rather than
          leaving the reader to discover it by clicking. A working-set file is
          addressed by name inside a session, and the only routes that read one
          are mounted under `/v1/code/sessions` behind a `mode="code"` check;
          giving Task its own would be a second authorization surface and a
          second addressing scheme, which is a boundary change with its own ADR
          (known-gaps F-14). Naming them is what can be done without one, and it
          is strictly more than the silence it replaces. */}
      {workspaceWrites.length === 0 ? null : (
        <div className="aw-artifacts-workspace">
          <h3>任务工作区里的文件</h3>
          <p className="aw-artifacts-note">
            这些文件在任务自己的工作区里，不是可下载的产物，控制台打不开它们。
          </p>
          {workspaceWrites.map((group) => (
            <div key={group.graphNodeId ?? "任务"}>
              <h4>
                {group.graphNodeId === null
                  ? "任务"
                  : workflowStageTitle(group.graphNodeId)}
              </h4>
              <ul className="aw-artifacts-names">
                {group.names.map((name) => (
                  <li key={name}>{name}</li>
                ))}
              </ul>
            </div>
          ))}
        </div>
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
 *
 * `waiting_approval` is not "still running" even though it is not terminal, and
 * that distinction is the whole point of the gate: the draft exists, the export
 * has not happened, and the reader is being asked to decide about the text. It
 * used to render as a spinner, which left the approve button asking for consent
 * to something the console would not show.
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
  // One vocabulary for "what does this file get" (`components/media.ts`),
  // instead of the pair of booleans whose negation used to mean "download,
  // silently" for everything that was neither text nor a document.
  const kind = artifact === null ? "none" : previewKind(artifact.media_type);
  const readable = kind === "text";
  const isDocument = kind === "docx";
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
  // Which file the reader sent back to text, rather than which one they asked
  // to lay out. The polarity is the point: a Word document opens on 版面 now,
  // because the document is what the Task was asked for, and opening it onto
  // extracted text read as "the task produced plain text" -- the rendered page
  // sat behind a control nothing pointed at. Still an artifact id rather than
  // a boolean: this component stays mounted while the reading column moves
  // from one artifact to the next, and a 文字 chosen for one document must not
  // decide the view for the next one. Narrowed on read, the same way the page
  // decides which artifact is open at all.
  const [textFor, setTextFor] = useState<string | null>(null);
  // Asked before the conversion is, because a browser that will not paint a
  // PDF makes the whole layout half moot: the server would start an external
  // converter, hold a document in memory and send it, for a frame that shows
  // the reader nothing. Declining here costs one property read.
  const viewerShowsPdf = browserShowsPdfInline();
  const wantsLayout =
    isDocument &&
    artifact !== null &&
    textFor !== artifact.artifact_id &&
    viewerShowsPdf;
  // The third query on one artifact, and it earns the same answer the second
  // one did: a different endpoint, a different shape, and a failure that means
  // something else again. This is the only one that can come back "this
  // deployment has no converter", which is a fact about the server rather than
  // about the document -- and the reason it resolves rather than throws.
  const layout = useQuery({
    queryKey: ["work", "artifact-layout", artifact?.artifact_id ?? ""],
    enabled: wantsLayout,
    staleTime: Number.POSITIVE_INFINITY,
    queryFn: () => {
      if (artifact === null) throw new Error("没有可预览的产物");
      return getDocumentPdf(identity, artifact.artifact_id);
    },
  });
  const layoutBlob = layout.data?.available === true ? layout.data.blob : null;
  // The first object URL on this page that has to outlive the render that made
  // it: a frame keeps reading its source, so the revoke `downloadArtifact` does
  // one line after the click would blank the panel here.
  //
  // Tied to the frame element instead of to a render, through a ref callback
  // and React 19's ref cleanup. The URL is created when the node appears and
  // revoked when it goes -- unmount, a switch back to the text view, a move to
  // another artifact -- which is the leak this has to not be: one URL per
  // preview, held for the life of the tab. Memoized on the blob because an
  // inline callback is a new function every render, and React would detach and
  // re-attach it each time, revoking a source the frame is still displaying.
  const attachLayoutFrame = useCallback(
    (frame: HTMLIFrameElement | null) => {
      if (frame === null || layoutBlob === null) return;
      const url = URL.createObjectURL(layoutBlob);
      frame.src = url;
      return () => {
        URL.revokeObjectURL(url);
      };
    },
    [layoutBlob],
  );
  // A decline is not an error and is deliberately not read off one. A network
  // failure is the only thing that reaches `isError` here, and it lands on the
  // same fallback as every declared refusal: there is no layout, the text
  // preview is unaffected, and the reader is told which of those is true.
  const layoutDeclined: PanelLayoutDecline | null = !viewerShowsPdf
    ? "viewer_unavailable"
    : layout.data?.available === false
      ? layout.data.reason
      : layout.isError
        ? "unavailable"
        : null;
  // What the panel shows, not what was asked for. A declined layout snaps the
  // control to 文字 rather than leaving 版面 lit over text -- the reader would
  // have no way to tell the view named from the one they got. And because 版面
  // is where a document now opens, this snap is also how a deployment without
  // a converter degrades: onto the text view with the note below saying why,
  // never silently.
  const showingLayout = wantsLayout && layoutDeclined === null;

  if (artifact === null) {
    // Parked, and its own thing. This Task was submitted under run semantics
    // this deployment cannot build, so the Registry stopped it and wrote why
    // (`waiting_migration` is one of the statuses that must carry a detail).
    // It is not executing and it is not finished, and -- unlike every other
    // non-terminal status -- nothing brings it back: the transition table gives
    // it no outgoing edge, so neither waiting nor resubmitting this Task moves
    // it. Reported as "任务正在执行" it was two lies at once, and the second one
    // is the expensive one: it invited the reader to wait for an event that
    // cannot arrive.
    if (status === "waiting_migration") {
      return (
        <section className="aw-result">
          <div className="aw-notice is-warning">
            <AlertTriangle aria-hidden="true" size={16} />
            <span>
              <strong>任务在等待迁移，没有在执行</strong>
              <small>
                它是按这套部署跑不了的执行版本提交的，已经停在这里：等下去不会有进展，
                重新提交同一个任务也不会，需要管理员先处理版本迁移。
              </small>
              {/* The server's own sentence, verbatim. It is English and it is
                  written for whoever has to act on it -- which is not the
                  reader of this page, but is the person they will quote it
                  to. */}
              {statusDetail === undefined ? null : <small>{statusDetail}</small>}
            </span>
          </div>
        </section>
      );
    }
    // The text the approval gate is a gate over. Held back until the reader
    // decided, it is exactly what they are being asked about, so this is the
    // one non-terminal status that has something to show.
    const awaitingDecision = status === "waiting_approval" && draftText !== null;
    if (!isSettledStatus(status) && !awaitingDecision) {
      return (
        <section className="aw-result is-running">
          {/* At the gate with no draft in hand means the timeline has not
              caught up yet -- it is still arriving, and the decision below is
              about text this page is in the middle of reading. Saying "任务正在
              执行" there would name the wrong thing as the reason to wait. */}
          <LoadingLine
            label={
              status === "waiting_approval"
                ? "正在读取待确认的内容"
                : "任务正在执行，完成后结果会显示在这里"
            }
          />
        </section>
      );
    }
    // The answer, with no file attached. For a Task that was never asked for
    // one this is simply the result -- headlining it "这次没有生成文件" reported
    // a normal outcome as a shortfall, and named the one thing that did not
    // happen instead of the thing that did.
    if ((status === "succeeded" || awaitingDecision) && draftText !== null) {
      // Only a finished Task can be missing a file it was asked for. One still
      // at the gate has not reached the export step, so saying so here would
      // report the gate itself as a shortfall.
      const missingReport = wantsReport === true && !awaitingDecision;
      return (
        <section
          className="aw-answer"
          aria-label={awaitingDecision ? "待确认的内容" : "任务结果"}
        >
          {/* Same caveat the exported branch shows (ADR-060): a success whose
              reviewer was still unsatisfied says so, file or no file. */}
          {status === "succeeded" && statusDetail !== undefined ? (
            <InfoNotice>
              <span>
                <strong>评审仍有未解决的意见</strong>
                <small>{statusDetail}</small>
              </span>
            </InfoNotice>
          ) : null}
          <header>
            <span className="aw-answer-mark" aria-hidden="true">A</span>
            <strong>{awaitingDecision ? "待确认的内容" : "回答"}</strong>
            {missingReport ? <small>没有生成文件</small> : null}
          </header>
          {missingReport ? (
            <p className="aw-page-note">
              这个任务要求生成文件，但没有产出；下面是它写出的内容。
            </p>
          ) : null}
          {awaitingDecision ? (
            <p className="aw-page-note">
              下面是任务写出的内容。确认后才会导出成文件；请先看过再决定。
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
    // Failed, cancelled or dead-lettered.
    return (
      <section className="aw-result">
        <RunFailure
          onRetry={onRetry}
          status={status}
          {...(statusDetail === undefined ? {} : { statusDetail })}
        />
        {/* A Task can fail after it has already written something -- a rejected
            export is the plain case, and every step failure downstream of
            `synthesize` is another. The draft lives in the timeline events, not
            in a field that the failure cleared, so it is still here; until now
            the status branch simply never asked for it, and the reader lost
            work that had been done. Shown under the notice rather than instead
            of it: why it stopped is the headline, what it wrote is the salvage. */}
        {draftText === null ? null : (
          <section className="aw-answer" aria-label="任务停下前写出的内容">
            <header>
              <span className="aw-answer-mark" aria-hidden="true">A</span>
              <strong>停下前写出的内容</strong>
            </header>
            <MarkdownContent text={draftText} />
          </section>
        )}
      </section>
    );
  }

  // A Task that exported a file reads the same way: the text flows under the
  // run like any other answer, and the file is what the header names.
  //
  // A succeeded Task's `status_detail` is the review caveat and nothing else
  // (ADR-060): the reviewer ran out of revisions still wanting changes, the
  // work shipped anyway, and hiding that would present a disputed draft as a
  // clean pass. The sentence is the server's own record, shown verbatim.
  const reviewCaveat =
    status === "succeeded" && statusDetail !== undefined ? statusDetail : null;
  return (
    <section className="aw-answer" aria-label="任务产出">
      {/* Above the file, and this branch did not have it at all. A Task can
          fail *after* rendering something -- a `render_document` that succeeded
          and an export that was refused is the plain case -- and then this
          branch drew the .docx layout, the download control and nothing else.
          The reader saw a finished-looking document under a heading that named
          it the deliverable, with the failure reported only by a pill in the
          page header. Which run stopped, and why, is not a decoration on the
          artifact view; it is the first thing about it that is true. */}
      <RunFailure
        onRetry={onRetry}
        status={status}
        {...(statusDetail === undefined ? {} : { statusDetail })}
      />
      {reviewCaveat === null ? null : (
        <InfoNotice>
          <span>
            <strong>评审仍有未解决的意见，产物按现状导出</strong>
            <small>{reviewCaveat}</small>
          </span>
        </InfoNotice>
      )}
      <header>
        <span className="aw-answer-mark" aria-hidden="true">A</span>
        {/* What it *is*, then what it is called. The header used to lead with
            the raw filename, so one file wore two names in one screen: the rail
            called it 报告文件 (`artifactLabel`, from the artifact kind) and the
            heading four inches away called it `report.md`. A reader scanning
            back for "the report" had no reason to connect them.

            The filename does not disappear -- it is what downloads, and a
            reader who wants to name the file to somebody else needs it -- but
            it is the subtitle, because the kind is what answers "which of the
            things this Task made is this". Where the kind is unknown,
            `artifactLabel` already falls back to the filename, and the subtitle
            is dropped rather than printed twice. */}
        <strong>{artifactLabel(artifact)}</strong>
        {artifact.filename === null ||
        artifact.filename === undefined ||
        artifact.filename === artifactLabel(artifact) ? null : (
          <span className="aw-answer-filename">{artifact.filename}</span>
        )}
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
            {/* The same control the approvals filter uses. Two views of one
                file, so the reader picks rather than scrolls past the wrong
                one; 版面 goes flat once this deployment has said it cannot,
                because a button that has already refused should not keep
                offering. */}
            <div className="aw-segmented aw-preview-views" aria-label="预览方式">
              <button
                aria-pressed={showingLayout}
                className={showingLayout ? "is-active" : ""}
                disabled={layoutDeclined !== null}
                onClick={() => setTextFor(null)}
                type="button"
              >
                版面
              </button>
              <button
                aria-pressed={!showingLayout}
                className={showingLayout ? "" : "is-active"}
                onClick={() => setTextFor(artifact.artifact_id)}
                type="button"
              >
                文字
              </button>
            </div>
            {/* Above the text it is explaining, and a note rather than an
                `ErrorNotice`: nothing here failed for the reader. The text
                below is intact and the file downloads unchanged, so painting
                this red would report the shape of a deployment as a fault and
                cast doubt on a preview that is fine. */}
            {layoutDeclined === null ? null : (
              <p className="aw-page-note">{layoutDeclineNote(layoutDeclined)}</p>
            )}
            {showingLayout ? (
              layoutBlob === null ? (
                <LoadingLine label="正在生成版面预览" />
              ) : (
                <>
                  <div className="aw-preview-frame">
                    {/* No `sandbox`. These bytes were typed `application/pdf`
                        by the client before the URL existed, so the frame can
                        only be the browser's own PDF viewer -- and a sandbox
                        strict enough to matter also stops that viewer, which
                        shows an empty panel with nothing saying why. */}
                    <iframe ref={attachLayoutFrame} title="版面预览" />
                  </div>
                  {/* The second sentence is for the frame above having shown
                      nothing. `browserShowsPdfInline` catches only browsers
                      that admit they have no viewer; a Chromium web view
                      reports one, paints its backdrop and raises nothing, so
                      there is no state this component could have entered
                      instead. What is left is to name the thing the reader is
                      looking at -- a flat dark rectangle -- and point at the
                      two ways out, rather than let it read as a broken
                      document. */}
                  <p className="aw-page-note">
                    这是转换出来的版面预览，和 Word 打开可能有细微差别；需要原样查看请下载。
                    这里若是一片空白或纯黑，是这个浏览器不显示内嵌
                    PDF——文档没问题，点「文字」看内容，或下载后用 Word 打开。
                  </p>
                </>
              )
            ) : (
              <>
                <MarkdownContent text={document.data.text} />
                {/* The cut used to be a note of its own, right here. It is a
                    row *in* the list now: an empty list is how this page says
                    the preview is faithful, and a note standing beside that
                    emptiness does not stop it being said. */}
                <PreviewGaps preview={document.data} />
                <p className="aw-page-note">
                  这是文档的文字预览，不含排版；需要原样查看请下载。
                </p>
              </>
            )}
          </>
        )
      ) : kind === "image" || kind === "pdf" ? (
        <BlobPreview
          kind={kind}
          load={() => getArtifactBlob(identity, artifact.artifact_id)}
          name={artifact.filename ?? artifact.kind}
          queryKey={["work", "artifact-blob", artifact.artifact_id]}
          sizeBytes={artifact.size_bytes}
        />
      ) : kind === "html" ? (
        // Rendered live in HtmlPreview's sandbox frame, not fed to the
        // Markdown path below -- which used to happen and answered a page
        // with its own sanitised remains: no source, no rendering, nothing.
        <HtmlPreview
          load={() => getArtifactText(identity, artifact.artifact_id)}
          name={artifact.filename ?? artifact.kind}
          queryKey={["work", "artifact-html", artifact.artifact_id]}
          sizeBytes={artifact.size_bytes}
        />
      ) : !readable ? (
        <p className="aw-page-note">
          {mediaLabel(artifact.media_type)} · {formatBytes(artifact.size_bytes)}
          ，这个类型只能下载后查看。
        </p>
      ) : preview.isPending ? (
        <LoadingLine label="正在读取产出内容" />
      ) : preview.isError ? (
        <ErrorNotice message={errorMessage(preview.error, "读取产出内容失败")} />
      ) : (
        <>
          {/* Markdown only for Markdown. Every `text` artifact used to go
              through the renderer, and a Task that produced a `.py` had it
              formatted as prose: indentation collapsed, `# 注释` promoted to a
              heading, `*args` eaten as emphasis. The code was still downloadable
              and the page was still calling it the deliverable, which is the
              worst combination -- the reader is looking straight at the thing
              and what they are looking at is wrong.

              A `<pre>` for everything else, matching what the Code console
              shows for the same bytes. That a Task cannot *run* its .py is a
              recorded trade (ADR-065 §4: no working set here); rendering it as
              a document was never a trade, just a default nobody had split. */}
          {previewKind(artifact.media_type) === "text" &&
          isMarkdown(artifact.media_type) ? (
            <MarkdownContent text={preview.data.text} />
          ) : (
            <pre className="aw-code-file-body">{preview.data.text}</pre>
          )}
          {preview.data.truncated ? (
            <p className="aw-page-note">内容较长，这里只显示开头；完整内容请下载。</p>
          ) : null}
        </>
      )}
    </section>
  );
}

/**
 * Why this run stopped, when it stopped badly -- and the retry, when the server
 * said the cause was retryable.
 *
 * Lifted out of the no-artifact branch because it belongs to the *run*, and the
 * run's outcome does not depend on whether a file came out of it. Rendered in
 * one branch only, it produced the page's most confident wrong impression: a
 * Word Task whose render succeeded and whose export was refused showed the
 * document, laid out, under 任务产出, with the failure visible only as a status
 * pill several hundred pixels up.
 *
 * `null` for every status that is not a bad ending, so both call sites can
 * render it unconditionally and neither has to re-derive when it applies.
 */
function RunFailure({
  onRetry,
  status,
  statusDetail,
}: {
  onRetry?: (() => void) | undefined;
  status: TaskStatus;
  statusDetail?: string;
}) {
  if (!isSettledStatus(status) || status === "succeeded") return null;
  // The server's detail is a stable sentence over a closed error vocabulary;
  // this says what it means, and offers the retry only when the server called
  // the cause retryable.
  const failure = explainFailure(statusDetail ?? null);
  return (
    <>
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
    </>
  );
}

/**
 * Whether these bytes were written to be read as Markdown.
 *
 * Asked by name rather than by `previewKind`, because the kind deliberately
 * answers a coarser question: `text/markdown` and `text/x-python` are both
 * `text` and get the same fetch, and that is right -- what differs is only how
 * the string is painted once it arrives.
 */
function isMarkdown(mediaType: string): boolean {
  const base = mediaType.split(";")[0]?.trim().toLowerCase() ?? "";
  return base === "text/markdown" || base === "text/x-markdown";
}

/**
 * What the text preview did not bring across, zeros left out.
 *
 * This replaces a sentence -- "不含排版、图片与页眉页脚；共 N 张表格" -- and the
 * sentence is why it exists. Prose can hold one number; the server reports
 * seven, and threading them into that clause produces a paragraph nobody
 * finishes reading. A list also survives the next count without being rewritten.
 *
 * Zeros are dropped rather than shown as 0. A document with no footnotes has
 * nothing missing on that axis, and a row saying so is noise competing with the
 * rows that mean something. The cost is that a count the server failed to send
 * would read as a zero and disappear, which is why the wire model requires
 * every one of them (`api/types.ts`).
 *
 * **The cut is one of the rows.** It has to be, because rendering nothing is
 * how this list says the preview is faithful, and a preview that stopped
 * partway through the document is not entitled to say that. The seven counts
 * cannot cover it: every one of them is of the whole document
 * (`adapters/documents/docx.py`), so a truncated preview reports the pictures
 * and the footnotes below the cut correctly and reports nothing whatever about
 * the prose that went with them -- and a document of plain paragraphs, cut in
 * half, scores zero on all seven. It leads the list rather than sorting into
 * it, and it is the only row without a number: what is missing is exactly the
 * part this preview did not read, which is why there is nothing to count.
 *
 * So an empty list now claims what it can carry: the text is whole and none of
 * the seven kinds was lost. Not that the document is fully represented --
 * endnotes, text boxes and tables nested inside cells go missing with no count
 * naming them, and that is a gap in the extraction rather than something this
 * list can close by staying quiet.
 *
 * Only under the text view. In 版面 the pictures and the running titles are on
 * screen, so this list would be describing losses the reader can see did not
 * happen.
 */
function PreviewGaps({ preview }: { preview: DocumentPreview }) {
  // Ordered by how invisible the loss is. A missing picture cannot be inferred
  // from the prose around it; a table that came through as plain rows is at
  // least visibly a table. Quantities carry their measure word, because "5" in
  // a column of counts says less than "5 段" does.
  const counted = [
    { label: "图片没有显示", count: preview.image_count, unit: "张" },
    { label: "脚注没有显示", count: preview.footnote_count, unit: "条" },
    { label: "页眉没有显示", count: preview.header_count, unit: "处" },
    { label: "页脚没有显示", count: preview.footer_count, unit: "处" },
    { label: "表格只保留文字", count: preview.table_count, unit: "张" },
    { label: "列表序号没有生成", count: preview.numbered_paragraph_count, unit: "段" },
    { label: "段落样式没有保留", count: preview.flattened_paragraph_count, unit: "段" },
  ].filter((gap) => gap.count > 0);
  if (!preview.truncated && counted.length === 0) return null;
  // The middle clause only when there are numbers under it to be read wrong.
  // They are of the whole file, which under a preview that stops early is the
  // difference between "four pictures" and "four pictures so far" -- and a
  // reader who takes the smaller reading concludes the rest is prose.
  const cutNote = [
    "正文只显示到这里，后面的内容没有进入预览。",
    counted.length === 0 ? "" : "下面的数字是按整份文档数的，不只是显示出来的这部分。",
    "完整内容请下载。",
  ].join("");
  return (
    <ul className="aw-preview-gaps" aria-label="预览没有还原的部分">
      {preview.truncated ? (
        <li className="aw-preview-gap-cut">{cutNote}</li>
      ) : null}
      {counted.map((gap) => (
        <li key={gap.label}>
          <span>{gap.label}</span>
          <strong>
            {gap.count} {gap.unit}
          </strong>
        </li>
      ))}
    </ul>
  );
}

/**
 * The one line explaining why there is no layout view.
 *
 * The server's own detail is not echoed, and for once that is not about leaking
 * internals: the reader of this panel cannot act on any of it. What they can
 * act on is which of two things is true -- this deployment cannot lay out any
 * document, or this document is the problem -- and each sentence ends by
 * pointing at what still works.
 */
/**
 * Why this panel has no layout to show -- the server's reasons, plus one of
 * its own.
 *
 * The server's vocabulary stays the server's: `getDocumentPdf` answers about a
 * deployment and a document, and `viewer_unavailable` is a fact about neither.
 * They meet here because the reader is owed one sentence rather than a taxonomy
 * -- what they see either way is the text view and a note saying why.
 */
type PanelLayoutDecline = DocumentLayoutDecline | "viewer_unavailable";

function layoutDeclineNote(reason: PanelLayoutDecline): string {
  if (reason === "converter_unavailable") {
    return "这套部署没有版面预览：服务器上没有可用的文档转换器。下面是文字预览，需要原样查看请下载。";
  }
  if (reason === "too_large") {
    return "这份文档的版面太大，页面里不展开。下面是文字预览，需要原样查看请下载。";
  }
  if (reason === "viewer_unavailable") {
    return "这个浏览器不显示内嵌 PDF，所以这里给不出版面（文档本身没问题）。下面是文字预览，要看排版请下载后用 Word 打开，或换一个浏览器打开控制台。";
  }
  return "这套部署给不出这份文档的版面。下面是文字预览，需要原样查看请下载。";
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
  return "系统默认";
}

function errorMessage(error: unknown, fallback: string): string {
  if (error instanceof Error && error.message.trim() !== "") return error.message;
  return fallback;
}

/**
 * The three views of the task list, and which statuses each one asks for.
 *
 * `在跑` is every status that has not settled -- including the two waiting
 * ones. A reader scanning for "what is still going" needs the Task parked for
 * migration and the one holding for an approval in that list: both are
 * unfinished and both may need them, and a "running-only" filter that hid
 * them would be a filter that loses work.
 *
 * `失败` deliberately excludes `cancelled`. A cancellation is somebody's
 * decision that was carried out correctly; filing it under failures would put
 * the reader's own action in the list of things that went wrong.
 */
const TASK_FILTERS = [
  { id: "all", label: "全部", statuses: undefined },
  {
    id: "running",
    label: "在跑",
    statuses: [
      "queued",
      "running",
      "waiting_approval",
      "waiting_migration",
    ] as const,
  },
  { id: "failed", label: "失败", statuses: ["failed", "dead_letter"] as const },
] as const;

type TaskFilterId = (typeof TASK_FILTERS)[number]["id"];

/**
 * How long a stage took, once both ends are known.
 *
 * Null while it is still running: a duration measured against "now" would tick
 * on every poll and read as a finished number that keeps changing. The stage's
 * own note already says 进行中 in that case.
 */
function stageDuration(stage: {
  startedAt: string | null;
  endedAt: string | null;
}): string | null {
  if (stage.startedAt === null || stage.endedAt === null) return null;
  const ms = Date.parse(stage.endedAt) - Date.parse(stage.startedAt);
  if (!Number.isFinite(ms) || ms < 0) return null;
  if (ms < 1000) return `${String(ms)} ms`;
  const seconds = Math.round(ms / 1000);
  if (seconds < 60) return `${String(seconds)} s`;
  const minutes = Math.floor(seconds / 60);
  const rest = seconds % 60;
  return rest === 0
    ? `${String(minutes)} 分`
    : `${String(minutes)} 分 ${String(rest)} 秒`;
}

function isSettledStatus(status: TaskStatus | undefined): boolean {
  return status !== undefined && SETTLED_STATUSES.has(status);
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
