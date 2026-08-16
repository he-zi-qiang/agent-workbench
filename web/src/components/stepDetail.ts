import type { ArtifactRef, EventEnvelope } from "../api/types";
import { formatDateTime } from "./ui";

/**
 * What one timeline event actually says, unpacked into things a reader can
 * look at. The raw payload stays available underneath: this decides what is
 * worth surfacing, it does not decide what is allowed to be seen.
 */
export interface StepFact {
  label: string;
  value: string;
  /** Long, monospace-worthy values get their own line rather than a column. */
  wide?: boolean;
}

export interface StepBody {
  label: string;
  text: string;
  /** Pretty-printed when the producer emitted JSON, so a plan reads as a plan. */
  format: "json" | "text";
}

export interface StepDetail {
  /** Shown collapsed, next to the title. Null when the title says it all. */
  summary: string | null;
  facts: StepFact[];
  /**
   * Verbatim text belonging to this step, in reading order. A model call has
   * two -- what it was given and what it wrote -- and showing only one of them
   * answers half the question a reader opened the step to ask.
   */
  bodies: StepBody[];
  /** Real content stored outside the event, openable through the artifact API. */
  artifact: ArtifactRef | null;
}

const RISK_LABELS: Readonly<Record<string, string>> = {
  read: "只读",
  write: "写入",
  external: "访问外部",
  destructive: "破坏性",
};

const RUN_KIND_LABELS: Readonly<Record<string, string>> = {
  chat: "对话回答",
  task: "任务执行",
  code: "编码会话",
};

const FINISH_LABELS: Readonly<Record<string, string>> = {
  stop: "正常结束",
  length: "达到长度上限",
  tool_use: "请求调用工具",
  error: "出错",
  cancelled: "已取消",
};

const EFFECT_LABELS: Readonly<Record<string, string>> = {
  allow: "允许",
  deny: "拒绝",
};

export function describeEvent(event: EventEnvelope): StepDetail {
  const payload = event.payload;
  const detail: StepDetail = { summary: null, facts: [], bodies: [], artifact: null };

  switch (event.event_type) {
    case "RunStarted": {
      const tools = stringArray(payload.tool_names);
      const budget = record(payload.budget);
      const deadline = wallClock(budget?.deadline);
      detail.summary = tools.length === 0 ? "没有可用工具" : `可用工具 ${tools.length} 个`;
      detail.facts = [
        fact("运行类型", label(RUN_KIND_LABELS, payload.run_kind)),
        fact("模型档位", text(payload.model_profile)),
        // "no tools" is the fact that explains an answer with no tool calls,
        // so it is stated rather than left as an empty list.
        fact("可用工具", tools.length === 0 ? "无" : tools.join("、")),
        fact("最多步数", numberText(budget?.max_steps)),
        fact("最多工具调用", numberText(budget?.max_tool_calls)),
        // The other two ceilings that can stop a run (`token_budget` and
        // `deadline` in `domain/runs.py`), which no step or tool count
        // predicts. Both are optional and often unset -- Chat sets neither --
        // so an absent token ceiling is said out loud, while an absent
        // deadline drops its row rather than adding an empty one to every run.
        fact("token 上限", ceilingText(budget, "max_total_tokens")),
        ...(deadline === null ? [] : [fact("截止时间", deadline)]),
        // `max_cost_micro_usd` is left out deliberately: nothing under
        // `config/` sets it, and no model profile configures the rate table
        // its usage counterpart would be priced from. A cost row would show an
        // unset ceiling above a figure that is zero for reasons the reader
        // cannot see.
      ];
      return detail;
    }

    case "ContextBuilt":
      detail.summary = `${numberText(payload.chunk_count)} 个片段`;
      detail.facts = [
        fact("检索到的片段", numberText(payload.chunk_count)),
        fact("可引用来源", numberText(payload.citation_count)),
        fact("估算 token", numberText(payload.token_estimate)),
      ];
      return detail;

    case "ModelStarted":
      detail.summary = text(payload.model_id);
      detail.facts = [
        fact("模型", text(payload.model_id)),
        fact("档位", text(payload.model_profile)),
        fact("调用 ID", text(payload.model_call_id), true),
      ];
      detail.bodies = promptBodies(payload);
      return detail;

    case "ModelCompleted": {
      const usage = record(payload.usage);
      const calls = stringArray(payload.tool_call_ids);
      const output = text(payload.text);
      detail.summary = summarize(output) ?? label(FINISH_LABELS, payload.finish_reason);
      detail.facts = [
        fact("结束原因", label(FINISH_LABELS, payload.finish_reason)),
        fact("输入 token", numberText(usage?.input_tokens)),
        fact("输出 token", numberText(usage?.output_tokens)),
        ...(calls.length === 0 ? [] : [fact("提出的工具调用", String(calls.length))]),
      ];
      // The prompt first, because it is what the output is a response to. Chat
      // carries it forward onto this event, since one model call is one row
      // there and the completion replaces the start.
      detail.bodies = promptBodies(payload);
      // The model's own words. Absent when the turn only proposed tool calls,
      // and absent in Chat, where an unpublished candidate never reaches state.
      if (output !== "—") detail.bodies.push(bodyOf("模型输出", output));
      detail.artifact = artifactRef(payload.output_ref);
      return detail;
    }

    case "ToolProposed": {
      const bytes = numberOf(payload.argument_bytes);
      // What the call was for, not its name -- the row's title is already the
      // tool name, and repeating it spends the summary saying nothing.
      detail.summary =
        salientArgument(payload) ?? (bytes === null ? null : `${bytes} 字节参数`);
      detail.facts = [
        fact("工具", text(payload.tool_name)),
        fact("风险等级", label(RISK_LABELS, payload.risk)),
        fact("参数大小", bytes === null ? "—" : `${bytes} 字节`),
        // The digest, because the arguments themselves are not in the event.
        // Shown so two proposals of the same call are comparable.
        fact("参数摘要", shortDigest(payload.argument_sha256), true),
      ];
      detail.bodies = argumentBodies(payload);
      return detail;
    }

    case "PermissionRequested":
      detail.facts = [
        fact("需要权限", stringArray(payload.required_scopes).join("、") || "—"),
        fact("风险等级", label(RISK_LABELS, payload.risk)),
      ];
      return detail;

    case "PermissionResolved": {
      const effect = text(payload.effect);
      detail.summary = label(EFFECT_LABELS, payload.effect);
      detail.facts = [
        fact("结果", label(EFFECT_LABELS, payload.effect)),
        // The reason code is the whole point of a denial: "denied" alone
        // leaves the reader unable to tell a policy gap from a bug.
        fact("原因", text(payload.reason_code)),
        ...(effect === "deny" ? [] : [fact("工具调用 ID", text(payload.tool_call_id), true)]),
      ];
      return detail;
    }

    case "ToolStarted":
      detail.summary = text(payload.tool_name);
      detail.facts = [fact("工具", text(payload.tool_name))];
      return detail;

    case "ToolCompleted": {
      const duration = numberOf(payload.duration_ms);
      detail.summary = duration === null ? null : `${duration} ms`;
      detail.facts = [
        fact("耗时", duration === null ? "—" : `${duration} ms`),
        fact("输出大小", bytesText(payload.output_bytes)),
        ...(payload.truncated === true ? [fact("输出被截断", "是")] : []),
      ];
      // What it actually returned, when the deployment records step inputs.
      // Until this existed an opened step could say a tool ran for 40ms and
      // returned 4 kilobytes and still not say what it found -- and for the
      // five workspace tools there is no artifact either, so the size was all
      // there was. The reader opening a step wants the answer, not a receipt.
      detail.bodies = outputBodies(payload);
      detail.artifact = artifactRef(payload.artifact);
      return detail;
    }

    case "ToolFailed": {
      const error = record(payload.error);
      const message = text(error?.message);
      // The message, falling back to the code. One code covers several
      // situations -- `provider_unavailable` is both "no provider configured"
      // and "found 5 pages and could read none of them" -- so the code alone
      // sends the reader to the wrong fix. Measured: a proxy that refused every
      // fetch read on screen as `provider_unavailable`, indistinguishable from
      // a deployment that never had web search at all.
      detail.summary = message === "—" ? text(error?.code) : summarize(message);
      detail.facts = [
        fact("错误码", text(error?.code)),
        // Three states, not two: an absent `retryable` means the event did not
        // say, and folding that into 否 would answer a question the server
        // never answered. Every other unknown in this file renders 「—」.
        fact("可以重试", retryableText(error?.retryable)),
      ];
      if (message !== "—") detail.bodies.push(bodyOf("错误信息", message));
      return detail;
    }

    case "RunCompleted": {
      detail.summary = label(
        { completed: "正常完成" },
        payload.stop_reason,
      );
      detail.facts = [
        fact("停止原因", text(payload.stop_reason)),
        ...usageFacts(payload),
      ];
      return detail;
    }

    case "RunFailed": {
      const error = record(payload.error);
      const message = text(error?.message);
      detail.summary = message === "—" ? text(error?.code) : summarize(message);
      detail.facts = [
        fact("停止原因", text(payload.stop_reason)),
        fact("错误码", text(error?.code)),
        // Three states, not two: an absent `retryable` means the event did not
        // say, and folding that into 否 would answer a question the server
        // never answered. Every other unknown in this file renders 「—」.
        fact("可以重试", retryableText(error?.retryable)),
        // A failed run still spent what it spent, and the same usage is on the
        // event. Reading it only on the runs that succeeded would understate
        // exactly the runs worth asking about.
        ...usageFacts(payload),
      ];
      if (message !== "—") detail.bodies.push(bodyOf("错误信息", message));
      return detail;
    }

    case "TaskSubmitted":
      detail.facts = [
        fact("输入 artifact", text(payload.input_ref), true),
        fact("流程版本", text(payload.graph_version)),
      ];
      return detail;

    case "TaskApprovalRequested":
      detail.facts = [
        fact("审批 ID", text(payload.approval_id), true),
        fact("审批环节", text(payload.graph_node_operation_id)),
      ];
      return detail;

    case "TaskApprovalDecided":
      detail.summary = text(payload.decision);
      detail.facts = [
        fact("决定", text(payload.decision)),
        fact("决定版本", numberText(payload.decision_version)),
        fact("审批 ID", text(payload.approval_id), true),
      ];
      return detail;

    case "TaskClaimed":
    case "TaskAwaitingApproval":
    case "TaskSucceeded":
    case "TaskFailed":
    case "TaskCancelled":
      detail.facts = [
        fact("状态", text(payload.status)),
        fact("第几次执行", numberText(payload.attempt)),
        fact("租约世代", numberText(payload.epoch)),
      ];
      return detail;

    default:
      return detail;
  }
}

/**
 * The prompt behind a model call, when the server recorded one. Absent unless
 * `observability.capture_model_prompts` is enabled, so the UI treats it as an
 * optional extra rather than assuming every deployment stores it.
 */
function promptBodies(payload: Record<string, unknown>): StepBody[] {
  const prompt = text(payload.prompt_preview);
  return prompt === "—" ? [] : [bodyOf("发给模型的提示词", prompt)];
}

/**
 * What a finished run spent, from the `BudgetUsage` its end event carries.
 *
 * Shared by the end events because they carry the same record, and "how much
 * did this run use" should not be answered differently depending on whether it
 * finished or failed.
 *
 * Cost is not among these. `usage.cost_micro_usd` is priced from a rate table
 * that none of the configs under `config/` sets up, so it stays zero -- a row
 * that would read as "this was free" rather than "this was never priced".
 */
function usageFacts(payload: Record<string, unknown>): StepFact[] {
  const usage = record(payload.usage);
  const tokens = record(usage?.tokens);
  const cacheRead = numberOf(tokens?.cache_read_tokens) ?? 0;
  const cacheWrite = numberOf(tokens?.cache_write_tokens) ?? 0;
  return [
    fact("步数", numberText(usage?.steps)),
    fact("工具调用", numberText(usage?.tool_calls)),
    fact("输入 token", numberText(tokens?.input_tokens)),
    // Worded as a part of the prompt above, because that is what it is: the
    // cached share is already inside `input_tokens`, and a label that read like
    // a separate stream would invite adding it twice.
    ...(cacheRead === 0 ? [] : [fact("其中缓存命中", String(cacheRead))]),
    fact("输出 token", numberText(tokens?.output_tokens)),
    ...(cacheWrite === 0 ? [] : [fact("缓存写入", String(cacheWrite))]),
    fact("总计 token", totalTokens(tokens)),
  ];
}

/**
 * Every token the run moved, counted the way the budget counts them.
 *
 * Not `input + output`: cache writes are reported outside the prompt, so the
 * two rows above can sum to less than the run spent. This is the figure
 * `max_total_tokens` is compared against (`TokenUsage.total`), and it is
 * recomputed here because that property is a Python `@property` and never
 * reaches the payload. Kept decomposed exactly as the domain decomposes it,
 * clamp included, so the number under the ceiling is the number that gated it.
 */
function totalTokens(tokens: Record<string, unknown> | null): string {
  if (tokens === null) return "—";
  const input = numberOf(tokens.input_tokens) ?? 0;
  const output = numberOf(tokens.output_tokens) ?? 0;
  const cacheRead = numberOf(tokens.cache_read_tokens) ?? 0;
  const cacheWrite = numberOf(tokens.cache_write_tokens) ?? 0;
  const uncachedInput = Math.max(0, input - cacheRead);
  return String(uncachedInput + cacheRead + output + cacheWrite);
}

/**
 * An optional budget ceiling. "Unset" is a real answer here rather than a
 * missing one -- the domain skips the check entirely when the field is null,
 * so the run genuinely has no such ceiling -- and only a payload with no
 * budget at all is unknown.
 */
function ceilingText(budget: Record<string, unknown> | null, key: string): string {
  if (budget === null) return "—";
  const parsed = numberOf(budget[key]);
  return parsed === null ? "未设上限" : String(parsed);
}

/**
 * An instant, as a clock a reader can compare against the step times beside
 * it. Null when there is nothing to show, so callers can drop the row instead
 * of printing a dash on the many runs that carry no deadline.
 */
function wallClock(value: unknown): string | null {
  return typeof value === "string" && value !== "" ? formatDateTime(value) : null;
}

/** Tool arguments, on the same opt-in terms as the prompt. */
function argumentBodies(payload: Record<string, unknown>): StepBody[] {
  const args = text(payload.argument_preview);
  return args === "—" ? [] : [bodyOf("调用参数", args)];
}

/**
 * The tool's own answer, and nothing when the deployment did not record one.
 *
 * Empty rather than a placeholder for a deployment with
 * `runtime.record_step_inputs` off: an empty preview there means "not
 * recorded", and rendering an empty box would read as "returned nothing",
 * which is a different and wrong fact.
 */
function outputBodies(payload: Record<string, unknown>): StepBody[] {
  const output = text(payload.output_preview);
  return output === "—" ? [] : [bodyOf("工具返回", output)];
}

function bodyOf(label: string, value: string): StepBody {
  const pretty = prettyJson(value);
  return pretty === null
    ? { label, text: value, format: "text" }
    : { label, text: pretty, format: "json" };
}

/**
 * Re-indent a JSON payload so a plan or a critic verdict reads as a structure.
 * Returns null for anything that is not a JSON object or array, so ordinary
 * prose is never mangled by a parse that happened to succeed -- `"42"` is
 * valid JSON and is not a document.
 */
function prettyJson(value: string): string | null {
  const trimmed = value.trim();
  if (!trimmed.startsWith("{") && !trimmed.startsWith("[")) return null;
  try {
    return JSON.stringify(JSON.parse(trimmed), null, 2);
  } catch {
    return null;
  }
}

/**
 * The one argument worth putting on a collapsed line.
 *
 * Not a renderer for arbitrary JSON -- opening the step already shows the
 * arguments faithfully. This answers "what was this call for" in the width of a
 * summary, from a small set of keys that carry a call's subject across this
 * system's tools. Nothing when none of them fit, rather than a truncated blob
 * of braces that reads as noise.
 */
const SALIENT_KEYS = ["query", "url", "question", "path", "name"] as const;

function salientArgument(payload: Record<string, unknown>): string | null {
  const preview = payload.argument_preview;
  if (typeof preview !== "string" || preview === "") return null;
  let parsed: unknown;
  try {
    parsed = JSON.parse(preview);
  } catch {
    return null;
  }
  const fields = record(parsed);
  if (fields === null) return null;
  for (const key of SALIENT_KEYS) {
    const value = fields[key];
    if (typeof value !== "string" || value.trim() === "") continue;
    return summarize(value);
  }
  return null;
}

function summarize(value: string): string | null {
  if (value === "—") return null;
  const collapsed = value.replace(/\s+/g, " ").trim();
  if (collapsed === "") return null;
  return collapsed.length <= 48 ? collapsed : `${collapsed.slice(0, 47)}…`;
}

function fact(label: string, value: string, wide = false): StepFact {
  return wide ? { label, value, wide } : { label, value };
}

function label(labels: Readonly<Record<string, string>>, value: unknown): string {
  const key = typeof value === "string" ? value : null;
  if (key === null) return "—";
  return labels[key] ?? key;
}

function text(value: unknown): string {
  return typeof value === "string" && value !== "" ? value : "—";
}

/** 是 / 否 / 没说 -- the third is not the second. */
function retryableText(value: unknown): string {
  if (value === true) return "是";
  if (value === false) return "否";
  return "—";
}

function numberOf(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function numberText(value: unknown): string {
  const parsed = numberOf(value);
  return parsed === null ? "—" : String(parsed);
}

function bytesText(value: unknown): string {
  const bytes = numberOf(value);
  if (bytes === null) return "—";
  if (bytes < 1024) return `${bytes} 字节`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

function shortDigest(value: unknown): string {
  const digest = text(value);
  return digest === "—" ? digest : `${digest.slice(0, 16)}…`;
}

function stringArray(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item) => typeof item === "string") : [];
}

function record(value: unknown): Record<string, unknown> | null {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function artifactRef(value: unknown): ArtifactRef | null {
  const source = record(value);
  if (source === null) return null;
  if (
    typeof source.artifact_id !== "string" ||
    source.artifact_id === "" ||
    typeof source.media_type !== "string" ||
    typeof source.size_bytes !== "number"
  ) {
    return null;
  }
  const artifact: ArtifactRef = {
    artifact_id: source.artifact_id,
    kind: typeof source.kind === "string" ? source.kind : "artifact",
    media_type: source.media_type,
    size_bytes: source.size_bytes,
    sha256: typeof source.sha256 === "string" ? source.sha256 : "",
  };
  if (typeof source.filename === "string") artifact.filename = source.filename;
  return artifact;
}
