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

import type {
  EventEnvelope,
  MessageView,
  TokenUsage,
  TurnUsageView,
} from "../../api/types";
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

/**
 * 这一轮为什么没有正常跑完。
 *
 * 从这个运行自己的终止事件读：`RunFailed` 带错误码和那句英文，`RunCancelled` 只
 * 有取消，`RunCompleted` 的 `stop_reason` 不是 `completed` 时是撞了某个上限
 * （deadline、max_steps、token_budget……）。正常完成的一轮是 `null`——成功靠不说话
 * 来说，这一行只在出事的那一轮出现。
 */
export interface TurnStop {
  kind: "failed" | "cancelled" | "ceiling";
  reason: string;
  code: string | null;
  message: string | null;
}

export interface CodeTurnBlock {
  key: string;
  /** 1-based, counted over the transcript's user messages. */
  index: number;
  runId: string | null;
  instruction: string;
  report: string | null;
  /**
   * 这一轮烧了多少，从这个运行自己的终止事件读。
   *
   * 不从消息上读，虽然 Chat 那边正是那么做的：一次编码 turn **不写**
   * `chat_turns` 行——那张表是 Chat 的 turn 账本——所以走消息那条路在这里永远
   * 是空。运行的终止事件是这一轮唯一记下过账的地方，而这一页本来就把那些事件
   * 重放了一遍。
   *
   * `null` 表示这里问不出答案：还在跑的那一轮没有终局，事件窗口之外的老运行也
   * 没有。不是零。
   */
  usage: TurnUsageView | null;
  /** 没跑完的那一轮为什么停。正常完成是 `null`。 */
  stop: TurnStop | null;
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
  /**
   * Which runs those were, oldest first.
   *
   * The count says *that* the page is missing something; the ids say *which*,
   * and that is what a caller needs to do anything about it. `CodePage`
   * re-reads the transcript once per id it has not already tried -- a run with
   * no instruction is usually not another tab at all, it is this tab having
   * fetched the transcript a moment before the server appended the sentence.
   *
   * Per id rather than on the count, because the count is not a fresh signal:
   * a genuinely orphaned run keeps it above zero forever, and a reload keyed
   * on that would re-fetch on every render for the life of the session.
   */
  orphanRunIds: string[];
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
  /**
   * The model call currently in flight, if any.
   *
   * Only the id, never the text. The text changes at token rate; putting it in
   * this function's inputs would rebuild every block on every frame.
   */
  liveCallId: string;
}): TurnBlocks {
  const { messages, events, running, pendingInstruction, liveCallId } = input;

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

  // Whether the live run belongs to the last message in the transcript rather
  // than to a pending sentence. Both cases are ordinary and which one holds is
  // a race the page does not control: the server appends the user message
  // *before* the run starts, so a transcript re-read that lands after that
  // append already carries the instruction and the page has nothing pending
  // left to draw. Without this the live run had no block to live in -- the
  // sentence showed, and its steps, its thinking and its report did not, until
  // the turn ended and the settled pairing finally found it.
  const adoptsLiveRun = pendingInstruction === null && live !== undefined;

  // How many of the transcript's instructions the *settled* runs pair against.
  // One fewer when the last one has already claimed the live run, or the newest
  // settled run would be handed to the turn before it and every card in the
  // session would slide one block back.
  //
  // Floored at zero for the one frame where a live run exists and the
  // transcript has not arrived: switching sessions mid-turn leaves `running`
  // true while the new session's history is still being fetched. `n - 1` would
  // be -1 there, and `orphanRuns` would come out one *higher* than the number
  // of runs -- a count on screen that cannot be true.
  const slots = adoptsLiveRun ? Math.max(0, n - 1) : n;
  // `m > slots` is the other-tab case. Pair only the newest `slots` and drop
  // the rest: a card on the wrong turn is a lie, a card that is missing is a
  // gap the page can admit to.
  const paired = Math.min(slots, m);
  const orphanRuns = Math.max(0, m - slots);
  // The dropped ones are the *oldest*, because the pairing above is
  // tail-aligned: the newest `slots` runs take the instructions.
  const orphanRunIds = settled.slice(0, orphanRuns).map((run) => run.runId);

  const blocks: CodeTurnBlock[] = [];
  for (const [position, messageIndex] of asked.entries()) {
    const claimsLive = adoptsLiveRun && position === n - 1;
    // Tail-aligned: the last instruction gets the last run.
    const fromEnd = slots - 1 - position;
    const run = claimsLive
      ? live
      : fromEnd >= 0 && fromEnd < paired
        ? settled[m - 1 - fromEnd]
        : undefined;
    const instruction = messages[messageIndex]?.text ?? "";
    const next = messages[messageIndex + 1];
    const report =
      next !== undefined && next.role === "assistant" ? next.text : null;
    const usage = usageOf(run?.events ?? []);
    blocks.push(
      blockOf({
        key: run?.runId ?? `unpaired:${String(position)}`,
        index: position + 1,
        runId: run?.runId ?? null,
        instruction,
        report,
        usage,
        stop: stopOf(run?.events ?? []),
        events: run?.events ?? [],
        live: claimsLive,
        liveCallId,
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
        // 还在跑：终局还没写下，所以这一轮的账这里问不出来。
        usage: null,
        stop: null,
        events: live?.events ?? [],
        live: true,
        liveCallId,
      }),
    );
  }

  annotateWriters(blocks);
  return { blocks, orphanRuns, orphanRunIds };
}


/**
 * 一个运行的账，从它的终止事件里取。
 *
 * `RunCompleted` / `RunFailed` / `RunCancelled` 各带一份 `BudgetUsage`——token
 * 和钱都在里面，而且是运行**当时**按那一刻的价目表算好写下的。一个运行只写一条
 * 终止事件，所以这里不会重复计。
 *
 * 失败和取消也读：一轮跑一半死掉照样烧了钱，把它显示成没有花销，会让最该被看见
 * 的那种花费恰好不可见。
 */
function usageOf(events: readonly EventEnvelope[]): TurnUsageView | null {
  for (const event of events) {
    if (
      event.event_type !== "RunCompleted" &&
      event.event_type !== "RunFailed" &&
      event.event_type !== "RunCancelled"
    ) {
      continue;
    }
    const payload = event.payload as {
      usage?: { tokens?: Partial<TokenUsage>; cost_micro_usd?: number };
    };
    const usage = payload.usage;
    if (usage === undefined) continue;
    const tokens = usage.tokens ?? {};
    return {
      input_tokens: tokens.input_tokens ?? 0,
      output_tokens: tokens.output_tokens ?? 0,
      cache_read_tokens: tokens.cache_read_tokens ?? 0,
      cache_write_tokens: tokens.cache_write_tokens ?? 0,
      cost_micro_usd: usage.cost_micro_usd ?? 0,
    };
  }
  return null;
}


/**
 * 一个运行的终局，只在它没有正常跑完时。
 *
 * 和 `usageOf` 读的是同一条事件，分开写是因为答的不是同一个问题：那边问「花了
 * 多少」，成功失败都要答；这边问「为什么没成」，成功时的答案就是没有这一行。
 */
function stopOf(events: readonly EventEnvelope[]): TurnStop | null {
  for (const event of events) {
    const payload = event.payload as {
      stop_reason?: unknown;
      error?: { code?: unknown; message?: unknown };
    };
    const reason = str(payload.stop_reason);
    if (event.event_type === "RunFailed") {
      return {
        kind: "failed",
        reason: reason ?? "error",
        code: str(payload.error?.code),
        message: str(payload.error?.message),
      };
    }
    if (event.event_type === "RunCancelled") {
      return { kind: "cancelled", reason: reason ?? "cancelled", code: null, message: null };
    }
    if (event.event_type === "RunCompleted") {
      if (reason === null || reason === "completed") return null;
      return { kind: "ceiling", reason, code: null, message: null };
    }
  }
  return null;
}


function blockOf(
  base: {
    key: string;
    index: number;
    runId: string | null;
    instruction: string;
    report: string | null;
    usage: TurnUsageView | null;
    stop: TurnStop | null;
    events: EventEnvelope[];
    live: boolean;
  } & { liveCallId: string },
): CodeTurnBlock {
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

  // The step a live thought lands on, when its `ModelStarted` has not arrived
  // yet. It is synthesised *here* rather than rendered as an extra `<li>` after
  // the mapped list, and that is not a tidiness preference -- it is the fix for
  // a real remount.
  //
  // React reconciles a mapped array by key *within that array*. An element that
  // moves from a trailing conditional slot into the array is, to React, a
  // different child position: it unmounts and rebuilds. Measured on one
  // session, 5 of 53 thoughts lived their whole life in that slot, and every
  // `ModelStarted` arrival -- which the transient delta beats by up to a whole
  // catch-up poll -- tore the row down, reset the disclosure the reader had
  // opened, and jittered the layout twice.
  //
  // One array, one key space, no remount.
  const { liveCallId: inFlight, ...rest } = base;
  if (
    rest.live &&
    inFlight !== "" &&
    !steps.some((step) => step.modelCallId === inFlight)
  ) {
    steps.push({
      key: `model:${inFlight}`,
      modelCallId: inFlight,
      thinking: "",
      group: null,
    });
  }

  return { ...rest, groups, steps, produced: producedIn(groups) };
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

/**
 * 这条流里被写过的项目文件，按第一次被写到的顺序。
 *
 * 一个纯函数而不是 `useCodeStream` 里的一块 state，理由和这个模块里其他东西
 * 一样：它是事件的函数，而 `steps` 已经在手上。加一块 state 只会多出一个可能
 * 和事件不一致的副本，并且要在换会话时记得清空——`CodePage` 的注释里已经把
 * 「从 effect 里清状态晚了一帧」这件事的代价写过一遍了。
 *
 * **它说的是「这条流看见过」，不是「这段会话写过」。** `useCodeStream` 只留
 * 最近 `KEPT_EVENTS` 条，所以一段很长的会话最早那几轮的写入不在里面；刷新页
 * 面之后，重放能给回多少就是多少。差别在界面上是一个标记没有出现，而不是一
 * 个标记出现在错的地方——少标一个是漏说，标错一个是撒谎，两者不等价，这也是
 * 这里不去猜的原因。
 *
 * `ToolCompleted.project_writes` 是唯一来源（ADR-086）。没有 `argument_preview`
 * 那条后备路线：那条路线在工作区那侧是为 ADR-063 之前的历史事件留的，而这个
 * 字段和它的发布者是同一次改动加上的——历史事件里没有它，也没有别的东西能
 * 冒充它。
 */
export function projectWritesIn(events: EventEnvelope[]): string[] {
  const seen = new Set<string>();
  const order: string[] = [];
  for (const event of events) {
    if (event.event_type !== "ToolCompleted") continue;
    const written = event.payload.project_writes;
    if (!Array.isArray(written)) continue;
    for (const entry of written) {
      const path = str(entry);
      if (path === null || seen.has(path)) continue;
      seen.add(path);
      order.push(path);
    }
  }
  return order;
}
