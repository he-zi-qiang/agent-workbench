import type { StreamStage, StreamStageState } from "../../components/StepStream";
import { formatTime } from "../../components/ui";
import type { EventEnvelope } from "../../api/types";

/**
 * A coding session as the turns it has run, each openable to its real steps.
 *
 * Work groups by `graph_node_id` because a Task is a graph; Chat groups by what
 * its events mean because a turn is not. Code is a third case and gets a third
 * reading: a session is a *sequence of turns*, and `run_id` already delimits
 * them exactly -- one run per instruction, assigned by the server.
 *
 * Grouping by turn is what makes the history legible. The alternative, one flat
 * list per session, answers "what happened" but not "what happened when I asked
 * that", and the second question is the one a reader has when they scroll back
 * to a report they do not believe.
 *
 * The newest turn comes last, in the reading order of the transcript above it.
 */

/** The events that are bookkeeping about a run rather than steps within it. */
const RUN_KINDS = new Set([
  "RunStarted",
  "RunCompleted",
  "RunFailed",
  "RunCancelled",
]);

/** Terminal run events, and the stage state each one implies. */
const RUN_OUTCOMES: Readonly<Record<string, StreamStageState>> = {
  RunCompleted: "done",
  RunFailed: "failed",
  RunCancelled: "skipped",
};

export function codeTurnStages(
  events: EventEnvelope[],
  running: boolean,
): StreamStage[] {
  // Insertion-ordered, so turns come out in the order the server emitted them
  // rather than in whatever order a map iteration would give.
  const byRun = new Map<string, EventEnvelope[]>();
  for (const event of events) {
    const held = byRun.get(event.run_id);
    if (held === undefined) byRun.set(event.run_id, [event]);
    else held.push(event);
  }

  const runs = [...byRun.entries()];
  return runs.map(([runId, runEvents], index) => {
    const terminal = runEvents.find((event) => event.event_type in RUN_OUTCOMES);
    const isLast = index === runs.length - 1;
    // Only the last turn can still be running, and only when the page says a
    // request is in flight. A turn with no terminal event that is *not* the
    // last one did not end -- the process it belonged to died, and ADR-recorded
    // premise F-01 says that turn is gone. Calling it "active" forever would
    // draw a spinner for something nothing is waiting on.
    const state: StreamStageState =
      terminal !== undefined
        ? RUN_OUTCOMES[terminal.event_type] ?? "done"
        : isLast && running
          ? "active"
          : "failed";

    const first = runEvents[0];
    const started = first === undefined ? "" : formatTime(first.timestamp);
    return {
      id: runId,
      // Numbered rather than titled by instruction: the instruction is already
      // the line above this in the transcript, and repeating it here would make
      // the same sentence the label of two different things.
      title: `第 ${String(index + 1)} 轮`,
      state,
      note:
        state === "active"
          ? "进行中"
          : state === "failed" && terminal === undefined
            ? "未完成"
            : started,
      // Run bookkeeping stays in the stage rather than being hoisted to `meta`:
      // for Code there is one run per stage, so its start and finish belong to
      // the turn they delimit, and `meta` is where a *shared* header goes.
      events: runEvents,
    };
  });
}

/** Whether an event is one of the run's own bookkeeping records. */
export function isRunEvent(event: EventEnvelope): boolean {
  return RUN_KINDS.has(event.event_type);
}
