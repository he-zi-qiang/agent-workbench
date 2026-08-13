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
  workspace_read: "读取工作区",
  workspace_list: "查看工作区",
  workspace_grep: "搜索工作区",
  export_artifact: "导出报告",
  sandbox_run: "运行代码",
};

/** The keys that carry a call's subject, in the order they are preferred. */
const SUBJECT_KEYS = ["query", "url", "name", "path", "question"] as const;

const MAX_SUBJECT_CHARS = 56;

interface Building {
  key: string;
  events: EventEnvelope[];
  /** Set once the group's identity is known, so late events cannot rename it. */
  toolName: string | null;
  subject: string | null;
  outcome: StepOutcome;
  modelText: string | null;
}

export function groupSteps(events: readonly EventEnvelope[]): StepGroup[] {
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
      group.outcome = outcomeAfter(group.outcome, event, payload);
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
        title: titleOf(group),
        subject: group.subject,
        outcome: group.outcome,
        events: group.events,
      };
    });
}

function titleOf(group: Building): string {
  if (group.toolName !== null) {
    return TOOL_VERBS[group.toolName] ?? group.toolName;
  }
  if (group.modelText !== null) return "模型作答";
  const first = group.events[0];
  return first === undefined ? "步骤" : first.event_type;
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
    const value = fields[key];
    if (typeof value !== "string") continue;
    const collapsed = value.replace(/\s+/g, " ").trim();
    if (collapsed === "") continue;
    return collapsed.length <= MAX_SUBJECT_CHARS
      ? collapsed
      : `${collapsed.slice(0, MAX_SUBJECT_CHARS - 1)}…`;
  }
  return null;
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
