import type {
  ArtifactRef,
  EventEnvelope,
  TaskGraphChoice,
  TaskIntent,
  TaskTimelineResponse,
} from "../../api/types";
import { checkCost } from "../../components/media";

export interface TaskInputArtifact {
  schema_version: number;
  objective: string;
  max_revisions: number;
  knowledge_base_id: string | null;
  // Absent on Tasks submitted before the field existed. Those ran under a graph
  // that always exported, so `true` is what re-submitting one has to mean.
  wants_report: boolean;
}

export interface TimelineState {
  taskId: string;
  events: EventEnvelope[];
  cursor: string | null;
  /**
   * Every position the server said it examined and could not deliver, oldest
   * first, across all the pages this state holds.
   *
   * Kept beside `events` and not derived from them: a hole is invisible in the
   * events, which is the whole reason the server names it.
   */
  skippedSequences: number[];
}

/** One undeliverable position, placed among the events that did arrive. */
export interface TimelineGap {
  sequence: number;
  /** The held event immediately before the hole, or null when none precedes it. */
  before: EventEnvelope | null;
  /** The held event immediately after the hole, or null when none follows it. */
  after: EventEnvelope | null;
}

export interface FinalReportMatch {
  artifact: ArtifactRef;
  toolCallId: string;
  completedEventId: string;
  succeededEventId: string;
}

const KNOWN_EVENT_TITLES: Readonly<Record<string, string>> = {
  TaskSubmitted: "任务已提交",
  TaskClaimed: "任务已领取",
  TaskRetryScheduled: "任务已安排重试",
  TaskDeadLettered: "任务进入死信状态",
  TaskAwaitingApproval: "任务等待批准",
  TaskApprovalRequested: "审批记录已创建",
  TaskApprovalDecided: "审批决定已记录",
  TaskSucceeded: "任务成功完成",
  TaskFailed: "任务执行失败",
  TaskCancelled: "任务已取消",
  TaskParkedForMigration: "任务等待迁移",
  RunStarted: "运行已开始",
  RunPaused: "运行已暂停",
  RunCompleted: "运行已完成",
  RunFailed: "运行失败",
  RunCancelled: "运行已取消",
  ContextBuilt: "上下文已构建",
  ModelStarted: "模型调用已开始",
  ModelCompleted: "模型调用已完成",
  ToolProposed: "工具调用已提出",
  PermissionRequested: "权限检查已请求",
  PermissionResolved: "权限检查已完成",
  ToolApprovalDecided: "工具审批已裁定",
  ToolStarted: "工具调用已开始",
  ToolProgress: "工具调用有新进展",
  ToolCompleted: "工具调用已完成",
  ToolFailed: "工具调用失败",
  ContextCompacted: "上下文已压缩",
  AgentDelegated: "子代理已委派",
  AgentCompleted: "子代理已完成",
};

export function createTimelineState(taskId: string): TimelineState {
  return { taskId, events: [], cursor: null, skippedSequences: [] };
}

/** Merge one oldest-first page without trusting a replay boundary to be unique. */
export function mergeTimelineResponse(
  state: TimelineState,
  response: TaskTimelineResponse,
): TimelineState {
  if (response.task_id !== state.taskId) return state;

  const seen = new Set(state.events.map((event) => event.event_id));
  const additions = response.events.filter((event) => {
    if (seen.has(event.event_id)) return false;
    seen.add(event.event_id);
    return true;
  });

  return {
    taskId: state.taskId,
    events: additions.length === 0 ? state.events : [...state.events, ...additions],
    cursor: response.cursor,
    skippedSequences: mergeSkippedSequences(
      state.skippedSequences,
      response.skipped_sequences,
    ),
  };
}

/**
 * The union of what every page said it could not deliver, oldest first.
 *
 * Union rather than replace, because each response speaks only for the page it
 * examined: this state holds the whole history a reader is looking at, so it
 * has to hold the whole history's holes with it. Deduplicated because an
 * overlapping re-read reports the same position a second time -- which is the
 * reason the server sends positions instead of a count, since a re-sent count
 * is indistinguishable from fresh damage.
 *
 * Sorted ascending so a hole reads in the same order as the events it sits
 * between, and filtered to real positions: sequences start at 1, and a client
 * that repeated a malformed number back to the user would be inventing a hole
 * rather than reporting one.
 *
 * The array's identity survives a page that adds nothing, so the callers that
 * memoise on it are not re-run by every poll.
 */
function mergeSkippedSequences(
  held: number[],
  reported: readonly number[] | undefined,
): number[] {
  // `?? []` for a server old enough to predate the field. Its silence is not a
  // claim of completeness, but it is not a claim of damage either, and turning
  // it into one here would be its own lie.
  const additions = (reported ?? []).filter(
    (sequence) =>
      Number.isInteger(sequence) && sequence >= 1 && !held.includes(sequence),
  );
  if (additions.length === 0) return held;
  return [...new Set([...held, ...additions])].sort((left, right) => left - right);
}

/**
 * Each undeliverable position, placed between the events that did arrive.
 *
 * This is what positions buy over a count. `sequence` on a delivered event and
 * the numbers in `skipped_sequences` are the same namespace, so a hole can be
 * shown where it happened -- "between these two steps" -- instead of as a bare
 * "some events are missing" that leaves a reader unable to tell which part of
 * the run they are looking at an incomplete account of.
 *
 * A position a delivered event now occupies is dropped rather than reported: a
 * re-read that decoded the row hands the client the event it was once told it
 * had lost, and going on claiming a hole there would be the same lie in the
 * other direction. Events without a sequence (transient, never stored) anchor
 * nothing, since they have no position to compare against.
 */
export function locateTimelineGaps(
  events: readonly EventEnvelope[],
  skippedSequences: readonly number[],
): TimelineGap[] {
  const placed: Array<{ sequence: number; event: EventEnvelope }> = [];
  for (const event of events) {
    const sequence = event.sequence;
    if (sequence === null || !Number.isInteger(sequence)) continue;
    placed.push({ sequence, event });
  }
  placed.sort((left, right) => left.sequence - right.sequence);

  const gaps: TimelineGap[] = [];
  for (const sequence of [...skippedSequences].sort((left, right) => left - right)) {
    let before: EventEnvelope | null = null;
    let after: EventEnvelope | null = null;
    let delivered = false;
    for (const entry of placed) {
      if (entry.sequence === sequence) {
        delivered = true;
        break;
      }
      if (entry.sequence < sequence) before = entry.event;
      else {
        after = entry.event;
        break;
      }
    }
    if (delivered) continue;
    gaps.push({ sequence, before, after });
  }
  return gaps;
}

export function findTaskInputRef(events: readonly EventEnvelope[]): string | null {
  for (const event of events) {
    if (!isEvent(event, "TaskSubmitted")) continue;
    const inputRef = stringField(event.payload, "input_ref");
    if (inputRef !== null) return inputRef;
  }
  return null;
}

/**
 * The pipeline this Task was submitted to, as a resubmittable choice.
 *
 * Read from `TaskSubmitted` rather than from the input artifact, because the
 * choice is deliberately not *in* the input: which pipeline runs a Task is a
 * property of the submission, and the submission event is where the resolved
 * version was recorded. `null` for a version this client cannot name -- a
 * retry then omits the field and takes the deployment default, which is the
 * honest move when the original's pipeline no longer has a name to ask for.
 */
export function findGraphChoice(
  events: readonly EventEnvelope[],
): TaskGraphChoice | null {
  for (const event of events) {
    if (!isEvent(event, "TaskSubmitted")) continue;
    const version = stringField(event.payload, "graph_version");
    if (version === "v1") return "research";
    if (version === "v2_general") return "general";
    return null;
  }
  return null;
}

const DECIDED_BY = new Set(["user", "model", "default"]);

/**
 * Who decided this Task's shape, from the submission event (ADR-036).
 *
 * `null` both for Tasks submitted before the field existed and for a block
 * this client cannot read -- the display is provenance, and a malformed
 * claim shown as fact would be worse than none.
 */
export function findTaskIntent(
  events: readonly EventEnvelope[],
): TaskIntent | null {
  for (const event of events) {
    if (!isEvent(event, "TaskSubmitted")) continue;
    const payload = event.payload as Record<string, unknown>;
    const intent = payload["intent"];
    if (!isRecord(intent)) return null;
    const graphDecidedBy = intent["graph_decided_by"];
    const wantsReportDecidedBy = intent["wants_report_decided_by"];
    const reason = intent["reason"];
    if (
      typeof graphDecidedBy !== "string" ||
      !DECIDED_BY.has(graphDecidedBy) ||
      typeof wantsReportDecidedBy !== "string" ||
      !DECIDED_BY.has(wantsReportDecidedBy) ||
      !(reason === null || reason === undefined || typeof reason === "string")
    ) {
      return null;
    }
    return {
      graph_decided_by: graphDecidedBy as TaskIntent["graph_decided_by"],
      wants_report_decided_by:
        wantsReportDecidedBy as TaskIntent["wants_report_decided_by"],
      reason: typeof reason === "string" ? reason : null,
    };
  }
  return null;
}

export function parseTaskInputArtifact(value: unknown): TaskInputArtifact | null {
  if (!isRecord(value)) return null;
  if (
    typeof value.schema_version !== "number" ||
    !Number.isInteger(value.schema_version) ||
    typeof value.objective !== "string" ||
    value.objective.trim() === "" ||
    typeof value.max_revisions !== "number" ||
    !Number.isInteger(value.max_revisions) ||
    value.max_revisions < 0 ||
    value.max_revisions > 20 ||
    !(
      value.knowledge_base_id === null ||
      typeof value.knowledge_base_id === "string"
    )
  ) {
    return null;
  }
  return {
    schema_version: value.schema_version,
    objective: value.objective,
    max_revisions: value.max_revisions,
    knowledge_base_id: value.knowledge_base_id,
    wants_report:
      typeof value.wants_report === "boolean" ? value.wants_report : true,
  };
}

export function findLatestApprovalId(events: readonly EventEnvelope[]): string | null {
  for (let index = events.length - 1; index >= 0; index -= 1) {
    const event = events[index];
    if (event === undefined || !isEvent(event, "TaskApprovalRequested")) continue;
    const approvalId = stringField(event.payload, "approval_id");
    if (approvalId !== null) return approvalId;
  }
  return null;
}

/**
 * A report is final only when an export_artifact proposal/start is followed by
 * its exact ToolCompleted event and the Task later records TaskSucceeded.
 */
export function findFinalReport(events: readonly EventEnvelope[]): FinalReportMatch | null {
  const exportToolCalls = new Set<string>();
  const completed: Array<
    Omit<FinalReportMatch, "succeededEventId"> & { completedIndex: number }
  > = [];

  for (let index = 0; index < events.length; index += 1) {
    const event = events[index];
    if (event === undefined) continue;

    if (isEvent(event, "ToolProposed") || isEvent(event, "ToolStarted")) {
      if (stringField(event.payload, "tool_name") === "export_artifact") {
        const toolCallId = stringField(event.payload, "tool_call_id");
        if (toolCallId !== null) exportToolCalls.add(toolCallKey(event, toolCallId));
      }
      continue;
    }

    if (!isEvent(event, "ToolCompleted")) continue;
    const toolCallId = stringField(event.payload, "tool_call_id");
    if (
      toolCallId === null ||
      !exportToolCalls.has(toolCallKey(event, toolCallId))
    ) {
      continue;
    }
    const artifact = parseArtifactRef(event.payload.artifact);
    if (artifact === null || artifact.kind !== "report") continue;
    completed.push({
      artifact,
      toolCallId,
      completedEventId: event.event_id,
      completedIndex: index,
    });
  }

  for (let candidateIndex = completed.length - 1; candidateIndex >= 0; candidateIndex -= 1) {
    const candidate = completed[candidateIndex];
    if (candidate === undefined) continue;
    for (let index = candidate.completedIndex + 1; index < events.length; index += 1) {
      const event = events[index];
      if (event !== undefined && isEvent(event, "TaskSucceeded")) {
        return {
          artifact: candidate.artifact,
          toolCallId: candidate.toolCallId,
          completedEventId: candidate.completedEventId,
          succeededEventId: event.event_id,
        };
      }
    }
  }
  return null;
}

/**
 * The nodes whose model output *is* the Task's draft, one per graph.
 *
 * `synthesize` in v1, `work` in v2 (ADR-031). Both write `draft_ref` for the
 * same reason and are the same step to a reader, so both have to be looked at
 * here -- reading only v1's node is what made a v2 Task with no file report
 * "没有产出内容" while its answer sat in the timeline unread.
 *
 * A set rather than a shape lookup: which graph ran is already decided by
 * `graphShapeOf`, but the node ids are disjoint, so asking "is this a drafting
 * node" needs no such decision and cannot disagree with one.
 */
const DRAFT_NODES: ReadonlySet<string> = new Set(["synthesize", "work"]);

/**
 * The draft the Task wrote, for a Task that produced no file.
 *
 * A Task that was not asked for a report still did the work, and the result has
 * to be readable somewhere. The drafting node's model output *is* that
 * result -- the export node only copies it into an artifact.
 *
 * The *last* text with something in it, because v2's drafting node is a tool
 * loop: every turn emits `ModelCompleted`, and the turns that only called tools
 * carry no text. `stringField` already drops `""`; the trim additionally drops
 * a turn whose whole text is whitespace, which renders as an empty answer block
 * over a draft the page is otherwise holding.
 *
 * Unlike Chat, a Task has no release fence over this text: there is no
 * `AnswerCommitted` boundary in the graph, and the report the export node
 * writes is this same text. Hiding it here would hide the Task's only output.
 */
export function findDraftText(events: readonly EventEnvelope[]): string | null {
  for (let index = events.length - 1; index >= 0; index -= 1) {
    const event = events[index];
    if (event === undefined) continue;
    if (event.graph_node_id === null || !DRAFT_NODES.has(event.graph_node_id)) {
      continue;
    }
    if (!isEvent(event, "ModelCompleted")) continue;
    const text = stringField(event.payload, "text");
    if (text !== null && text.trim() !== "") return text;
  }
  return null;
}

/** One artifact the Task produced, as the side rail lists it. */
export interface TaskArtifact {
  artifact: ArtifactRef;
  /** The stage that produced it, for saying where it came from. */
  graphNodeId: string | null;
  producedAt: string;
}

const ARTIFACT_KIND_LABELS: Record<string, string> = {
  evidence_bundle: "检索到的证据",
  report: "报告文件",
  task_input: "任务输入",
};

export function artifactLabel(artifact: ArtifactRef): string {
  return (
    ARTIFACT_KIND_LABELS[artifact.kind] ??
    artifact.filename ??
    artifact.kind
  );
}

/**
 * One stage's files that live in the Task's working set rather than the
 * artifact store.
 */
export interface WorkspaceWriteGroup {
  /** The stage that wrote them; null for an event with no node recorded. */
  graphNodeId: string | null;
  /** Names, first-write order, deduplicated within this group. */
  names: string[];
}

/**
 * The files a Task's tools wrote into its working set, by the stage that wrote
 * them.
 *
 * These are **not** artifacts and this rail cannot open them, which is the
 * whole reason the function exists as something separate rather than as more
 * rows in `collectArtifacts`. An artifact has an id the client may hold and a
 * route that serves it; a working-set file is addressed by name inside a
 * session, and the only routes that read one are mounted under
 * `/v1/code/sessions` behind a `mode="code"` check. Listing them here as if
 * they were openable would be a promise this page cannot keep.
 *
 * Why list them at all: `ToolCompleted.workspace_writes` has carried these
 * names unconditionally since ADR-063 -- outside the `record_step_inputs` gate,
 * precisely so they survive a default deployment -- and the Work page read none
 * of it. A Task that rendered three files into its working set showed a reader
 * nothing at all: not the names, not a count, not a sentence. Saying "these
 * exist and cannot be opened here" is strictly more than silence, and it costs
 * no new endpoint (known-gaps F-14).
 *
 * Grouped by stage rather than flattened because the stage is the only context
 * available -- there is no size, no media type, and no time beyond the event's
 * own -- and "which step made this" is the question a name alone leaves open.
 *
 * Deduplicated within a group and **not** across groups: one stage writing the
 * same name twice did one thing worth reporting once, while two stages writing
 * it are two facts, and collapsing them would hide that the second overwrote
 * the first.
 */
export function collectWorkspaceWrites(
  events: readonly EventEnvelope[],
): WorkspaceWriteGroup[] {
  const groups = new Map<string, WorkspaceWriteGroup>();
  for (const event of events) {
    if (!isEvent(event, "ToolCompleted")) continue;
    const written = event.payload.workspace_writes;
    if (!Array.isArray(written)) continue;
    // Keyed on the node so a stage that ran twice keeps one group. `\u0000` is
    // not a legal node id, so it cannot collide with a real one.
    const key = event.graph_node_id ?? "\u0000";
    let group = groups.get(key);
    if (group === undefined) {
      group = { graphNodeId: event.graph_node_id, names: [] };
      groups.set(key, group);
    }
    for (const name of written) {
      // Guarded rather than cast: this is a JSON payload off the wire, and a
      // non-string here would render as `[object Object]` in a file list.
      if (typeof name !== "string" || name === "") continue;
      if (!group.names.includes(name)) group.names.push(name);
    }
  }
  return [...groups.values()].filter((group) => group.names.length > 0);
}

/**
 * Every artifact this Task produced, oldest first.
 *
 * Collected from the timeline rather than from a dedicated endpoint because
 * the timeline is already the record of what happened, and an artifact only
 * exists here if some step is on record as having written it. Deduplicated by
 * id: a retried step re-reports the artifact it already wrote.
 */
export function collectArtifacts(
  events: readonly EventEnvelope[],
): TaskArtifact[] {
  const seen = new Set<string>();
  const found: TaskArtifact[] = [];
  for (const event of events) {
    // Two fields, because a Task puts files into the store through two doors
    // and the rail only ever watched one. `payload.artifact` is a tool's
    // result; `payload.output_ref` is a model call's own output -- which is
    // where the draft a `synthesize` or `work` node wrote lives. The step
    // detail pane has read `output_ref` all along (`stepDetail.ts`), so the
    // file was reachable, just three disclosures down: the rail claimed to
    // answer "what did this Task produce" while quietly meaning "what did its
    // tools produce".
    for (const field of [event.payload.artifact, event.payload.output_ref]) {
      const artifact = parseArtifactRef(field);
      if (artifact === null || seen.has(artifact.artifact_id)) continue;
      seen.add(artifact.artifact_id);
      found.push({
        artifact,
        graphNodeId: event.graph_node_id,
        producedAt: event.timestamp,
      });
    }
  }
  return found;
}

/**
 * Media types that are a Task's *product* rather than its paperwork.
 *
 * Named as a set of what counts rather than a rule over what does not, because
 * the interesting cases here are all positive: a deployment adds a renderer,
 * and the file it produces should headline the page the moment it exists.
 *
 * `text/html` joined when ADR-062 taught this console to run a produced page in
 * an opaque-origin frame. A page is the clearest case this set describes -- the
 * only way to accept one is to run it -- and it is now the artifact whose
 * headline position costs the reader least of all.
 *
 * The spreadsheet and presentation types stay listed but no longer headline on
 * their own: `findDeliverable` also asks whether this page can show the file,
 * and nothing here can show those two. They were promoted for a renderer this
 * deployment does not have, and a headline that opens on 这个类型只能下载查看
 * spends the most prominent place on the page saying nothing.
 */
const DOCUMENT_MEDIA_TYPES: ReadonlySet<string> = new Set([
  "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
  "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
  "application/vnd.openxmlformats-officedocument.presentationml.presentation",
  "application/pdf",
  "text/html",
]);

/**
 * What the Task surface can do with a file: convert a document, never run a
 * script. The counterpart of `CODE_ABILITIES` -- two surfaces, two rows.
 */
export const WORK_ABILITIES = { canRun: false, canConvert: true } as const;

/**
 * Stages whose artifacts are material the Task *fetched*, not work it produced.
 *
 * A blacklist rather than a whitelist of producing stages, and the difference is
 * a bug this page already shipped once. `v2_general` renders its document in
 * `work`, not in `export`, so "only export and render count" would drop a Word
 * Task's .docx off the headline and put `report.md` back -- which is exactly
 * what `findDeliverable` exists to have fixed.
 *
 * What it catches: a research Task that pulls a PDF off the web through the web
 * MCP server. That file is a real artifact, it is a `DOCUMENT_MEDIA_TYPE`, and
 * it arrives late, so it wins the "last document" rule and headlines the page
 * under a name like `mcp-result.bin`. It is somebody else's document.
 *
 * Not fixed by giving fetched results their own `ArtifactKind`: every MCP tool
 * shares one `put(kind="tool_result")`, so the word server's rendered .docx and
 * the web server's downloaded PDF go through the same line, and separating them
 * there would take the Word Task's headline with it.
 */
const FETCHING_NODES: ReadonlySet<string> = new Set([
  "research_internal",
  "research_external",
]);

/**
 * The file this Task was actually asked for, or the exported report when there
 * is none.
 *
 * A Task told to "create a Word document" produces two artifacts, and until now
 * the page led with the wrong one. `export_artifact` always writes `report.md`
 * -- that is the graph's own contract, the reviewed draft as a file -- so a run
 * that had already rendered a 37 KB .docx headlined a Markdown page reading
 * "Task report", with the document the reader asked for filed in the
 * attachment rail behind it. The reader's own words were "还是显示报告 md".
 *
 * Neither artifact is wrong and neither is hidden; what was wrong was the
 * order. So this prefers a rendered document when the run produced one and
 * falls back to the export when it did not, which leaves every research Task --
 * whose whole product *is* the written report -- exactly as it was.
 *
 * The *last* document, because a re-render after a reviewer's note supersedes
 * the draft that prompted it, and the superseded file is still in the rail.
 */
export function findDeliverable(
  events: readonly EventEnvelope[],
): ArtifactRef | null {
  const produced = collectArtifacts(events);
  for (let index = produced.length - 1; index >= 0; index -= 1) {
    const candidate = produced[index];
    if (candidate === undefined) continue;
    if (!DOCUMENT_MEDIA_TYPES.has(candidate.artifact.media_type)) continue;
    // Where it came from. A document a research stage pulled off the web is
    // evidence, not the answer -- and it stays in the rail, one click away.
    if (
      candidate.graphNodeId !== null &&
      FETCHING_NODES.has(candidate.graphNodeId)
    ) {
      continue;
    }
    // Whether this page can show it at all. The headline is where a "只能下载"
    // costs most, because it is what the reader sees before anything else.
    if (
      checkCost(
        candidate.artifact.media_type,
        candidate.artifact.filename ?? "",
        WORK_ABILITIES,
      ) === "unchecked"
    ) {
      continue;
    }
    return candidate.artifact;
  }
  return findFinalReport(events)?.artifact ?? null;
}

export function eventTitle(event: EventEnvelope): string {
  const title = KNOWN_EVENT_TITLES[event.event_type];
  if (title === undefined) return `未识别事件：${event.event_type}`;

  if (event.event_type === "ToolProposed" || event.event_type === "ToolStarted") {
    const toolName = stringField(event.payload, "tool_name");
    return toolName === null ? title : `${title}：${toolName}`;
  }
  return title;
}

export function isKnownEventType(eventType: string): boolean {
  return Object.hasOwn(KNOWN_EVENT_TITLES, eventType);
}

function isEvent(event: EventEnvelope, kind: string): boolean {
  return event.event_type === kind && event.payload.kind === kind;
}

function stringField(value: Record<string, unknown>, key: string): string | null {
  const field = value[key];
  return typeof field === "string" && field !== "" ? field : null;
}

function parseArtifactRef(value: unknown): ArtifactRef | null {
  if (!isRecord(value)) return null;
  if (
    typeof value.artifact_id !== "string" ||
    value.artifact_id === "" ||
    typeof value.kind !== "string" ||
    value.kind === "" ||
    typeof value.media_type !== "string" ||
    value.media_type === "" ||
    typeof value.size_bytes !== "number" ||
    !Number.isInteger(value.size_bytes) ||
    !Number.isFinite(value.size_bytes) ||
    value.size_bytes < 0 ||
    typeof value.sha256 !== "string" ||
    !/^[0-9a-f]{64}$/.test(value.sha256)
  ) {
    return null;
  }

  const artifact: ArtifactRef = {
    artifact_id: value.artifact_id,
    kind: value.kind,
    media_type: value.media_type,
    size_bytes: value.size_bytes,
    sha256: value.sha256,
  };
  if (typeof value.schema_version === "number") artifact.schema_version = value.schema_version;
  if (typeof value.tenant_id === "string") artifact.tenant_id = value.tenant_id;
  if (typeof value.filename === "string" || value.filename === null) {
    artifact.filename = value.filename;
  }
  return artifact;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function toolCallKey(event: EventEnvelope, toolCallId: string): string {
  return `${event.run_id}\u0000${toolCallId}`;
}
