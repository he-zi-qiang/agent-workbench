/**
 * A coding session as the turns a reader lived through, not as a graph run.
 *
 * This replaces `turnStages.ts`, which grouped the same events into
 * `StreamStage`s so that `StepStream` -- Work's component -- could draw them.
 * That was the wrong borrowing. Work has a graph and its reader asks which
 * node a run is on; a coding session has no graph, and its reader asks what
 * happened when they said the thing they said. Reusing the stage tree gave
 * Code a vertical rail of node dots, a `第 N 轮` pseudo-stage, and three levels
 * of nested disclosure to reach a filename -- Task's vocabulary answering a
 * question nobody at a coding prompt was asking.
 *
 * What comes out instead is one block per instruction, holding the six things
 * that happened under it in the order they happened: the instruction, the
 * thought in flight, what it did, what it produced, the report, and -- folded
 * away -- what it thought along the way.
 *
 * Everything here is a pure function over data the page already has. The one
 * hard problem is the third section below.
 *
 * ## Why turns are paired from the tail
 *
 * `MessageView` is `{role, text}` and nothing else (`api/types.ts:60-63`): the
 * transcript carries no `run_id`, so a message cannot name the run that served
 * it. The events carry `run_id` and nothing that names a message. The join has
 * to be positional, and the only end that is reliably aligned is the recent
 * one: `useCodeStream` keeps `KEPT_EVENTS = 2000` events, so a long session
 * loses its *oldest* runs from the stream while the transcript keeps every
 * message. Anchoring at the head would shift every block by the number of runs
 * that fell out of the window; anchoring at the tail leaves the oldest
 * instructions with no block, which is both correct and visible.
 *
 * Making this exact would mean putting `run_id` on the stored message -- a
 * persistence schema change with a migration and a pass through the
 * parameterised `tests/contracts/` suite on two implementations. That was
 * weighed and declined (ADR-063); the tail anchor is a priced decision, not an
 * oversight.
 *
 * ## Why a produced file is not read out of a sentence
 *
 * A card needs the *name* a tool wrote. Three sources exist and only one is a
 * contract:
 *
 * * `ToolCompleted.workspace_writes` -- structured, published outside the
 *   `record_step_inputs` gate on purpose (ADR-063). This is the one.
 * * `ToolProposed.argument_preview` -- canonical JSON, `sort_keys=True`, so
 *   `workspace_write`'s keys arrive as `content < media_type < name`. The
 *   preview is bounded at 4096 characters, which truncates the *name* away
 *   exactly when the written body is large. Kept as a secondary source because
 *   it recovers every pre-ADR-063 write under that ceiling, and because it
 *   couples to a JSON shape rather than to a sentence.
 * * `ToolCompleted.output_preview` -- three untested English sentences in two
 *   adapters. Not used. A UI that parses prose breaks when somebody improves
 *   the wording, and nothing in the test suite would notice.
 */

import type { EventEnvelope, MessageView } from "../../api/types";
import { groupSteps, type StepGroup } from "../../components/stepGroups";

/** What one tool call put into the workspace, as the card says it. */
export interface ProducedFile {
  name: string;
  /** What the call did. Never what the file *is* -- see `overwrote`. */
  action: "write" | "edit" | "run";
  /**
   * An earlier turn in this stream already wrote this name.
   *
   * Only ever true when we actually watched that happen. "新建" is not on
   * offer anywhere in this module: the event window starts where it starts,
   * and a file written before it began is indistinguishable from one that
   * never existed. Claiming novelty is the one thing the data cannot support.
   */
  overwrote: boolean;
  /** A later turn wrote it again -- that turn's number. Null when this is the last. */
  supersededByTurn: number | null;
  toolCallId: string;
}

/**
 * One step of a turn: what the model thought, and what that thought caused.
 *
 * `group` is null in two ordinary cases, neither of them an error -- the turn
 * that produced text rather than a call (the report follows it), and the call
 * that has not come back yet, which is the anchor the live thought lands on.
 */
export interface TurnStep {
  /** Stable across polls: `groupSteps`'s key, never a position. */
  key: string;
  /** Which model call did the thinking. Null for a tool group with no model events. */
  modelCallId: string | null;
  /** The durable excerpt. Empty when the call did not think, or has not returned. */
  thinking: string;
  /** What it did. Null for an answering turn and for a call still in flight. */
  group: StepGroup | null;
}

export interface CodeTurnBlock {
  key: string;
  /** 1-based, counted over the transcript's user messages. */
  index: number;
  runId: string | null;
  instruction: string;
  report: string | null;
  /** Tool calls only, for the produced-file cards. */
  groups: StepGroup[];
  /** What happened, in order: each thought beside the action it caused. */
  steps: TurnStep[];
  produced: ProducedFile[];
  /** Every event of this run, in order, for the raw disclosure. */
  events: EventEnvelope[];
  live: boolean;
}

export interface TurnBlocks {
  blocks: CodeTurnBlock[];
  /**
   * Runs that could not be paired with an instruction and were dropped.
   *
   * Non-zero means another tab ran turns in this same session, so the stream
   * holds more runs than this transcript has instructions. The page says so
   * rather than shifting every card onto the wrong turn.
   */
  orphanRuns: number;
}

/**
 * Run bookkeeping that says a run is over. A run with none of these, while a
 * request is open, is the one being watched.
 */
const TERMINAL_RUN_EVENTS = new Set([
  "RunCompleted",
  "RunFailed",
  "RunCancelled",
]);

export function buildTurnBlocks(input: {
  messages: MessageView[];
  events: EventEnvelope[];
  /** Whether a turn's request is open. Nothing is live when it is not. */
  running: boolean;
  pendingInstruction: string | null;
}): TurnBlocks {
  const { messages, events, running, pendingInstruction } = input;

  // Insertion-ordered, so runs come out in the order the server emitted them.
  const byRun = new Map<string, EventEnvelope[]>();
  for (const event of events) {
    const held = byRun.get(event.run_id);
    if (held === undefined) byRun.set(event.run_id, [event]);
    else held.push(event);
  }
  const runs = [...byRun.entries()].map(([runId, runEvents]) => ({
    runId,
    events: runEvents,
  }));

  // Which run is being watched, read off the events rather than remembered
  // from the moment of sending. A run the server has finished carries one of
  // three terminal records; the newest run without one, while a request is
  // open, is the turn in flight.
  //
  // The alternative -- snapshotting the run ids at send time and taking the
  // set difference -- needs that snapshot to have landed as state before the
  // first frame of the new run arrives, and it has a wrong answer for the
  // frames in between. This reads the same fact from the data that carries it.
  //
  // "Newest without a terminal record" and not simply "the newest": a run that
  // never finished because the process holding it died is not live, and
  // drawing it as active would spin forever for something nothing is waiting
  // on. It is excluded here only while `running` is false, which is exactly
  // when nothing is waiting.
  const live = running
    ? runs
        .filter(
          (run) =>
            !run.events.some((event) =>
              TERMINAL_RUN_EVENTS.has(event.event_type),
            ),
        )
        .at(-1)
    : undefined;
  const liveRunId = live?.runId ?? null;
  const settled = runs.filter((run) => run.runId !== liveRunId);

  // Indices of the user messages, which are what a turn is counted by. Not
  // `messages[2k]`: an assistant message is appended only when the turn came
  // back with text, so a turn that ran out of budget leaves two user messages
  // adjacent and every even-index assumption after it is off by one.
  const asked: number[] = [];
  for (const [index, message] of messages.entries()) {
    if (message.role === "user") asked.push(index);
  }

  const n = asked.length;
  const m = settled.length;
  // `m > n` is the other-tab case. Pair only the newest `n` and drop the rest:
  // a card on the wrong turn is a lie, a card that is missing is a gap the
  // page can admit to.
  const paired = Math.min(n, m);
  const orphanRuns = Math.max(0, m - n);

  const blocks: CodeTurnBlock[] = [];
  for (const [position, messageIndex] of asked.entries()) {
    // Tail-aligned: the last instruction gets the last run.
    const fromEnd = n - 1 - position;
    const run = fromEnd < paired ? settled[m - 1 - fromEnd] : undefined;
    const instruction = messages[messageIndex]?.text ?? "";
    const next = messages[messageIndex + 1];
    const report =
      next !== undefined && next.role === "assistant" ? next.text : null;
    blocks.push(
      blockOf({
        key: run?.runId ?? `unpaired:${String(position)}`,
        index: position + 1,
        runId: run?.runId ?? null,
        instruction,
        report,
        events: run?.events ?? [],
        live: false,
      }),
    );
  }

  // The instruction whose request is still open. It is held separately from
  // `messages` rather than appended optimistically: the transcript is re-read
  // from the server when the turn settles, and an optimistic copy that is
  // still present when the server's arrives shows the sentence twice.
  if (pendingInstruction !== null) {
    blocks.push(
      blockOf({
        key: liveRunId ?? "pending",
        index: n + 1,
        runId: liveRunId,
        instruction: pendingInstruction,
        report: null,
        events: live?.events ?? [],
        live: true,
      }),
    );
  }

  annotateWriters(blocks);
  return { blocks, orphanRuns };
}

function blockOf(base: {
  key: string;
  index: number;
  runId: string | null;
  instruction: string;
  report: string | null;
  events: EventEnvelope[];
  live: boolean;
}): CodeTurnBlock {
  // No `titleFor`: that argument is how Work injects its lifecycle dictionary,
  // and `TaskDeadLettered` is not a phrase that belongs over a coding step.
  // Without it `groupSteps` falls back to the raw event type, which only ever
  // shows on a `solo:` group -- and those are dropped below.
  const all = groupSteps(base.events);
  const groups = all.filter((group) => group.key.startsWith("tool:"));

  // The timeline, in the order the server emitted it. Nothing here sorts,
  // joins or infers: `groupSteps` has already filed each tool-calling model
  // turn *ahead of* the first call it named (`stepGroups.ts`, pinned by
  // `stepGroups.test.ts`), so a thought is already sitting on the step it
  // caused. All this does is stop throwing that away.
  //
  // The previous shape built a second, flat list of every `ModelCompleted`'s
  // excerpt and rendered it in one disclosure at the foot of the turn. Both
  // lists came from the same ordered array, and nothing put them back
  // together -- so the reader got a column of paragraphs that could not answer
  // the one question they exist for: *why did it run that command*.
  //
  // Three conditions hold this together, and each fails visibly rather than
  // silently, which is why they are worth naming:
  //   (a) a tool-calling turn *usually* comes back with empty text, so
  //       `groupSteps` folds it into the call it named. Measured on a real
  //       session, DeepSeek narrates on some turns and not others -- three of
  //       four calls in one run kept their own `model:` group. Relying on that
  //       fold alone left the thought and its command on two sibling rows, so
  //       the join below is done here too, from `tool_call_ids`. Pairing must
  //       not depend on whether the model felt like saying something.
  //   (b) the merge is a *prepend*, which is the only thing that makes the
  //       thought read as preceding the action.
  //   (c) a turn naming two calls is filed on the first, so one model call is
  //       never drawn as two thoughts.
  // Which tool call each thought named, taken from the event that carries
  // both. Filled on the model group and read on the tool group it points at,
  // which works in one pass because `ModelCompleted` always precedes the
  // `ToolProposed` of the call it proposed.
  const thoughtFor = new Map<string, { callId: string; text: string }>();
  for (const group of all) {
    if (!group.key.startsWith("model:")) continue;
    const completed = group.events.find(
      (event) => event.event_type === "ModelCompleted",
    );
    const thinking = str(completed?.payload.thinking_preview);
    const named = firstId(completed?.payload.tool_call_ids);
    if (thinking === null || named === null) continue;
    thoughtFor.set(`tool:${named}`, {
      callId: str(completed?.payload.model_call_id) ?? named,
      text: thinking,
    });
  }

  const steps: TurnStep[] = [];
  for (const group of all) {
    // `solo:` is run bookkeeping -- RunStarted, RunCompleted. Never a step.
    if (group.key.startsWith("solo:")) continue;
    const completed = group.events.find(
      (event) => event.event_type === "ModelCompleted",
    );
    const started = group.events.find(
      (event) => event.event_type === "ModelStarted",
    );
    const isTool = group.key.startsWith("tool:");

    if (isTool) {
      // Either the model turn was folded in here by `groupSteps` (the
      // no-narration path), or it stayed a group of its own and we join it by
      // the id it named. Both end with the thought on the action it caused.
      const claimed = thoughtFor.get(group.key);
      const modelCallId =
        str(completed?.payload.model_call_id) ??
        str(started?.payload.model_call_id) ??
        claimed?.callId ??
        null;
      steps.push({
        key: group.key,
        modelCallId,
        thinking: str(completed?.payload.thinking_preview) ?? claimed?.text ?? "",
        group,
      });
      continue;
    }

    // A model group. If its thought was claimed by the call it named, it is
    // already rendered there and must not appear again.
    const named = firstId(completed?.payload.tool_call_ids);
    if (named !== null && thoughtFor.has(`tool:${named}`)) continue;

    const thinking = str(completed?.payload.thinking_preview) ?? "";
    // No thought and nothing to wait for: an empty row saying nothing. The one
    // exception is the anchor a live thought is about to land on, and only a
    // live block has one of those.
    if (thinking === "" && !(base.live && completed === undefined)) continue;
    steps.push({
      key: group.key,
      modelCallId:
        str(completed?.payload.model_call_id) ??
        str(started?.payload.model_call_id),
      thinking,
      group: null,
    });
  }

  return { ...base, groups, steps, produced: producedIn(groups) };
}

/** The files one turn's successful tool calls put into the workspace. */
function producedIn(groups: StepGroup[]): ProducedFile[] {
  const found: ProducedFile[] = [];
  for (const group of groups) {
    // A denied or failed call wrote nothing. `outcome` already prefers a
    // denial over the failure it caused, so both are excluded by this one test.
    if (group.outcome !== "ok") continue;
    const written = writesOf(group);
    for (const name of written.names) {
      found.push({
        name,
        action: written.action,
        overwrote: false,
        supersededByTurn: null,
        toolCallId: group.key.slice("tool:".length),
      });
    }
  }
  // Same name twice inside one turn is one card, the last one.
  const seen = new Map<string, ProducedFile>();
  for (const file of found) seen.set(file.name, file);
  return [...seen.values()];
}

function writesOf(group: StepGroup): {
  names: string[];
  action: ProducedFile["action"];
} {
  const completed = group.events.find(
    (event) => event.event_type === "ToolCompleted",
  );
  const proposed = group.events.find(
    (event) => event.event_type === "ToolProposed",
  );
  const toolName = str(proposed?.payload.tool_name) ?? "";
  const action: ProducedFile["action"] =
    toolName === "workspace_edit"
      ? "edit"
      : toolName === "sandbox_run"
        ? "run"
        : "write";

  const declared = completed?.payload.workspace_writes;
  if (Array.isArray(declared)) {
    const names = declared.filter(
      (entry): entry is string => typeof entry === "string" && entry !== "",
    );
    if (names.length > 0) return { names, action };
  }

  // The pre-ADR-063 fallback. Restricted to the two tools whose argument
  // schema has a `name`, so a `sandbox_run` whose *script* happens to contain
  // the word cannot be mistaken for a declaration.
  if (toolName === "workspace_write" || toolName === "workspace_edit") {
    const name = nameInPreview(str(proposed?.payload.argument_preview));
    if (name !== null) return { names: [name], action };
  }
  return { names: [], action };
}

/**
 * The `name` field of a bounded canonical-JSON argument preview.
 *
 * Returns null far more often than it looks like it should: the preview is cut
 * at 4096 characters and `name` sorts after `content`, so any write with a
 * body over that ceiling arrives as unparseable JSON. That silence is the
 * reason ADR-063 exists; here it is simply a card that does not appear.
 */
function nameInPreview(preview: string | null): string | null {
  if (preview === null) return null;
  let parsed: unknown;
  try {
    parsed = JSON.parse(preview);
  } catch {
    return null;
  }
  if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
    return null;
  }
  return str((parsed as Record<string, unknown>).name);
}

/**
 * Which turn last wrote each name, in two passes.
 *
 * Forward for `overwrote` (has this stream seen the name before?), backward
 * for `supersededByTurn` (does a later turn write it again?). Both are said
 * out loud on the card, because a workspace file route serves the *current*
 * bytes -- there is no way to ask for the version a turn produced, and
 * `tests/architecture/test_a_workspace_version_is_never_asked_for.py` is why.
 * The card can either say so before the click or mislead after it.
 */
function annotateWriters(blocks: CodeTurnBlock[]): void {
  const seen = new Set<string>();
  for (const block of blocks) {
    for (const file of block.produced) {
      file.overwrote = seen.has(file.name);
      seen.add(file.name);
    }
  }
  const lastWriter = new Map<string, number>();
  for (let index = blocks.length - 1; index >= 0; index -= 1) {
    const block = blocks[index];
    if (block === undefined) continue;
    for (const file of block.produced) {
      const later = lastWriter.get(file.name);
      file.supersededByTurn = later ?? null;
      lastWriter.set(file.name, block.index);
    }
  }
}

/** The first tool call id a model turn named, if it named any. */
function firstId(value: unknown): string | null {
  if (!Array.isArray(value)) return null;
  for (const entry of value) {
    const id = str(entry);
    if (id !== null) return id;
  }
  return null;
}

function str(value: unknown): string | null {
  if (typeof value !== "string") return null;
  const trimmed = value.trim();
  return trimmed === "" ? null : trimmed;
}
