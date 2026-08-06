import type { ArtifactRef, EventEnvelope, TaskTimelineResponse } from "../../api/types";

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
}

export interface TimelineGroup {
  id: string;
  graphNodeId: string | null;
  events: EventEnvelope[];
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
  ToolStarted: "工具调用已开始",
  ToolProgress: "工具调用有新进展",
  ToolCompleted: "工具调用已完成",
  ToolFailed: "工具调用失败",
  ContextCompacted: "上下文已压缩",
  AgentDelegated: "子代理已委派",
  AgentCompleted: "子代理已完成",
};

export function createTimelineState(taskId: string): TimelineState {
  return { taskId, events: [], cursor: null };
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
  };
}

export function groupTimelineEvents(events: readonly EventEnvelope[]): TimelineGroup[] {
  const groups = new Map<string, TimelineGroup>();
  for (const event of events) {
    const id = event.graph_node_id === null ? "task:" : `node:${event.graph_node_id}`;
    const group = groups.get(id);
    if (group === undefined) {
      groups.set(id, {
        id,
        graphNodeId: event.graph_node_id,
        events: [event],
      });
    } else {
      group.events.push(event);
    }
  }
  return [...groups.values()];
}

export function findTaskInputRef(events: readonly EventEnvelope[]): string | null {
  for (const event of events) {
    if (!isEvent(event, "TaskSubmitted")) continue;
    const inputRef = stringField(event.payload, "input_ref");
    if (inputRef !== null) return inputRef;
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
 * The draft the Task wrote, for a Task that produced no file.
 *
 * A Task that was not asked for a report still did the work, and the result has
 * to be readable somewhere. The synthesize node's model output *is* that
 * result -- the export node only copies it into an artifact.
 *
 * Unlike Chat, a Task has no release fence over this text: there is no
 * `AnswerCommitted` boundary in the graph, and the report the export node
 * writes is this same text. Hiding it here would hide the Task's only output.
 */
export function findDraftText(events: readonly EventEnvelope[]): string | null {
  for (let index = events.length - 1; index >= 0; index -= 1) {
    const event = events[index];
    if (event === undefined) continue;
    if (event.graph_node_id !== "synthesize") continue;
    if (!isEvent(event, "ModelCompleted")) continue;
    const text = stringField(event.payload, "text");
    if (text !== null) return text;
  }
  return null;
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
