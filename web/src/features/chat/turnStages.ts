import type { StreamStage, StreamStageState } from "../../components/StepStream";
import { formatTime } from "../../components/ui";
import type { ChatActivity, ChatTurnPhase } from "./model";

/**
 * A Chat turn as the stages it went through.
 *
 * Work derives its stages from `graph_node_id`, because a Task *is* a graph.
 * A Chat turn is not: every event on a turn carries a null node id, so there is
 * nothing to group by except what the events mean. That is why this table is
 * keyed on event kind rather than on node, and why it lives here rather than
 * being shared with Work's -- the two read different data to answer the same
 * question.
 *
 * Three stages, because that is what a turn actually does: assemble what the
 * model will see, call the model, then decide whether the answer may be
 * published. The publication step is a real stage rather than bookkeeping --
 * it is where an answer can be withheld, and ADR-018's ungrounded path is
 * visible nowhere else.
 */
const STAGES: ReadonlyArray<{
  id: string;
  title: string;
  kinds: readonly string[];
}> = [
  {
    id: "context",
    title: "检索资料",
    kinds: [
      "ContextBuilt",
      "RetrievalRejected",
      "ToolProposed",
      "PermissionResolved",
      "ToolStarted",
      "ToolCompleted",
      "ToolFailed",
    ],
  },
  { id: "model", title: "生成回答", kinds: ["ModelStarted", "ModelCompleted"] },
  {
    id: "publish",
    title: "核对与发布",
    kinds: ["AnswerCommitted", "UngroundedAnswerCommitted", "AnswerWithheld"],
  },
];

/** Run bookkeeping. Real, and not one of the three things a turn does. */
const META_KINDS = new Set([
  "RunStarted",
  "RunCompleted",
  "RunFailed",
  "RunCancelled",
  "ChatTurnExpired",
]);

const TERMINAL_PHASES = new Set<ChatTurnPhase>([
  "committed",
  "withheld",
  "failed",
]);

export function isTurnMetaActivity(activity: ChatActivity): boolean {
  return META_KINDS.has(activity.kind);
}

function stageIdOf(kind: string): string | null {
  for (const stage of STAGES) {
    if (stage.kinds.includes(kind)) return stage.id;
  }
  return null;
}

/**
 * The turn's stages, in order, each carrying its own events.
 *
 * An activity whose kind is in neither table still lands somewhere: unknown
 * kinds join the stage that is currently last, so a server that grows an event
 * type shows it rather than dropping it silently.
 */
export function deriveTurnStages(
  activities: readonly ChatActivity[],
  phase: ChatTurnPhase,
): StreamStage[] {
  const byStage = new Map<string, ChatActivity[]>();
  const order: string[] = [];
  let lastKnown = STAGES[0]?.id ?? "context";
  for (const activity of activities) {
    if (META_KINDS.has(activity.kind)) continue;
    const id = stageIdOf(activity.kind) ?? lastKnown;
    lastKnown = id;
    const existing = byStage.get(id);
    if (existing === undefined) {
      byStage.set(id, [activity]);
      order.push(id);
    } else {
      existing.push(activity);
    }
  }

  const terminal = TERMINAL_PHASES.has(phase);
  const lastSeen = order.at(-1) ?? null;

  return STAGES.map((stage) => {
    const found = byStage.get(stage.id) ?? [];
    const state = stageState(stage.id, found, {
      terminal,
      phase,
      isLast: stage.id === lastSeen,
    });
    return {
      id: stage.id,
      title: stage.title,
      state,
      note: stageNote(state, found),
      events: found.map((activity) => activity.envelope),
    };
  });
}

function stageState(
  _id: string,
  found: readonly ChatActivity[],
  context: { terminal: boolean; phase: ChatTurnPhase; isLast: boolean },
): StreamStageState {
  if (found.length === 0) {
    // A finished turn that never retrieved did not stall on retrieval -- it
    // answered directly, which is the whole point of the no-knowledge-base
    // path. Leaving it "pending" would show unfinished work on a done turn.
    return context.terminal ? "skipped" : "pending";
  }
  if (found.some((activity) => activity.state === "failed")) return "failed";
  if (found.some((activity) => activity.state === "waiting")) return "waiting";
  if (context.isLast && !context.terminal) return "active";
  if (context.isLast && context.phase === "failed") return "failed";
  return "done";
}

function stageNote(
  state: StreamStageState,
  found: readonly ChatActivity[],
): string {
  if (state === "skipped") return "未执行";
  if (state === "pending") return "等待中";
  if (state === "waiting") return "已阻止";
  if (state === "active") return "进行中";
  const last = found.at(-1);
  return last === undefined ? "" : formatTime(last.timestamp);
}
