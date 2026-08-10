import type { EventEnvelope, TaskStatus } from "../../api/types";

/**
 * The Task graphs, as a reader follows them rather than as they are compiled.
 *
 * One list per graph (ADR-031), because this page renders the *declared* list
 * -- pending stages preview what a running Task will do, and a v2 Task
 * previewed with v1's six stages would promise research and drafting that its
 * graph does not contain. Which list applies is read from the timeline itself:
 * `TaskSubmitted` records the graph version, so the very first event settles
 * it. See {@link graphShapeOf}.
 *
 * Within each: graph nodes are grouped. In v1, `route` is bookkeeping between
 * planning and research, the two research nodes fan out in parallel, and the
 * quality gate is the critic's verdict rather than a separate thing that
 * happens. The `review` stage id is deliberately shared across the two lists:
 * v2's reviewer and v1's critic are the same step to a reader, which is why
 * the server shared their vocabulary in the first place.
 *
 * Node ids that are not listed still appear -- as their own stage, at the end.
 * A graph that grew a node should show it, not hide it because this table is
 * out of date.
 */
interface StageSpec {
  id: string;
  title: string;
  nodes: readonly string[];
}

const V1_STAGES: readonly StageSpec[] = [
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

const V2_STAGES: readonly StageSpec[] = [
  { id: "understand", title: "理解目标", nodes: ["understand"] },
  { id: "work", title: "动手做事", nodes: ["work"] },
  { id: "review", title: "检查与修订", nodes: ["review"] },
  { id: "deliver", title: "确认与产出", nodes: ["approval", "export"] },
];

export type GraphShape = "v1" | "v2";

const STAGES_BY_SHAPE: Record<GraphShape, readonly StageSpec[]> = {
  v1: V1_STAGES,
  v2: V2_STAGES,
};

/** Node ids that exist in exactly one graph, for the event-shaped fallback. */
const V2_ONLY_NODES = new Set(["work", "review"]);
const V1_ONLY_NODES = new Set([
  "plan",
  "route",
  "research_internal",
  "research_external",
  "synthesize",
  "critic",
  "quality_gate",
]);

/**
 * Which graph wrote this timeline.
 *
 * `TaskSubmitted` is the authority -- it is written in the same transaction as
 * the Task row and carries the resolved version -- so the shape is known from
 * the first event, before any node has run. The node-id fallback covers a
 * timeline read mid-stream from a cursor past the submission event. Defaulting
 * to v1 keeps every pre-v2 Task rendering exactly as it did.
 */
export function graphShapeOf(events: readonly EventEnvelope[]): GraphShape {
  for (const event of events) {
    if (event.event_type === "TaskSubmitted") {
      return event.payload["graph_version"] === "v2_general" ? "v2" : "v1";
    }
    if (event.graph_node_id !== null) {
      if (V2_ONLY_NODES.has(event.graph_node_id)) return "v2";
      if (V1_ONLY_NODES.has(event.graph_node_id)) return "v1";
    }
  }
  return "v1";
}

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

/**
 * The stage a graph node belongs to, for grouping its events under it.
 *
 * One mapping across both graphs rather than one per shape, because it is
 * consistent: the node sets are disjoint except for the three ids the graphs
 * deliberately share, and those land in the same stage either way. Unknown
 * nodes map to themselves, matching how `deriveLifecycle` gives an unlisted
 * node its own stage: a node this table has not heard of still shows its work
 * rather than dropping it.
 */
export function stageOfNode(graphNodeId: string): string {
  for (const stage of [...V1_STAGES, ...V2_STAGES]) {
    if (stage.nodes.includes(graphNodeId)) return stage.id;
  }
  return graphNodeId;
}

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
  const declared = STAGES_BY_SHAPE[graphShapeOf(events)];
  const known = new Map(declared.map((stage) => [stage.id, stage] as const));
  const stageOf = new Map<string, string>();
  for (const stage of declared) {
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
    ...declared.map((stage) => stage.id),
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
