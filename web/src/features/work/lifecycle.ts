import type { EventEnvelope, TaskStatus } from "../../api/types";

/**
 * The Task graph, as a reader follows it rather than as it is compiled.
 *
 * Graph nodes are grouped: `route` is bookkeeping between planning and
 * research, the two research nodes fan out in parallel, and the quality gate is
 * the critic's verdict rather than a separate thing that happens. Showing ten
 * nodes made the page look like ten decisions when there are six.
 *
 * Node ids that are not listed still appear -- as their own stage, at the end.
 * A graph that grew a node should show it, not hide it because this table is
 * out of date.
 */
const STAGES: ReadonlyArray<{ id: string; title: string; nodes: readonly string[] }> = [
  { id: "understand", title: "理解目标", nodes: ["understand"] },
  { id: "plan", title: "制定计划", nodes: ["plan", "route"] },
  {
    id: "research",
    title: "收集资料",
    nodes: ["research_internal", "research_external"],
  },
  { id: "synthesize", title: "撰写草稿", nodes: ["synthesize"] },
  { id: "review", title: "检查与修订", nodes: ["critic", "quality_gate"] },
  { id: "deliver", title: "确认与产出", nodes: ["approval", "export"] },
];

/**
 * What makes a stage failed.
 *
 * `ToolFailed` is deliberately not here. A denied or failing tool is recorded
 * on the step and the graph routinely continues past it -- the research stage
 * proposes `external_search`, policy refuses it, and the Task goes on to
 * succeed. Marking the stage red for that paints a finished Task as broken.
 * A stage failed when its *run* did.
 */
const FAILURE_EVENTS = new Set(["RunFailed", "TaskFailed", "TaskDeadLettered"]);

const TERMINAL_STATUSES = new Set<TaskStatus>([
  "succeeded",
  "failed",
  "cancelled",
  "dead_letter",
]);

export type StageState =
  | "done"
  | "active"
  | "failed"
  | "waiting"
  | "pending"
  | "skipped";

export interface LifecycleStage {
  id: string;
  title: string;
  state: StageState;
  eventCount: number;
  startedAt: string | null;
  endedAt: string | null;
}

export interface Lifecycle {
  stages: LifecycleStage[];
  doneCount: number;
  /** The stage a reader should be looking at, or null once nothing is moving. */
  currentTitle: string | null;
}

export function deriveLifecycle(
  events: readonly EventEnvelope[],
  status: TaskStatus | undefined,
): Lifecycle {
  const known = new Map(STAGES.map((stage) => [stage.id, stage] as const));
  const stageOf = new Map<string, string>();
  for (const stage of STAGES) {
    for (const node of stage.nodes) stageOf.set(node, stage.id);
  }

  const seen = new Map<
    string,
    { first: string; last: string; count: number; failed: boolean }
  >();
  const order: string[] = [];
  for (const event of events) {
    if (event.graph_node_id === null) continue;
    const id = stageOf.get(event.graph_node_id) ?? event.graph_node_id;
    const entry = seen.get(id);
    if (entry === undefined) {
      seen.set(id, {
        first: event.timestamp,
        last: event.timestamp,
        count: 1,
        failed: FAILURE_EVENTS.has(event.event_type),
      });
      order.push(id);
    } else {
      entry.last = event.timestamp;
      entry.count += 1;
      entry.failed = entry.failed || FAILURE_EVENTS.has(event.event_type);
    }
  }

  // Declared order first, then anything the graph produced that this table does
  // not know about, so a new node is visible rather than silently dropped.
  const ids = [
    ...STAGES.map((stage) => stage.id),
    ...order.filter((id) => !known.has(id)),
  ];
  const lastSeen = order.at(-1) ?? null;
  const terminal = status !== undefined && TERMINAL_STATUSES.has(status);
  const succeeded = status === "succeeded";

  const stages = ids.map<LifecycleStage>((id) => {
    const entry = seen.get(id);
    const title = known.get(id)?.title ?? id;
    if (entry === undefined) {
      return {
        id,
        title,
        // A stage that never ran on a Task that already finished did not stall
        // -- the graph routed past it, which is what happens when a Task was
        // never asked for a report. Leaving it "pending" would show unfinished
        // work on a succeeded Task forever.
        state: terminal ? "skipped" : "pending",
        eventCount: 0,
        startedAt: null,
        endedAt: null,
      };
    }
    const state: StageState = entry.failed
      ? "failed"
      : id === lastSeen && !terminal
        ? status === "waiting_approval"
          ? "waiting"
          : "active"
        : id === lastSeen && terminal && !succeeded
          ? "failed"
          : "done";
    return {
      id,
      title,
      state,
      eventCount: entry.count,
      startedAt: entry.first,
      endedAt: entry.last,
    };
  });

  const current = stages.find(
    (stage) => stage.state === "active" || stage.state === "waiting",
  );
  return {
    stages,
    doneCount: stages.filter((stage) => stage.state === "done").length,
    currentTitle: current?.title ?? null,
  };
}
