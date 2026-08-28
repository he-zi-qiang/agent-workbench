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
 *
 * **Where this one goes further, and why that is not a fourth rule.** The
 * server returns a shape an HTTP client has to be able to parse a year from
 * now; this one is read by one component in the same repository. So the fields
 * below that the server's `RunNode` does not have -- the ceiling a run declared
 * for itself, why it failed, what it is doing now -- are *additions to the
 * node*, never changes to which nodes exist or what status they carry. Those
 * three questions are the ones the two must never answer differently, and the
 * parity test at the bottom of `runTree.test.ts` is where that is enforced.
 */

import type { EventEnvelope } from "../api/types";

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
  /**
   * The one cache figure that is additive.
   *
   * `cache_read_tokens` is a *subset* of `input_tokens` -- the part of the
   * prompt served from cache -- so adding it would count the cached prompt
   * twice. `cache_write_tokens` is reported outside the prompt count, and is
   * therefore the only one that belongs in a total. Carried rather than
   * dropped because `max_total_tokens` is judged against a figure that
   * includes it (`domain/runs.py::TokenUsage.total`), and a panel that showed
   * a run's spend against its ceiling while omitting it would draw the run as
   * further from that ceiling than the runtime believes it to be.
   */
  cacheWriteTokens: number;
}

/**
 * The ceilings a run declared for itself when it started.
 *
 * First-hand and per-run: `RunStarted.budget` is what this run was actually
 * given, not what the deployment is configured with today. A delegated run
 * carries `multi_agent.max_tokens_per_agent_invocation` here
 * (`apps/task_worker/composition.py`), which is the number that stops it.
 *
 * Every field is optional in the domain except the two step ceilings, and a
 * `null` here means the run declared none -- not zero, and not "unlimited as
 * far as we know". The panel shows a ceiling only where there is one.
 */
export interface RunCeiling {
  maxSteps: number | null;
  maxToolCalls: number | null;
  maxTotalTokens: number | null;
}

/** Why a run stopped, as its own terminal event or its parent reported it. */
export interface RunFailure {
  code: string;
  message: string;
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
  /** What this run said it was allowed to spend, from its own `RunStarted`. */
  ceiling: RunCeiling;
  /**
   * The model profile this run was started under, when it said so.
   *
   * A delegated run may be given a different profile from its parent, and
   * "which model is this sub-agent" is otherwise not answerable from the page.
   */
  modelProfile: string | null;
  /** How many tools this run was given. `null` when its `RunStarted` is not in view. */
  toolCount: number | null;
  /**
   * Why it stopped, from `RunFailed.error`.
   *
   * A row that can say a run failed and cannot say why sends the reader to the
   * step stream to find a fact the panel was already holding. `AgentCompleted`
   * carries no error, so a child whose own `RunFailed` is not in view keeps a
   * `failed` status with a `null` failure -- second-hand that it failed, and
   * no second-hand account of why.
   */
  failure: RunFailure | null;
  /** The `stop_reason` its terminal event carried. */
  stopReason: string | null;
  /**
   * What it is waiting for, while the last thing it wrote was `RunPaused`.
   *
   * Tracked rather than derived from `latestEventType` at the call site,
   * because the *reason* is the half worth showing: a run stopped at an
   * approval gate and a run parked for a migration are both "paused" and need
   * different things from the reader. Cleared by the next event, so a run that
   * resumed is not still described as waiting.
   */
  pausedFor: string | null;
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
  cacheWriteTokens: 0,
};

const NO_CEILING: RunCeiling = {
  maxSteps: null,
  maxToolCalls: null,
  maxTotalTokens: null,
};

/** The event that closed a run, to the status it closed in. Keyed by event type. */
const STATUS_FOR_EVENT: Readonly<Record<string, RunStatus>> = {
  RunCompleted: "completed",
  RunFailed: "failed",
  RunCancelled: "cancelled",
};

/**
 * A parent's second-hand report of how its child ended, to a node's status.
 *
 * **A separate table from `STATUS_FOR_EVENT`, and the reason is a bug this
 * file shipped with.** `AgentCompleted.status` is a `RunStatus` -- one of
 * `completed`/`failed`/`cancelled` (`domain/runs.py:35`) -- and the first
 * version of this module looked it up in the event-type table above. Those
 * keys are `RunCompleted`/`RunFailed`/`RunCancelled`, so the lookup missed
 * every time and fell through to `unknown`: the entire second-hand path, which
 * is the one thing `AgentCompleted` exists to make possible, silently never
 * resolved. A page holding a parent whose child's own events were skipped
 * showed that child as 等待中 forever, including when the parent had recorded
 * it as failed.
 *
 * Written as a mapping rather than a cast, for the same reason
 * `application/run_tree.py::_STATUS_FOR_RUN_STATUS` is: a status added to the
 * domain then has to be considered here, instead of silently becoming
 * `unknown` again.
 */
const STATUS_FOR_RUN_STATUS: Readonly<Record<string, RunStatus>> = {
  completed: "completed",
  failed: "failed",
  cancelled: "cancelled",
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
    tokens?: {
      input_tokens?: unknown;
      output_tokens?: unknown;
      cache_write_tokens?: unknown;
    };
  };
  return {
    steps: count(held.steps),
    toolCalls: count(held.tool_calls),
    inputTokens: count(held.tokens?.input_tokens),
    outputTokens: count(held.tokens?.output_tokens),
    cacheWriteTokens: count(held.tokens?.cache_write_tokens),
  };
}

function count(value: unknown): number {
  return typeof value === "number" && Number.isFinite(value) ? value : 0;
}

/** A ceiling the run declared, or `null` where it declared none. */
function ceilingOf(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function isEmpty(spend: RunSpend): boolean {
  return (
    spend.steps === 0 &&
    spend.toolCalls === 0 &&
    spend.inputTokens === 0 &&
    spend.outputTokens === 0 &&
    spend.cacheWriteTokens === 0
  );
}

/**
 * Every token a run moved, counting each of them once.
 *
 * The same arithmetic as `domain/runs.py::TokenUsage.total`, and it has to be:
 * this is the figure `max_total_tokens` is judged against, so a panel drawing
 * spend against ceiling with different arithmetic would draw a run as further
 * from its ceiling than the runtime that will stop it believes it to be.
 * `cache_read_tokens` is not added because it is already inside
 * `input_tokens`.
 */
export function totalTokens(spend: RunSpend): number {
  return spend.inputTokens + spend.outputTokens + spend.cacheWriteTokens;
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
        ceiling: NO_CEILING,
        modelProfile: null,
        toolCount: null,
        failure: null,
        stopReason: null,
        pausedFor: null,
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
    // Whatever this run is now doing, it is no longer waiting. Cleared for
    // every event rather than only for the ones that mean "resumed": the set
    // of events that can follow a pause is open, and a stale 等待批准 on a row
    // that has since called three tools is the failure worth designing out.
    if (event.event_type !== "RunPaused") own.pausedFor = null;
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
      // And it has a position already: the delegation that named it is what a
      // reader would scroll to, and it is the only position an announced-but-
      // silent child has. The server fills `sequence` from exactly here
      // (`application/run_tree.py`); leaving it null until the child's own
      // first event -- which is what this did before -- made the one field
      // both sides compute disagree in the one state it exists for.
      if (child.firstSequence === null) child.firstSequence = event.sequence;
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
        child.status = STATUS_FOR_RUN_STATUS[status] ?? "unknown";
      }
      if (child.stopReason === null && typeof payload.stop_reason === "string") {
        child.stopReason = payload.stop_reason;
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
      // The rest of `RunStarted` is what makes a ceiling first-hand. It is the
      // budget *this* run was given -- not the deployment's current config,
      // which is a different number the moment anybody edits a profile.
      const budget = payload.budget;
      if (typeof budget === "object" && budget !== null) {
        const held = budget as Record<string, unknown>;
        own.ceiling = {
          maxSteps: ceilingOf(held.max_steps),
          maxToolCalls: ceilingOf(held.max_tool_calls),
          maxTotalTokens: ceilingOf(held.max_total_tokens),
        };
      }
      if (typeof payload.model_profile === "string") {
        own.modelProfile = payload.model_profile;
      }
      if (Array.isArray(payload.tool_names)) {
        own.toolCount = payload.tool_names.length;
      }
      continue;
    }

    if (event.event_type === "RunPaused") {
      own.attested = true;
      const reason = payload.reason;
      // A pause with no reason still pauses. Recorded as the empty string so
      // the row can say "waiting" without inventing what for.
      own.pausedFor = typeof reason === "string" ? reason : "";
      continue;
    }

    const terminal = STATUS_FOR_EVENT[event.event_type];
    if (terminal !== undefined) {
      own.attested = true;
      own.status = terminal;
      own.spend = spendFrom(payload.usage);
      if (typeof payload.stop_reason === "string") {
        own.stopReason = payload.stop_reason;
      }
      const error = payload.error;
      if (typeof error === "object" && error !== null) {
        const held = error as { code?: unknown; message?: unknown };
        if (typeof held.code === "string" && typeof held.message === "string") {
          own.failure = { code: held.code, message: held.message };
        }
      }
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
      ceiling: held.ceiling,
      modelProfile: held.modelProfile,
      toolCount: held.toolCount,
      failure: held.failure,
      stopReason: held.stopReason,
      pausedFor: held.pausedFor,
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
      cacheWriteTokens: total.cacheWriteTokens + node.spend.cacheWriteTokens,
    }),
    EMPTY_SPEND,
  );
}
