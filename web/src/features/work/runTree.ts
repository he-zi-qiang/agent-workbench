/**
 * The runs a Task's stream holds, arranged by who started whom.
 *
 * The counterpart of `application/run_tree.py`, and the duplication is
 * deliberate rather than an oversight. The two answer the same question for
 * callers in opposite situations: the server's builds a tree for a client that
 * does **not** hold the stream (a deep link into one sub-agent, a non-browser
 * client), and this one serves the page that already has every event and
 * receives new ones as they arrive. Calling `/runs` from here would be a second
 * request to learn what the first one carried -- and, worse, it would only
 * refresh when something asked it to, so a panel meant to show live progress
 * would lag the timeline sitting next to it.
 *
 * Both must agree, so both follow the same three rules, and each is written out
 * where it is enforced below:
 *
 * 1. a run that started and never finished is **running**, not omitted;
 * 2. a child a parent announced counts even before it has written anything;
 * 3. an id nothing attested as a run -- a Task's own lifecycle events are
 *    written under the *task* id -- is not a node.
 */

import type { EventEnvelope } from "../../api/types";

export type RunStatus =
  | "running"
  | "completed"
  | "failed"
  | "cancelled"
  | "unknown";

/** What one run spent, as far as this page has been told. */
export interface RunSpend {
  steps: number;
  toolCalls: number;
  inputTokens: number;
  outputTokens: number;
}

export interface RunNode {
  runId: string;
  parentRunId: string | null;
  /** The sub-agent it was started as; `null` for a run nobody delegated. */
  definitionName: string | null;
  /** The graph node it ran under, when its events carried one. */
  nodeId: string | null;
  status: RunStatus;
  spend: RunSpend;
  /** How many of this run's events this page holds. The progress denominator. */
  eventCount: number;
  /** What it is doing now, or the last thing it did. */
  latestEventType: string | null;
  firstSequence: number | null;
  children: RunNode[];
}

const EMPTY_SPEND: RunSpend = {
  steps: 0,
  toolCalls: 0,
  inputTokens: 0,
  outputTokens: 0,
};

const TERMINAL_STATUS: Readonly<Record<string, RunStatus>> = {
  RunCompleted: "completed",
  RunFailed: "failed",
  RunCancelled: "cancelled",
};

interface Accumulator extends Omit<RunNode, "children"> {
  attested: boolean;
  childIds: string[];
}

function spendFrom(usage: unknown): RunSpend {
  if (typeof usage !== "object" || usage === null) return EMPTY_SPEND;
  const held = usage as {
    steps?: unknown;
    tool_calls?: unknown;
    tokens?: { input_tokens?: unknown; output_tokens?: unknown };
  };
  const number = (value: unknown): number =>
    typeof value === "number" && Number.isFinite(value) ? value : 0;
  return {
    steps: number(held.steps),
    toolCalls: number(held.tool_calls),
    inputTokens: number(held.tokens?.input_tokens),
    outputTokens: number(held.tokens?.output_tokens),
  };
}

function isEmpty(spend: RunSpend): boolean {
  return (
    spend.steps === 0 &&
    spend.toolCalls === 0 &&
    spend.inputTokens === 0 &&
    spend.outputTokens === 0
  );
}

/**
 * Rebuild the tree the given events describe.
 *
 * Tolerant by construction, because the input is whatever the page holds: the
 * middle of a stream, a child whose delegation has scrolled away, a parent
 * whose child has not written yet. Each of those produces a node rather than an
 * exception -- a partial timeline is the normal case while a Task is running,
 * not a corrupt one.
 */
export function buildRunTree(events: readonly EventEnvelope[]): RunNode[] {
  const runs = new Map<string, Accumulator>();
  const order: string[] = [];

  const accumulator = (runId: string): Accumulator => {
    let held = runs.get(runId);
    if (held === undefined) {
      held = {
        runId,
        parentRunId: null,
        definitionName: null,
        nodeId: null,
        status: "unknown",
        spend: EMPTY_SPEND,
        eventCount: 0,
        latestEventType: null,
        firstSequence: null,
        attested: false,
        childIds: [],
      };
      runs.set(runId, held);
      order.push(runId);
    }
    return held;
  };

  for (const event of events) {
    const own = accumulator(event.run_id);
    own.eventCount += 1;
    own.latestEventType = event.event_type;
    if (own.nodeId === null) own.nodeId = event.graph_node_id;
    if (own.firstSequence === null) own.firstSequence = event.sequence;

    const payload = event.payload as Record<string, unknown>;

    if (event.event_type === "AgentDelegated") {
      const childId = payload.child_agent_run_id;
      if (typeof childId !== "string" || childId === "") continue;
      const child = accumulator(childId);
      child.parentRunId = event.run_id;
      child.definitionName =
        typeof payload.profile_name === "string" ? payload.profile_name : null;
      // Rule 2: announced is enough. `AgentDelegated` is written before the
      // child's first event, so between those two writes the child exists and
      // has said nothing.
      child.attested = true;
      // And announcing one attests the announcer, for a page that begins after
      // its `RunStarted`.
      own.attested = true;
      if (!own.childIds.includes(childId)) own.childIds.push(childId);
      continue;
    }

    if (event.event_type === "AgentCompleted") {
      const childId = payload.child_agent_run_id;
      if (typeof childId !== "string" || childId === "") continue;
      const child = accumulator(childId);
      child.attested = true;
      own.attested = true;
      // Second-hand, and used only where the child did not report for itself.
      const status = payload.status;
      if (child.status === "unknown" && typeof status === "string") {
        child.status = TERMINAL_STATUS[status] ?? "unknown";
      }
      if (isEmpty(child.spend)) child.spend = spendFrom(payload.usage);
      continue;
    }

    if (event.event_type === "RunStarted") {
      // Rule 1: started and nothing since is `running`, which is exactly what
      // a crashed Worker leaves behind. Omitting it would make a crash look
      // like work that was never attempted.
      own.attested = true;
      own.status = "running";
      continue;
    }

    const terminal = TERMINAL_STATUS[event.event_type];
    if (terminal !== undefined) {
      own.attested = true;
      own.status = terminal;
      own.spend = spendFrom(payload.usage);
    }
  }

  const shape = (runId: string, seen: ReadonlySet<string>): RunNode => {
    const held = runs.get(runId) as Accumulator;
    const nextSeen = new Set(seen).add(runId);
    const children = held.childIds
      // `seen` guards a cycle no correct producer can write, because an endless
      // recursion in a read model is a far worse symptom than a short branch.
      .filter((id) => runs.get(id)?.attested === true && !seen.has(id))
      .map((id) => shape(id, nextSeen));
    return {
      runId: held.runId,
      parentRunId: held.parentRunId,
      definitionName: held.definitionName,
      nodeId: held.nodeId,
      status: held.status,
      spend: held.spend,
      eventCount: held.eventCount,
      latestEventType: held.latestEventType,
      firstSequence: held.firstSequence,
      children,
    };
  };

  return order
    // Rule 3. A Task's own lifecycle events are written under the *task* id, so
    // without this every Task grows a phantom root that is `unknown` forever
    // and has spent nothing. The panel claims to list runs; that is not one.
    .filter((runId) => {
      const held = runs.get(runId) as Accumulator;
      if (!held.attested) return false;
      return (
        held.parentRunId === null || runs.get(held.parentRunId) === undefined
      );
    })
    .map((runId) => shape(runId, new Set()));
}

/** Every node of a tree, parents before children. */
export function flattenRuns(nodes: readonly RunNode[]): RunNode[] {
  return nodes.flatMap((node) => [node, ...flattenRuns(node.children)]);
}

/** What the whole tree spent, children included. */
export function totalSpend(nodes: readonly RunNode[]): RunSpend {
  return flattenRuns(nodes).reduce<RunSpend>(
    (total, node) => ({
      steps: total.steps + node.spend.steps,
      toolCalls: total.toolCalls + node.spend.toolCalls,
      inputTokens: total.inputTokens + node.spend.inputTokens,
      outputTokens: total.outputTokens + node.spend.outputTokens,
    }),
    EMPTY_SPEND,
  );
}
