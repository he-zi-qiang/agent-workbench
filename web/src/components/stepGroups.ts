import type { EventEnvelope } from "../api/types";

/**
 * A run's events, folded into the steps a reader would say it took.
 *
 * The stream this folds is faithful and unreadable. One tool call emits five
 * events -- proposed, permission requested, permission resolved, started,
 * completed -- and each model turn emits two more, so a Task that read a dozen
 * pages arrives as sixty rows of bookkeeping with the dozen actual actions
 * scattered through it. A reader asking "what did it do" has to reconstruct the
 * answer from lifecycle vocabulary, which is the log's vocabulary and not
 * theirs.
 *
 * **Folded, never dropped.** Every event that went in comes out, inside the
 * group it belongs to, in its original order. This module decides what a step
 * is *called* and what the collapsed line says; it does not decide what is
 * allowed to be seen, and opening a group still shows the five raw events with
 * their payloads. That distinction is the same one `StepDetail` draws, and it
 * matters more here: a summary that quietly discarded a permission denial would
 * be a UI that hides exactly the events an operator opened the page to find.
 *
 * Three kinds of group come out:
 *
 * * **A tool call**, keyed by `tool_call_id` -- the whole five-event lifecycle
 *   as one line that names the tool, its subject and how it ended.
 * * **A model turn that produced text**, keyed by `model_call_id`. This is the
 *   agent saying something, and it is a step in its own right.
 * * **A model turn that produced only tool calls**, which is not a step at all.
 *   It is how the previous line caused the next one, so it is folded into the
 *   group of the first call it named (`tool_call_ids`) rather than given a row
 *   that says the model decided to do the thing the very next row shows it
 *   doing. This is the fold that takes sixty rows down to the dozen.
 *
 * Anything else keeps a row of its own. A step this module has no opinion about
 * is shown as it always was, because a grouping table that silently swallowed
 * an event type somebody added later is the failure mode worth designing out.
 */

/** How a step ended, as the collapsed line reports it. */
export type StepOutcome = "ok" | "failed" | "denied" | "running";

/**
 * The four things that have to happen to a tool call, in the order they have
 * to happen in.
 *
 * The names are the reader's, not the log's: 提议 is `ToolProposed`, 授权 is
 * whatever the policy gateway and any human approver decided, 开始 is
 * `ToolStarted`, 完成 is `ToolCompleted` or `ToolFailed`. Four rather than the
 * five events, because `PermissionRequested` and `PermissionResolved` are one
 * question and its answer -- a reader watching a call get held does not need
 * two beads to be told it is being decided.
 */
export type GateStepKey = "proposed" | "authorized" | "started" | "finished";

/**
 * Where one of those four stands.
 *
 * `pending` is the honest default and the reason this is derived per-bead
 * rather than as a single "how far did it get" index: a call refused by a hook
 * before it ever reached the policy gateway emits no `PermissionResolved` at
 * all, and an index would have to guess whether that means "not yet" or
 * "skipped". Each bead lights only on an event that actually arrived, so the
 * gap in that call's row is a fact about the run rather than a rendering
 * choice.
 */
export type GateStepState = "pending" | "done" | "denied" | "failed";

export interface GateStep {
  key: GateStepKey;
  /** 被拒 replaces 授权 when that is where the call stopped. */
  label: string;
  state: GateStepState;
}

export interface StepGroup {
  /** Stable across polls: derived from the ids in the events, never positional. */
  key: string;
  title: string;
  /**
   * The tool's subject -- a query, a URL, a filename -- when it has one that
   * fits. Null rather than a truncated blob of JSON, which reads as noise.
   */
  subject: string | null;
  outcome: StepOutcome;
  /**
   * The authorization sequence, for a tool call; null for anything else.
   *
   * Null and not an empty array: a model turn has no gate to pass, and an
   * empty array would render as "four stages, none reached".
   */
  gate: GateStep[] | null;
  /** Every event that folded into this step, in the order it arrived. */
  events: EventEnvelope[];
}

/**
 * What this system's tools do, in the words a reader would use.
 *
 * Falls back to the raw tool name, deliberately. A tool missing from this table
 * is a tool nobody has written a phrase for yet, and showing its real name is
 * both honest and more useful than a generic "调用工具" that would make two
 * different tools look like the same step.
 */
const TOOL_VERBS: Readonly<Record<string, string>> = {
  external_search: "搜索网络",
  web_search: "搜索网络",
  knowledge_search: "检索知识库",
  mcp_web_fetch_page: "读取网页",
  mcp_web_download_document: "下载文件",
  mcp_word_render_document: "生成 Word 文档",
  workspace_write: "写入工作区",
  // The one Code tool that was missing, and the one it uses most: an edit is
  // what every follow-up instruction produces. Without a phrase here the row
  // read `workspace_edit`, in a list where its four siblings were in Chinese.
  workspace_edit: "修改工作区文件",
  workspace_read: "读取工作区",
  workspace_list: "查看工作区",
  workspace_grep: "搜索工作区",
  // The project set (ADR-073), which is not the workspace tools with another
  // backend behind them: these reach a real directory on the reader's machine
  // (ADR-072). ADR-073 keeps the two sets mutually exclusive within a run, so
  // one list never mixes them -- but the reader scanning a list does not know
  // which set this session got, and 写入项目目录 has to be tellable from
  // 写入工作区 at a glance. Hence 目录 in every phrase rather than 项目 swapped
  // in for 工作区: 项目 alone cannot carry it, because ADR-071 leaves a Project
  // a nullable label that also tags conversations and tasks, so 项目文件 would
  // name something that exists with no directory behind it.
  //
  // Five now, not four. The comment here used to say there is no
  // `project_grep`, and that stopped being true when one was written --
  // `CODE_PROJECT_TOOLS` has five entries. A stale absence claim is worse than
  // a missing phrase: it tells the next reader not to look.
  project_read: "读取项目目录",
  project_write: "写入项目目录",
  project_edit: "修改项目目录文件",
  project_list: "查看项目目录",
  project_grep: "搜索项目目录",
  export_artifact: "导出报告",
  sandbox_run: "运行代码",
  // Not 运行代码, which is `sandbox_run`'s phrase and describes a throwaway
  // container with no network. This one runs on the reader's own machine, in
  // their own directory, with no sandbox and no undo (ADR-077) -- and it is
  // the row they are looking at while deciding whether to approve it. The two
  // have to be tellable apart at a glance, so the phrase names the machine
  // rather than the act.
  project_run: "在本机执行命令",
};

/** The keys that carry a call's subject, in the order they are preferred. */
const SUBJECT_KEYS = ["query", "url", "name", "path", "question"] as const;

/**
 * The keys whose value is a *list* of subjects; the first one names the call.
 *
 * `sandbox_run` takes `{"inputs": ["snake.html"], "script": "…"}` -- the file
 * it runs against is the only part a reader wants, and it is never a string,
 * so the loop above walks straight past it and the row renders as a bare
 * 运行代码 with nothing to say what it ran on.
 */
const SUBJECT_LIST_KEYS = ["inputs", "paths", "names"] as const;

const MAX_SUBJECT_CHARS = 56;

interface Building {
  key: string;
  events: EventEnvelope[];
  /** Set once the group's identity is known, so late events cannot rename it. */
  toolName: string | null;
  subject: string | null;
  outcome: StepOutcome;
  modelText: string | null;
  /** Null until an event proves otherwise; see `GateStepState`. */
  gate: GateFacts | null;
}

/** What the events said about the gate, before it is phrased for a reader. */
interface GateFacts {
  proposed: boolean;
  /** allow / allow_with_modified_input from the policy engine. */
  allowed: boolean;
  /** A policy deny, or a human answering the question the policy raised. */
  denied: boolean;
  started: boolean;
  completed: boolean;
  failed: boolean;
}

/**
 * @param titleFor  What to call an event this module has no verb for. Passed in
 * rather than imported because the vocabulary lives in a feature and this is a
 * component: a caller that has words supplies them, and one that does not gets
 * the event type, which is what this always did.
 */
export function groupSteps(
  events: readonly EventEnvelope[],
  titleFor?: (event: EventEnvelope) => string,
): StepGroup[] {
  const order: string[] = [];
  const groups = new Map<string, Building>();
  // Where a text-less model turn should be filed once its tool calls are known.
  // Recorded as the turn is read and applied afterwards, because the turn is
  // seen before the calls it names.
  const modelTurnHome = new Map<string, string>();

  const open = (key: string): Building => {
    const held = groups.get(key);
    if (held !== undefined) return held;
    const made: Building = {
      key,
      events: [],
      toolName: null,
      subject: null,
      outcome: "running",
      modelText: null,
      gate: null,
    };
    groups.set(key, made);
    order.push(key);
    return made;
  };

  for (const event of events) {
    const payload = event.payload as Record<string, unknown>;
    const toolCallId = text(payload.tool_call_id);

    if (toolCallId !== null) {
      const group = open(`tool:${toolCallId}`);
      group.events.push(event);
      const toolName = text(payload.tool_name);
      if (toolName !== null && group.toolName === null) {
        group.toolName = toolName;
        group.subject = subjectOf(payload);
      }
      // A write says what it wrote only after it has written it.
      //
      // `ToolProposed.argument_preview` is truncated, and `workspace_write`
      // sends `{"content": "<!DOCTYPE html>…"}` with the whole file first --
      // so the `path` key is past the cut in every real call, and the row read
      // 写入工作区 with no filename on it. `ToolCompleted.workspace_writes`
      // carries the name, and carries it as fact rather than as intent.
      //
      // Only fills a hole; an argument-derived subject is never overwritten,
      // because that one is what the call *asked* for and stays true even if
      // the write later failed.
      if (group.subject === null) {
        group.subject = subjectFromResult(payload);
      }
      group.outcome = outcomeAfter(group.outcome, event, payload);
      group.gate = gateAfter(group.gate, event, payload);
      continue;
    }

    const modelCallId = text(payload.model_call_id);
    if (modelCallId !== null) {
      const key = `model:${modelCallId}`;
      const group = open(key);
      group.events.push(event);
      if (event.event_type === "ModelCompleted") {
        // A turn that came back is done. Without this a finished model step
        // kept the "running" it opened with, so a settled Task showed a column
        // of 进行中 beside steps that had plainly ended.
        group.outcome = "ok";
        const said = text(payload.text);
        if (said !== null) group.modelText = said;
        else {
          // A turn that only called tools. Filed under the first call it named
          // -- "first" because a turn naming two tools produces two rows either
          // way, and putting the turn on the earlier of them keeps it ahead of
          // the work it caused.
          const named = firstToolCallId(payload.tool_call_ids);
          if (named !== null) modelTurnHome.set(key, `tool:${named}`);
        }
      }
      continue;
    }

    const group = open(`solo:${event.event_id}`);
    group.events.push(event);
    group.outcome = "ok";
  }

  for (const [from, into] of modelTurnHome) {
    const source = groups.get(from);
    const target = groups.get(into);
    // Only when the destination exists. A turn whose tool call never reached
    // this page -- a truncated timeline, a run still in flight -- keeps its own
    // row instead of vanishing into a group that is not here.
    if (source === undefined || target === undefined) continue;
    target.events = [...source.events, ...target.events];
    groups.delete(from);
  }

  return order
    .filter((key) => groups.has(key))
    .map((key) => {
      const group = groups.get(key) as Building;
      return {
        key,
        title: titleOf(group, titleFor),
        subject: group.subject,
        outcome: group.outcome,
        gate: group.gate === null ? null : phraseGate(group.gate),
        events: group.events,
      };
    });
}

function titleOf(
  group: Building,
  titleFor?: (event: EventEnvelope) => string,
): string {
  if (group.toolName !== null) {
    return TOOL_VERBS[group.toolName] ?? group.toolName;
  }
  if (group.modelText !== null) return "模型作答";
  const first = group.events[0];
  if (first === undefined) return "步骤";
  // The raw type is the last resort, not the first. A pane that showed
  // `RunStarted` beside 写入工作区 is half translated, which reads as a bug in
  // the half that is not.
  return titleFor?.(first) ?? first.event_type;
}

/**
 * How the step ended so far, refined by each event rather than read off the
 * last one.
 *
 * A denial and a failure both arrive on a tool call that fails -- the policy
 * says no, and the call then fails *because* it said no. Reporting the failure
 * would describe the consequence and hide the cause, so a denial once seen
 * stays.
 */
function outcomeAfter(
  held: StepOutcome,
  event: EventEnvelope,
  payload: Record<string, unknown>,
): StepOutcome {
  if (held === "denied") return held;
  switch (event.event_type) {
    case "PermissionResolved":
      return text(payload.effect) === "deny" ? "denied" : held;
    case "ToolCompleted":
      return "ok";
    case "ToolFailed":
      return "failed";
    default:
      return held;
  }
}

/**
 * The gate facts, refined by each event of a tool call.
 *
 * Only `PermissionResolved` and `ToolApprovalDecided` can answer the middle
 * bead, and they can disagree: the policy engine allows a call and the human it
 * then asked says no. A denial from either is sticky for the same reason it is
 * sticky in `outcomeAfter` -- the refusal that follows describes the
 * consequence, and a reader needs the cause.
 *
 * `PermissionResolved` can also arrive more than once: the gateway loops while
 * a policy hook rewrites the arguments, emitting one per round. Accumulating
 * rather than overwriting is what makes that harmless -- a later allow cannot
 * un-say an earlier deny, which is the shape of a rewrite carrying a call past
 * its own refusal.
 */
function gateAfter(
  held: GateFacts | null,
  event: EventEnvelope,
  payload: Record<string, unknown>,
): GateFacts {
  const facts: GateFacts = held ?? {
    proposed: false,
    allowed: false,
    denied: false,
    started: false,
    completed: false,
    failed: false,
  };
  switch (event.event_type) {
    case "ToolProposed":
      return { ...facts, proposed: true };
    case "PermissionResolved": {
      const effect = text(payload.effect);
      if (effect === "deny") return { ...facts, denied: true };
      return { ...facts, allowed: true };
    }
    case "ToolApprovalDecided":
      // `deny` is the only decision that stops the call. A timeout is recorded
      // here too, as a decision nobody made -- it leaves the bead pending,
      // which is what "nobody answered" looks like.
      return text(payload.decision) === "deny"
        ? { ...facts, denied: true }
        : { ...facts, allowed: true };
    case "ToolStarted":
      return { ...facts, started: true };
    case "ToolCompleted":
      return { ...facts, completed: true };
    case "ToolFailed":
      return { ...facts, failed: true };
    default:
      return facts;
  }
}

/** The four beads, in order, as a reader reads them. */
function phraseGate(facts: GateFacts): GateStep[] {
  return [
    {
      key: "proposed",
      label: "提议",
      state: facts.proposed ? "done" : "pending",
    },
    {
      key: "authorized",
      label: facts.denied ? "被拒" : "授权",
      state: facts.denied ? "denied" : facts.allowed ? "done" : "pending",
    },
    {
      key: "started",
      label: "开始",
      state: facts.started ? "done" : "pending",
    },
    {
      key: "finished",
      label: "完成",
      state: facts.failed ? "failed" : facts.completed ? "done" : "pending",
    },
  ];
}

function subjectOf(payload: Record<string, unknown>): string | null {
  const preview = payload.argument_preview;
  if (typeof preview !== "string" || preview === "") return null;
  let parsed: unknown;
  try {
    parsed = JSON.parse(preview);
  } catch {
    return null;
  }
  if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
    return null;
  }
  const fields = parsed as Record<string, unknown>;
  for (const key of SUBJECT_KEYS) {
    const fitted = fit(fields[key]);
    if (fitted !== null) return fitted;
  }
  for (const key of SUBJECT_LIST_KEYS) {
    const value = fields[key];
    if (!Array.isArray(value)) continue;
    // First entry only. A call over three files is still one row, and three
    // names in a 56-character slot is a row nobody finishes reading.
    for (const entry of value) {
      const fitted = fit(entry);
      if (fitted !== null) return fitted;
    }
  }
  return null;
}

/**
 * The subject a completed call proves, for the calls whose arguments hid it.
 *
 * Reads `workspace_writes`, which `ToolCompleted` carries as the list of names
 * the call actually wrote. See the caller for why the arguments cannot answer.
 */
function subjectFromResult(payload: Record<string, unknown>): string | null {
  const writes = payload.workspace_writes;
  if (!Array.isArray(writes)) return null;
  for (const entry of writes) {
    const fitted = fit(entry);
    if (fitted !== null) return fitted;
  }
  return null;
}

/** One subject, whitespace collapsed and capped, or null if it is not one. */
function fit(value: unknown): string | null {
  if (typeof value !== "string") return null;
  const collapsed = value.replace(/\s+/g, " ").trim();
  if (collapsed === "") return null;
  return collapsed.length <= MAX_SUBJECT_CHARS
    ? collapsed
    : `${collapsed.slice(0, MAX_SUBJECT_CHARS - 1)}…`;
}

function firstToolCallId(value: unknown): string | null {
  if (!Array.isArray(value)) return null;
  for (const entry of value) {
    const id = text(entry);
    if (id !== null) return id;
  }
  return null;
}

function text(value: unknown): string | null {
  if (typeof value !== "string") return null;
  const trimmed = value.trim();
  return trimmed === "" ? null : trimmed;
}

/**
 * What a collapsed stage did, in one line.
 *
 * A stage that says "16 步" reports a quantity where a reader wants a fact. It
 * is also the only thing on screen until they click: a finished run collapses
 * to one row per stage, so "collect evidence, 16 steps" is the entire account
 * of sixteen actions. Naming the actions -- "搜索网络 ×3 · 读取网页 ×12" -- costs
 * the same line and answers the question the count only measures.
 *
 * Ordered by count, then by first appearance, so the line is stable across
 * polls and the biggest thing leads. Truncated at three kinds, because past
 * that it stops being a line and starts being the list it is standing in for.
 */
export function summariseGroups(groups: readonly StepGroup[]): string {
  const counts = new Map<string, number>();
  for (const group of groups) {
    counts.set(group.title, (counts.get(group.title) ?? 0) + 1);
  }
  const kinds = [...counts.entries()];
  if (kinds.length === 0) return "";

  const ranked = kinds
    .map(([title, count], index) => ({ title, count, index }))
    .sort((left, right) =>
      left.count === right.count
        ? left.index - right.index
        : right.count - left.count,
    );
  const shown = ranked.slice(0, 3);
  // `×1` says nothing a title does not, and a row of them reads like a table.
  const parts = shown.map(({ title, count }) =>
    count === 1 ? title : `${title} ×${String(count)}`,
  );
  const hidden = ranked.length - shown.length;
  if (hidden > 0) parts.push(`等 ${String(hidden)} 项`);
  return parts.join(" · ");
}
