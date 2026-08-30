import {
  useInfiniteQuery,
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import {
  AlertTriangle,
  ArrowUp,
  ChevronRight,
  ClipboardCheck,
  FileDown,
  LoaderCircle,
  PanelLeft,
  Paperclip,
  RefreshCw,
  Trash2,
  X,
} from "lucide-react";
import {
  type FormEvent,
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import {
  Link,
  useNavigate,
  useParams,
  useSearchParams,
} from "react-router-dom";
import {
  ApiError,
  cancelTask,
  deleteTask,
  createTask,
  decideApproval,
  downloadArtifact,
  getApproval,
  getArtifactJson,
  getTask,
  getTaskCapabilities,
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
  TokenUsage,
  TriageOption,
} from "../../api/types";
import { useIdentity } from "../../app/IdentityContext";
import {
  useWorkspaceSidebar,
  WorkspaceSidebarActions,
  WorkspaceSidebarPortal,
} from "../../app/WorkspaceSidebar";
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
  NewSessionAction,
  StatusPill,
  formatDateTime,
  formatStatus,
  formatTime,
  shortId,
} from "../../components/ui";
import { MarkdownContent } from "../../components/MarkdownContent";
import {
  ModeStarterPrompts,
  ModeStartHeader,
  submitTextareaOnEnter,
} from "../../components/ModeStart";
import {
  StepStream,
  type StreamRunLabel,
  type StreamStage,
} from "../../components/StepStream";
import { explainFailure } from "./failure";
import { deriveLifecycle, type Lifecycle, stageOfNode } from "./lifecycle";
import { useTaskTimeline } from "./useTaskTimeline";
import { errorMessage } from "../../components/ui";
import { workIdentityQueryKey } from "./workQueryKeys";
import { useDismissOnEscape } from "../../hooks/useDismissOnEscape";
import { ArtifactPreview } from "./ArtifactPreview";
import { formatBytes } from "./preview";
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
import { useStoredState } from "../../hooks/useStoredState";
import { type PartialTurnUsage } from "../../components/TurnUsage";
import {
  AgentEntryLine,
  AgentPanel,
  StreamNarrowNotice,
} from "./AgentPanel";
import { readDelegations, type DelegationFacts } from "./delegations";
import { DelegationScopeNote } from "./DelegationScope";
import {
  buildRunTree,
  flattenRuns,
  totalTokens,
  type RunNode,
  type RunStatus,
} from "../../components/runTree";

const CANCELLABLE_STATUSES = new Set<TaskStatus>([
  "queued",
  "running",
  "waiting_approval",
]);

const WORK_STARTERS = [
  {
    title: "梳理资料并给出结论",
    prompt: "整理所选资料，提炼关键结论、待确认事项和下一步建议。",
  },
  {
    title: "比较方案并生成报告",
    prompt: "比较几个可行方案的收益、成本和风险，并生成一份推荐报告。",
  },
  {
    title: "拆解并执行复杂任务",
    prompt: "把这个目标拆成可执行步骤，逐步完成并保留过程与最终产物。",
  },
] as const;
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
/**
 * 还没走完的状态：跑着的，和停下来等人的。
 *
 * 和 `SETTLED_STATUSES` 不是互补的两半，这一点是故意的：那一个回答的是
 * 「还要不要接着轮询」，而 waiting_migration 的答案是「不用，它停在那儿等人
 * 来搬」。这一个回答的是「这一行还需要读者惦记吗」，同一个状态的答案是「需要」。
 */
const UNFINISHED_STATUSES = new Set<TaskStatus>([
  "queued",
  "running",
  "waiting_approval",
  "waiting_migration",
]);

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
  const [searchParams, setSearchParams] = useSearchParams();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { identity } = useIdentity();
  const identityKey = workIdentityQueryKey(identity);
  const workspaceSidebar = useWorkspaceSidebar();

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

  const taskQueryKey = [
    "work",
    "task",
    ...identityKey,
    selectedTaskId,
  ] as const;
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

  // 这一台部署会不会让下一个任务派子代理，以及派得起几个。
  //
  // 键里没有 taskId，因为它答的不是任何一个任务：已经提交的任务跑在它自己那份
  // 冻结的快照上，把今天的进程配置摆在它旁边，等于报一个它从来没见过的数。所以
  // 它只出现在提交表单里。
  //
  // `staleTime: Infinity` 是因为这是进程启动时投影出来的常量——它在这个 API
  // 进程的生命周期里不会变，重投影要重启。轮询它是在问一个不会有新答案的问题。
  const capabilitiesQuery = useQuery({
    queryKey: ["work", "capabilities", ...identityKey],
    queryFn: () => getTaskCapabilities(identity),
    staleTime: Number.POSITIVE_INFINITY,
    // 读不到就当没有：这一段是提交表单里的一块说明，它加载失败不该把整个
    // 新建任务的表单变成一个错误页。渲染处按 undefined 处理。
    retry: 1,
  });
  const delegation = capabilitiesQuery.data?.delegation;

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

  // Which run the reader has singled out in the panel, or `null` for all of
  // them. Held here rather than in the panel because it narrows the stream
  // below it, and a selection that only the panel knew about could not.
  //
  // **In the URL rather than in `useState`,** which buys three things at once
  // and costs a line. It makes the narrowing shareable and survivable across a
  // reload -- the reason `/runs` and `timeline?run_id=` exist server-side is
  // exactly this deep link, and until now no client produced one. It also
  // scopes the selection to its Task for free: the task id is in the path, so
  // opening another Task builds a different URL and the query goes with it.
  // Held as state, the id travelled across and left the next Task filtered to
  // a run that is not in it -- every stage empty, the stream empty, and the one
  // control that undoes it gone with the panel, because a panel that renders
  // only where a delegation happened does not render on a Task that has not
  // delegated yet.
  //
  // `replace` rather than a new history entry: this is a filter, not a
  // destination. Pushing one per click would make the back button mean "undo
  // the last narrowing" for as many clicks as the reader made before it meant
  // "leave this Task", and the panel already carries an explicit 显示全部.
  const selectedRunId = searchParams.get("run");
  const setSelectedRunId = useCallback(
    (runId: string | null) => {
      setSearchParams(
        (held) => {
          const next = new URLSearchParams(held);
          if (runId === null) next.delete("run");
          else next.set("run", runId);
          return next;
        },
        { replace: true },
      );
    },
    [setSearchParams],
  );
  // The narrowing is applied *here*, once, rather than in each derivation
  // below: two filters would be two chances for the stage list and the task
  // events to disagree about which run is on screen.
  const shownEvents = useMemo(
    () =>
      selectedRunId === null
        ? timeline.events
        : timeline.events.filter((event) => event.run_id === selectedRunId),
    [timeline.events, selectedRunId],
  );
  // Events keyed by the lifecycle stage that owns them, so a stage in the
  // stream can show its own work instead of pointing at a separate log.
  const stageEvents = useMemo(() => {
    const byStage = new Map<string, EventEnvelope[]>();
    for (const event of shownEvents) {
      if (event.graph_node_id === null) continue;
      const id = stageOfNode(event.graph_node_id);
      const existing = byStage.get(id);
      if (existing === undefined) byStage.set(id, [event]);
      else existing.push(event);
    }
    return byStage;
  }, [shownEvents]);
  const taskEvents = useMemo(
    () => shownEvents.filter((event) => event.graph_node_id === null),
    [shownEvents],
  );
  // Read once per page rather than per row: every row would otherwise rescan
  // the whole timeline for the delegation that names its run.
  const delegations = useMemo(
    () => readDelegations(timeline.events),
    [timeline.events],
  );
  // Always over the *whole* timeline, never over the narrowed view: a panel
  // that lost its other rows the moment you picked one would take away the
  // only thing you could use to pick a different one.
  const runTree = useMemo(() => buildRunTree(timeline.events), [timeline.events]);
  const [agentsOpen, setAgentsOpen] = useStoredState("aw.work.agents-open", false);
  // 副面板展开的是哪一个子代理，和「步骤流被收窄到哪一个运行」是两件事，所以是
  // 两个状态。把它们并成一个（让打开详情顺手收窄步骤流）省一个 useState，代价是
  // 读者点开一个子代理想看看它，正文却在他没要求的情况下被换掉了——而且那一步
  // 没有一个明显的撤销入口，撤销藏在步骤流上方那行提示里。收窄仍然可以做，但它
  // 是详情里一个写着字的按钮，按下去才发生。
  const [openAgentRunId, setOpenAgentRunId] = useState<string | null>(null);
  const hasAgents = useMemo(
    () => flattenRuns(runTree).some((run) => run.parentRunId !== null),
    [runTree],
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
  const draftText = useMemo(
    () => findDraftText(timeline.events),
    [timeline.events],
  );
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
  const [reportOverride, setReportOverride] = useState<boolean | "auto">(
    "auto",
  );
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
  const [submissionKey, setSubmissionKey] = useState(() =>
    newIdempotencyKey("task"),
  );

  // Which task the page is showing *now*. Creating one is not a single round
  // trip: triage may think for up to `TRIAGE_TIMEOUT_MS` before the POST even
  // goes out, and the form is only rendered while no task is selected -- so a
  // continuation started from the start page can land after the reader has
  // opened something else.
  const shownTask = useRef(selectedTaskId);
  const detailPane = useRef<HTMLElement>(null);
  useLayoutEffect(() => {
    shownTask.current = selectedTaskId;
    // The pane survives route changes, so its old scroll offset survives too.
    // A task opened after reading another one's document otherwise lands in
    // the middle of the new preview and hides the execution process we now
    // lead with.
    if (detailPane.current !== null) detailPane.current.scrollTop = 0;
  }, [selectedTaskId]);

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
      // Only a reader who is still on the start page. Pressing 创建任务 asks
      // for a task; it does not ask to be moved off whatever was opened while
      // the request was out. The new task is at the top of the list either
      // way, so nothing is lost by leaving them where they are.
      if (shownTask.current === undefined) {
        void navigate(`/work/${encodeURIComponent(task.task_id)}`);
      }
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
      if (
        !window.confirm(
          "删除这个任务？它的执行记录会一起消失，产出文件不再可达。",
        )
      ) {
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
    opened !== null &&
    opened.taskId === selectedTaskId &&
    // 点到产物本身不开抽屉：它已经在阅读栏里了。开了的话同一份文件会同时活
    // 在两块面上，各有各的滚动位置、各自再取一次字节——而对读者来说它们看
    // 起来是同一个东西。
    //
    // 这条推理不是新的：旧代码在同一个条件下压掉过那颗「返回任务结果」，
    // 理由是「返回到你正在看的那个文件的按钮，是一颗什么也不做的按钮」。
    // 按钮没了，条件留下。
    opened.artifact.artifact_id !== deliverable?.artifact_id
      ? opened.artifact
      : null;
  const closePreview = useCallback(() => {
    setOpened(null);
  }, [setOpened]);
  useDismissOnEscape(openedArtifact !== null, closePreview);

  const [approvalNotice, setApprovalNotice] = useState<ApprovalNotice | null>(
    null,
  );
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
            ? "已经记下了。"
            : `你的“${formatStatus(intent.decision)}”没有生效，这条现在是“${formatStatus(approval.status)}”。`,
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
        //
        // Left set even when the reader has opened a task in the meantime:
        // the form that renders this question only exists on the start page,
        // so it waits there for them. That is a real gap -- they pressed
        // 创建任务, no task was created, and nothing where they now are says
        // so -- and closing it properly needs a notice surface this page does
        // not have. Recorded rather than papered over.
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
      setTriageError(errorMessage(error, "无法判定执行方式，请稍后重试。"));
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
    if (triaging || createMutation.isPending || pendingAsk !== null) return;
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
      !window.confirm("切换知识库会清空当前任务的待上传附件，是否继续？")
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
    <form
      aria-busy={createBusy}
      className="aw-create-task"
      id="aw-create-task-form"
      onSubmit={handleCreate}
    >
      <div className="aw-create-task-head">
        <ModeStartHeader
          action={
            <IconButton
              className="aw-work-mobile-back"
              controls="workspace-sidebar-context"
              expanded={workspaceSidebar.drawerOpen}
              label="打开任务列表"
              onClick={workspaceSidebar.open}
            >
              <PanelLeft aria-hidden="true" size={18} />
            </IconButton>
          }
          description="描述结果，Agent 会选择合适的执行方式并持续保存进度。"
          title="想完成什么？"
        />
      </div>
      <label className="aw-sr-only" htmlFor="work-objective">
        目标
      </label>
      {/* 一张卡，和对话页的输入框是同一个形状。此前这里是四个平铺的兄弟节点
          （输入框、知识库选择、附件行、提交键），提交键还被 `高级设置` 和几段
          条件文案隔在最下面——「提交这件事」在版面上散成了四块。 */}
      <div className="aw-create-task-card aw-mode-composer-card">
        <textarea
          aria-keyshortcuts="Enter"
          enterKeyHint="send"
          id="work-objective"
          disabled={createBusy}
          maxLength={4096}
          onChange={(event) => {
            setObjective(event.target.value);
            markTaskIntentEdited();
          }}
          onKeyDown={submitTextareaOnEnter}
          placeholder="例如：整理项目资料，比较三个方案并输出建议报告"
          required
          rows={4}
          value={objective}
        />
        <AttachmentTray
          items={attachments.items}
          onRemove={attachments.remove}
          onRetry={attachments.retry}
        />
        <div className="aw-create-task-bar">
          <AttachmentButton
            disabled={createBusy || attachments.readOnlyReason !== null}
            {...(attachments.readOnlyReason === null
              ? {}
              : { disabledReason: attachments.readOnlyReason })}
            onFiles={attachments.addFiles}
          />
          <KnowledgeSourcePicker
            compact
            disabled={createBusy}
            identity={identity}
            onChange={(knowledgeBase) =>
              changeKnowledgeBase(knowledgeBase?.knowledge_base_id ?? null)
            }
            value={knowledgeBaseId}
          />
          <span className="aw-create-task-spacer" />
          <button
            aria-label="创建任务"
            className="aw-button is-primary aw-mode-send"
            disabled={
              createBusy ||
              pendingAsk !== null ||
              sourceResolving ||
              objective.trim() === "" ||
              attachments.hasBlockingItems
            }
            type="submit"
          >
            {createBusy ? (
              <LoaderCircle aria-hidden className="aw-spin" size={17} />
            ) : (
              <ArrowUp aria-hidden size={17} />
            )}
          </button>
        </div>
      </div>
      <ModeStarterPrompts
        disabled={createBusy}
        items={WORK_STARTERS}
        label="任务起点"
        onChoose={(prompt) => {
          setObjective(prompt);
          markTaskIntentEdited();
          window.requestAnimationFrame(() =>
            document.getElementById("work-objective")?.focus(),
          );
        }}
      />
      {attachments.readOnlyReason === null ? null : (
        <p className="aw-create-task-hint">
          {attachments.readOnlyReason}
        </p>
      )}
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
        <DelegationScopeNote delegation={delegation} />
      </details>
      {attachments.hasBlockingItems ? (
        <p className="aw-create-task-hint">
          附件正在上传或索引，完成后才能创建任务。
        </p>
      ) : null}
      {pendingAsk !== null ? (
        <div
          aria-label="选择执行方式"
          aria-live="polite"
          className="aw-triage-ask"
          role="group"
        >
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
      {createMutation.isError ? (
        <ErrorNotice
          message={errorMessage(createMutation.error, "创建任务失败")}
        />
      ) : null}
    </form>
  );

  return (
    <div
      className={`aw-work-page ${selectedTaskId === undefined ? "" : "has-selection"}`}
    >
      <WorkspaceSidebarActions>
        <button
          // 不叫「刷新任务列表」：那个名字里整整齐齐含着「新任务」，
          // 而「新任务」是它旁边的另一个按钮。两个控件的无障碍名互为子串，
          // 对按名字找控件的人和工具都是一次歧义。
          aria-label="重新加载列表"
          className="aw-side-action"
          disabled={tasksQuery.isFetching}
          onClick={() => void tasksQuery.refetch()}
          title="重新加载列表"
          type="button"
        >
          <RefreshCw aria-hidden="true" size={15} />
        </button>
        <NewSessionAction
          label="新任务"
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
        />
      </WorkspaceSidebarActions>
      <WorkspaceSidebarPortal>
        <aside className="aw-work-sidebar" aria-label="任务列表与新建任务">
          <IconButton
          className="aw-work-sessions-close"
          label="关闭任务列表"
          onClick={workspaceSidebar.close}
          >
          <X aria-hidden="true" size={17} />
          </IconButton>

          <nav className="aw-task-list" aria-label="任务">
            {tasksQuery.isPending ? <LoadingLine label="正在加载任务" /> : null}
            {tasksQuery.isError ? (
              <ErrorNotice
                message={errorMessage(tasksQuery.error, "加载任务列表失败")}
              />
            ) : null}
            {tasks.map((task) => (
              <div className="aw-task-list-row" key={task.task_id}>
                <Link
                  aria-current={
                    task.task_id === selectedTaskId ? "page" : undefined
                  }
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
                    <small>{formatDateTime(task.created_at)}</small>
                  </span>
                  {/* 只有还没走完的任务带这颗点。结束了的（成功、失败、取消）
                      不带——见文件末尾那段注释：一条最近列表不需要用红色把
                      每一次失败再说一遍，而「还没完」是扫一眼看不出来的。

                      问的是 `UNFINISHED_STATUSES` 而不是 `isSettledStatus`：
                      后者把 waiting_migration 算作已结束，因为轮询它没有意义
                      （它停在那儿等人搬）——但那恰恰是最该带点的一行。 */}
                  {UNFINISHED_STATUSES.has(task.status) ? (
                    <span
                      aria-label={`状态：${formatStatus(task.status)}`}
                      className="aw-task-status-dot"
                      data-status={task.status}
                      role="img"
                      title={formatStatus(task.status)}
                    />
                  ) : null}
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
            {!tasksQuery.isPending &&
            !tasksQuery.isError &&
            tasks.length === 0 ? (
              <p className="aw-muted">
                还没有任务。说一件要做的事，就能开一个。
              </p>
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

      <main className="aw-work-detail" ref={detailPane}>
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
          <section className="aw-work-start aw-mode-start">
            {createTaskForm}
          </section>
        ) : null}
        {selectedTaskId !== undefined && taskQuery.isPending ? (
          <LoadingLine label="正在加载任务详情" />
        ) : null}
        {selectedTaskId !== undefined && taskQuery.isError ? (
          <ErrorNotice
            message={errorMessage(taskQuery.error, "加载任务详情失败")}
          />
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
                  {selectedTask.objective_preview ??
                    shortId(selectedTask.task_id, 28)}
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

            {/* The live process comes first while the task is unfolding. A
                large document preview used to push the only explanation of
                what the agent did several screens below the fold. */}
            <div
              className={`aw-work-body ${hasOutputRail ? "has-output" : ""}`}
            >
              <div className="aw-work-run">
                <div
                  className={`aw-work-process${
                    isSettledStatus(selectedTask.status) ? "" : " is-live"
                  }`}
                >
                  <header className="aw-work-process-header">
                    <div>
                      <span aria-hidden="true" className="aw-work-process-pulse" />
                      <strong>执行过程</strong>
                    </div>
                    <small>
                      {isSettledStatus(selectedTask.status)
                        ? "完整记录"
                        : "按任务事件持续同步"}
                    </small>
                  </header>
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
                    delegations={delegations}
                    onSelectRun={setSelectedRunId}
                    agentsOpen={agentsOpen}
                    onOpenAgents={() => {
                      setAgentsOpen(true);
                    }}
                    runTree={runTree}
                    streamIncomplete={timeline.skippedSequences.length > 0}
                    selectedRunId={selectedRunId}
                    stageEvents={stageEvents}
                    status={selectedTask.status}
                    taskEvents={taskEvents}
                  />
                  <TimelineGapNotice gaps={timelineGaps} />
                  {timeline.error !== null ? (
                    <ErrorNotice
                      message={errorMessage(timeline.error, "读取执行过程失败")}
                    />
                  ) : null}
                </div>

                <TaskResult
                  // 永远是这次任务自己的产物。从文件栏点开的那个现在长在
                  // 右侧抽屉里，不再顶掉这一栏。
                  artifact={deliverable}
                  draftText={draftText}
                  identity={identity}
                  onDownload={(artifact) => downloadMutation.mutate(artifact)}
                  onRetry={
                    retryInput === null ? undefined : () => resubmit(retryInput)
                  }
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
                !(
                  approvalMutation.error instanceof ApiError &&
                  approvalMutation.error.status === 409
                ) ? (
                  <ErrorNotice
                    message={errorMessage(
                      approvalMutation.error,
                      "提交审批决定失败",
                    )}
                  />
                ) : null}

                <details className="aw-work-fold">
                  <summary>
                    <ChevronRight
                      aria-hidden="true"
                      className="aw-step-caret"
                      size={14}
                    />
                    任务详情
                  </summary>
                  <div className="aw-work-fold-body">
                    {taskInputQuery.isPending && taskInputRef !== null ? (
                      <LoadingLine label="正在读取任务输入" />
                    ) : null}
                    {taskInputQuery.isError ? (
                      <ErrorNotice
                        message={errorMessage(
                          taskInputQuery.error,
                          "读取任务输入失败",
                        )}
                      />
                    ) : null}
                    {taskInputQuery.data === undefined ? null : (
                      <p className="aw-task-objective-full">
                        {taskInputQuery.data.objective}
                      </p>
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
                            value={
                              taskInputQuery.data.knowledge_base_id ?? "未使用"
                            }
                          />
                        </>
                      )}
                      {retryGraph !== null ? (
                        <KeyValue
                          label="执行方式"
                          value={
                            retryGraph === "research" ? "调研报告" : "通用执行"
                          }
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
                      <KeyValue
                        label="创建时间"
                        value={formatDateTime(selectedTask.created_at)}
                      />
                      <KeyValue
                        label="更新时间"
                        value={formatDateTime(selectedTask.updated_at)}
                      />
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
                            disabled={
                              cancelMutation.isPending ||
                              cancelReason.trim() === ""
                            }
                            onClick={() =>
                              cancelMutation.mutate({
                                taskId: selectedTask.task_id,
                                reason: cancelReason.trim(),
                              })
                            }
                            type="button"
                          >
                            {cancelMutation.isPending
                              ? "正在取消…"
                              : "取消任务"}
                          </button>
                        </div>
                        {cancelMutation.isError ? (
                          <ErrorNotice
                            message={errorMessage(
                              cancelMutation.error,
                              "取消任务失败",
                            )}
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

      {selectedTask !== undefined && agentsOpen && hasAgents ? (
        <AgentPanel
          events={timeline.events}
          onClose={() => {
            setAgentsOpen(false);
            // 收起时把详情也退回集合：下次打开应该落在「谁怎么样了」，而不是
            // 上一次碰巧点进去的那一个。
            setOpenAgentRunId(null);
          }}
          onInspect={(runId) => {
            setSelectedRunId(runId);
          }}
          onOpen={setOpenAgentRunId}
          openRunId={openAgentRunId}
          roots={runTree}
        />
      ) : null}

      {/* 产出文件栏里点开的文件长在右侧抽屉里，而不是把阅读栏换掉。
          此前点一个文件就把「这次任务怎么样」整段顶走——而那一段正是读者
          用来判断这个文件值不值得看的东西。抽屉之后两边同时在。
          放在 `.aw-work-detail` 外面、`.aw-work-page` 里面：前者有
          container-type: inline-size，那等于 layout containment，窄屏那份
          position: fixed 会被它锚住。 */}
      {openedArtifact === null ? null : (
        <>
          <button
            aria-label="关闭预览"
            className="aw-drawer-backdrop"
            onClick={closePreview}
            type="button"
          />
          <aside aria-label="预览" className="aw-drawer">
            <header className="aw-drawer-header">
              <h2>{artifactName(openedArtifact)}</h2>
              <div className="aw-drawer-actions">
                <button
                  className="aw-button"
                  onClick={() => downloadMutation.mutate(openedArtifact)}
                  type="button"
                >
                  下载
                </button>
                <button
                  className="aw-button"
                  onClick={closePreview}
                  type="button"
                >
                  关闭
                </button>
              </div>
            </header>
            <section
              aria-label={`文件 ${artifactName(openedArtifact)}`}
              className="aw-drawer-body"
            >
              <ArtifactPreview artifact={openedArtifact} identity={identity} />
            </section>
          </aside>
        </>
      )}
    </div>
  );
}

/** 产出的显示名。没有文件名的产出用它的 kind——不拿 id 编一个。 */

/**
 * 这一段烧了多少 token。
 *
 * 加的是 `ModelCompleted.usage`，一次模型调用一条，一段里可能有好几条。**不加**
 * `RunCompleted.usage`：那一条是整个运行的累计，和这一段的几次调用重叠，两个都
 * 算等于把这一段之前的每一步再算一遍。
 *
 * 返回 `null` 而不是零：一段没有模型调用（`route` 常常就是）和一段调用了但没花
 * token，在屏幕上是两回事，而后者不存在。
 *
 * 钱是 `null`，永远。费用随终止事件按**运行**一次写下，一段步骤加不出它——要在
 * 读的时候重新定价才行，而重新定价的总额会随配置追溯变化，那正是用量那条路一直
 * 拒绝走的。所以这里显示 token、不显示钱。
 */
function stageUsage(events: readonly EventEnvelope[]): PartialTurnUsage | null {
  let input = 0;
  let output = 0;
  let cacheRead = 0;
  let cacheWrite = 0;
  let seen = false;
  for (const event of events) {
    if (event.event_type !== "ModelCompleted") continue;
    const payload = event.payload as { usage?: TokenUsage };
    const usage = payload.usage;
    if (usage === undefined) continue;
    seen = true;
    input += usage.input_tokens ?? 0;
    output += usage.output_tokens ?? 0;
    cacheRead += usage.cache_read_tokens ?? 0;
    cacheWrite += usage.cache_write_tokens ?? 0;
  }
  if (!seen) return null;
  return {
    input_tokens: input,
    output_tokens: output,
    cache_read_tokens: cacheRead,
    cache_write_tokens: cacheWrite,
    cost_micro_usd: null,
  };
}

function artifactName(artifact: ArtifactRef): string {
  return artifact.filename ?? artifact.kind;
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
/**
 * 运行的状态，到时间线上那一块该长什么样。
 *
 * 写成表而不是三元链，理由和 `runTree.ts` 里那两张表一样：`RunStatus` 加一个取值
 * 时，这里会因为缺 key 而红，而不是悄悄落到某个 else 分支上。`cancelled` 和
 * `failed` 归一档——对读这条流的人来说它们要做的下一件事是同一件：那段没跑完。
 */
const RUN_SECTION_OUTCOME: Readonly<
  Record<RunStatus, StreamRunLabel["outcome"]>
> = {
  running: "running",
  completed: "done",
  failed: "failed",
  cancelled: "failed",
  unknown: "unknown",
};

/**
 * 一个子代理块右侧那行小字：它烧掉了多少。
 *
 * 和 `RunPanel` 的 `formatTokens` 同形而不共用，理由与 `DelegationScope` 那一处
 * 相同：那一份格式化的是面板上会随轮询跳动的数，这一份是折叠行上的一个注脚，两者
 * 不为同一件事负责。合并会让改其中一个的人以为自己只改了一个。
 */
function formatSpentTokens(value: number): string {
  if (value < 1000) return String(value);
  return `${(value / 1000).toFixed(1)}k`;
}

function TaskStepStream({
  lifecycle,
  loading,
  onOpenArtifact,
  status,
  stageEvents,
  taskEvents,
  delegations,
  runTree,
  selectedRunId,
  onSelectRun,
  agentsOpen,
  onOpenAgents,
  streamIncomplete,
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
  // Which runs in this stream were started by another one (ADR-082). A
  // delegated run's events land on its parent's node, so they arrive in the
  // right stage already -- what they lack is any sign that a different agent
  // produced them.
  delegations: ReadonlyMap<string, DelegationFacts>;
  //: Every run this Task holds, nested under whoever started it.
  runTree: RunNode[];
  // Whether this page was told it did not receive part of the stream. The
  // panel needs it for the same reason `TimelineGapNotice` below does, and for
  // a sharper one: a hole in the steps is visible as a hole, while a hole that
  // swallowed an `AgentDelegated` removes a branch from the tree with nothing
  // left behind to look wrong.
  streamIncomplete: boolean;
  selectedRunId: string | null;
  onSelectRun: (runId: string | null) => void;
  agentsOpen: boolean;
  onOpenAgents: () => void;
}) {
  if (loading) return <LoadingLine label="正在读取执行过程" />;

  const stages: StreamStage[] = lifecycle.stages.map((stage) => {
    const events = stageEvents.get(stage.id) ?? [];
    const usage = stageUsage(events);
    return {
      id: stage.id,
      title: stage.title,
      state: stage.state,
      // 一段可能对应两个节点（plan · route、approval · export），所以是连起来的
      // 一串而不是一个。表里没有的节点不画：那种情况下标题本身就是节点 id。
      ...(stage.nodes.length === 0 ? {} : { nodes: stage.nodes.join(" · ") }),
      ...(stageDuration(stage) === null
        ? {}
        : { duration: stageDuration(stage) as string }),
      ...(usage === null ? {} : { usage }),
      // 阶段的状态读的是**整条流**（`deriveLifecycle(timeline.events, …)`），
      // 它下面的步骤读的是收窄之后的那份。两者本该如此——阶段是**任务**的骨架，
      // 不因为读者在看其中一个运行就该被改写；按运行重算阶段，等于让"这个任务
      // 走到哪了"随一次点击变来变去。
      //
      // 但收窄开着时，一个写着「已完成 12:03」却一条步骤都没有的阶段，说出来的
      // 是"这一段什么也没做"，而真相是"这一段做的事不属于你选的那个运行"。
      // `note` 正是这句话该待的地方：它本来就是右侧那行"现在是什么状态"的文字，
      // 所以这条改动**不动 `StepStream`**，也就不波及同样用它的 Chat 与 Code。
      note:
        selectedRunId !== null && events.length === 0
          ? "不含所选运行"
          : stage.state === "skipped"
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
      events,
    };
  });

  /**
   * 一个不属于当前阶段的运行，读者该怎么称呼它。
   *
   * 只有 Work 答得出这个问题：它要读 `AgentDelegated` 才知道某个 run_id 是一次
   * 委派、被派出去的是谁，而 Chat 的一轮里连这个事件都不会有。所以这个函数在这里
   * 而不在 `StepStream` 里——切段是机械的，命名不是。
   *
   * 认不出来就返回 `null`，由组件用 `运行 xxxxxxxx` 兜底。这一半不能省：一页没送到
   * 的 `AgentDelegated` 会让 `readDelegations` 说不出这个子运行是谁，而那时候把它的
   * 事件画成父运行干的，是这里唯一错的答案——那正是之前「子代理 X：」前缀在缺页时
   * 会静默退化成的样子。
   */
  // 按 run_id 查节点。`runTree` 是根的数组，而这里要问的是某一个 run_id 的状态与
  // 花费——摊平一次比在渲染里递归找便宜，也比让 `runTree.ts` 再导出一张表诚实：
  // 那棵树的形状才是它的产物，索引是使用方的事。
  const allRuns = flattenRuns(runTree);
  const runsById = new Map(allRuns.map((node) => [node.runId, node] as const));

  // 同一个图节点跑了第几次。
  //
  // 只数**根**运行：被委派出去的那些有自己的名字，而且它们的 `nodeId` 沿用父运行的
  // ——把它们一起数会让「第 2 次运行」这句话指到一个子代理身上。按 `firstSequence`
  // 排序而不是按 Map 的插入序：后者是事件到达的顺序，而重放一页会改变它。
  const attemptByRun = new Map<string, number>();
  const rootsByNode = new Map<string, RunNode[]>();
  for (const node of allRuns) {
    if (node.parentRunId !== null || node.nodeId === null) continue;
    const held = rootsByNode.get(node.nodeId);
    if (held === undefined) rootsByNode.set(node.nodeId, [node]);
    else held.push(node);
  }
  for (const runs of rootsByNode.values()) {
    if (runs.length < 2) continue;
    [...runs]
      .sort((a, b) => (a.firstSequence ?? 0) - (b.firstSequence ?? 0))
      .forEach((node, index) => attemptByRun.set(node.runId, index + 1));
  }

  const runLabel = (runId: string): StreamRunLabel | null => {
    const node = runsById.get(runId);
    const spent = node === undefined ? 0 : totalTokens(node.spend);
    const outcome = RUN_SECTION_OUTCOME[node?.status ?? "unknown"];
    const note = spent > 0 ? { note: formatSpentTokens(spent) } : {};

    const facts = delegations.get(runId);
    if (facts !== undefined) {
      return {
        title: facts.definitionName ?? `运行 ${shortId(runId, 8)}`,
        // 只有这一支敢说「子代理」：它读到了那条 `AgentDelegated`。
        badge: "子代理",
        outcome,
        ...note,
      };
    }

    // 不是委派，但页面认得它：同一个图节点的第二、第三次运行。实测
    // `task_75cd1e0c` 的 `review` 节点就跑了两次（20 条 + 4 条事件），此前它们在
    // 时间线上是一列 24 行，没有任何东西说过这是两回。它不是任何人的子代理，
    // 所以不挂徽标——装框只说「这一段来自另一次运行」这一件事。
    const attempt = attemptByRun.get(runId);
    if (attempt !== undefined) {
      return { title: `第 ${String(attempt)} 次运行`, outcome, ...note };
    }

    return null;
  };

  return (
    <>
      <AgentEntryLine
        incomplete={streamIncomplete}
        onOpen={onOpenAgents}
        open={agentsOpen}
        roots={runTree}
      />
      <StreamNarrowNotice
        narrowedToMissingRun={
          selectedRunId !== null &&
          !allRuns.some((run) => run.runId === selectedRunId)
        }
        onClear={() => {
          onSelectRun(null);
        }}
        selectedRunId={selectedRunId}
      />
      <StepStream
        ariaLabel="执行过程"
        eventTitle={eventTitle}
        isKnownEvent={(event) => isKnownEventType(event.event_type)}
        meta={{ title: "运行记录", events: taskEvents }}
        onOpenArtifact={onOpenArtifact}
        running={!isSettledStatus(status)}
        runLabel={runLabel}
        stages={stages}
      />
    </>
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
        <strong>这段历史不完整：上面的步骤中缺了 {gaps.length} 个位置。</strong>
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
  if (gap.before !== null)
    return `${position}：在「${eventTitle(gap.before)}」之后`;
  if (gap.after !== null)
    return `${position}：在「${eventTitle(gap.after)}」之前`;
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
function ArtifactRail({
  artifacts,
  onOpen,
  workspaceWrites,
}: {
  artifacts: TaskArtifact[];
  onOpen: (artifact: ArtifactRef) => void;
  /**
   * Files in the Task's working set.
   *
   * Openable since ADR-088, through the same `onOpen` the artifacts above use
   * and the same `/v1/artifacts/{id}` behind it -- a working-set entry is an
   * artifact, so there is one viewer and one authorization check, not two. A
   * row whose reference did not arrive is still listed and still inert.
   */
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
                  {graphNodeId === null
                    ? "任务"
                    : workflowStageTitle(graphNodeId)}{" "}
                  · {formatTime(producedAt)} ·{" "}
                  {formatBytes(artifact.size_bytes)}
                </small>
              </button>
            </li>
          ))}
        </ul>
      )}

      {/* The second group. These names come from
          `ToolCompleted.workspace_writes`, which ADR-063 publishes
          unconditionally -- outside the `record_step_inputs` gate, so they
          survive a default deployment -- and which this page read none of
          before: a Task that rendered three files into its working set showed
          the reader nothing, not even a count.

          **They open now (ADR-088), and the reason they could not is worth
          keeping.** The old note here said opening one would need a Task
          workspace read surface, which would be a second authorization surface
          and a second addressing scheme. That was true of a route serving
          *bytes by name* -- and it stays true, which is why no such route
          exists. But a working-set entry is stored *as an artifact*
          (`WorkspaceManifest` binds each name to an `ArtifactRef`), and
          `/v1/artifacts/{id}` already serves those bytes under the owner check
          the write recorded. Nothing was missing except the binding, and the
          write now publishes it.

          A name still opens only when its reference arrived. Events written
          before ADR-088 carry none, so those rows stay exactly as they were --
          listed, not openable -- rather than becoming buttons that 404. */}
      {workspaceWrites.length === 0 ? null : (
        <div className="aw-artifacts-workspace">
          <h3>任务工作区里的文件</h3>
          <p className="aw-artifacts-note">
            这些文件在任务自己的工作区里，不是导出的产物。带链接的可以直接打开。
          </p>
          {workspaceWrites.map((group) => (
            <div key={group.graphNodeId ?? "任务"}>
              <h4>
                {group.graphNodeId === null
                  ? "任务"
                  : workflowStageTitle(group.graphNodeId)}
              </h4>
              <ul className="aw-artifacts-names">
                {group.names.map((name) => {
                  const ref = group.refs.get(name);
                  return (
                    <li key={name}>
                      {ref === undefined ? (
                        // No reference, so no claim that it can be opened. This
                        // is what every row looked like before ADR-088, and it
                        // is what a Task that ran under an older Worker still
                        // looks like.
                        name
                      ) : (
                        <button
                          className="aw-artifacts-name-button"
                          onClick={() => {
                            onOpen(ref);
                          }}
                          type="button"
                        >
                          {name}
                        </button>
                      )}
                    </li>
                  );
                })}
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
  onDownload,
  onRetry,
  status,
  statusDetail,
  wantsReport,
}: {
  artifact: ArtifactRef | null;
  draftText: string | null;
  identity: PrincipalIdentity;
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
  // 「这份产出长什么样」整段搬去了 `ArtifactPreview`：那一段只读 artifact 和
  // identity，而这个组件其余部分说的是「这次任务怎么样」——运行失败、复核
  // 提醒、下载与返回。两件事挤在一起的结果是它们只能出现在同一个地方，于是
  // 从产出文件栏点开一个文件就得把整个阅读栏换掉。

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
                它是按这套部署跑不了的执行版本提交的，已经停在这里：等下去不会有进展，重新提交同一个任务也不会，需要管理员先处理版本迁移。
              </small>
              {/* The server's own sentence, verbatim. It is English and it is
                  written for whoever has to act on it -- which is not the
                  reader of this page, but is the person they will quote it
                  to. */}
              {statusDetail === undefined ? null : (
                <small>{statusDetail}</small>
              )}
            </span>
          </div>
        </section>
      );
    }
    // The text the approval gate is a gate over. Held back until the reader
    // decided, it is exactly what they are being asked about, so this is the
    // one non-terminal status that has something to show.
    const awaitingDecision =
      status === "waiting_approval" && draftText !== null;
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
            <span className="aw-answer-mark" aria-hidden="true">
              A
            </span>
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
            <span>
              任务已完成，没有产出内容。展开上面的执行过程可以看到每一步做了什么。
            </span>
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
              <span className="aw-answer-mark" aria-hidden="true">
                A
              </span>
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
        <span className="aw-answer-mark" aria-hidden="true">
          A
        </span>
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
        <span className="aw-answer-size">
          {formatBytes(artifact.size_bytes)}
        </span>
        {/* 这里此前有一颗「返回任务结果」：从文件栏点开一个文件会把阅读栏
            换掉，那颗按钮是唯一的回头路，没有它读者会被困在一份证据包里。
            现在阅读栏根本不会离开——文件长在右侧抽屉里——所以那条回头路
            由结构本身给出，不再需要一个按钮。 */}
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
      <ArtifactPreview artifact={artifact} identity={identity} />
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
          <button
            className="aw-button is-primary"
            onClick={onRetry}
            type="button"
          >
            用同样的目标再试一次
          </button>
        </div>
      ) : null}
    </>
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
  const decidable =
    approval?.status === "pending" && taskStatus === "waiting_approval";

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
        <ErrorNotice message="这条确认对不上当前任务，先刷新页面。" />
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

/* 侧栏此前在列表上方还有一条「全部 / 在跑 / 失败」筛选，它连着一个服务端
 * status 过滤。删掉的理由不是它不好用，是它回答的问题在这条侧栏里问不出来：
 * 这一栏是「我最近在做的几件事」，一次列 25 条，而按状态筛一份 25 条的列表，
 * 眼睛比按钮快。代价是「失败」这一类不再能一键聚起来——真要按状态找，那属于
 * 一个任务检索界面，不属于导航栏里的最近列表。
 *
 * 同时删掉的还有已结束任务行首那颗状态点。它此前用形状+颜色说四种状态，
 * 而结束了的任务里，那颗点唯一在说的是「这件事失败了」——一条列表用红色
 * 复述每一次失败，就是在让人反复读自己最不想读的那一行。原因没有丢：点开
 * 任务，详情页第一句仍然写着它为什么停下来。
 *
 * 没结束的那两类（在跑 / 等你）留着，见 `aw-task-status-dot` 那段注释：
 * 它们说的不是好坏，是「这件事还没完，可能在等你」，而那是一条最近列表
 * 唯一没法用眼睛扫出来的信息。
 */

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
  if (normalized.includes("draft") || normalized.includes("write"))
    return "撰写结果";
  if (normalized.includes("critic") || normalized.includes("review"))
    return "检查与修订";
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
    return `任务已经是“${formatStatus(task.status)}”了，这个确认不用做了。`;
  }
  if (approval !== undefined && approval.status !== "pending") {
    return `这条确认已经是“${formatStatus(approval.status)}”了，不用再做一次。`;
  }
  return "这个决定没有生效：期间有人改过它。已经取回最新状态，请再确认一次。";
}
