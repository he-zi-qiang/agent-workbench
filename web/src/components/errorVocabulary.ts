/**
 * What this system's error codes and run-level failures mean, in words.
 *
 * Lives here rather than beside the Task page because two surfaces read it now:
 * `RunPanel` renders a failed run wherever the panel is shown, and Work's
 * `explainFailure` builds a Task's sentence from the same table. One
 * vocabulary, because the symptom of two is not an error -- it is one surface
 * saying 超出预算 while the one beside it says `budget_exceeded`.
 */

/**
 * The closed error vocabulary, in words.
 *
 * Shared rather than copied. A failed Task and a failed *run* inside it draw
 * their codes from the same `domain/errors.py::ErrorCode`, so a second table
 * would be a second place to forget a code -- and the symptom of forgetting is
 * not an error, it is one surface saying 超出预算 while the one beside it says
 * `budget_exceeded`.
 */
export const CODE_LABELS: Readonly<Record<string, string>> = {
  provider_error: "调用模型服务失败",
  provider_unavailable: "模型服务暂时不可用",
  provider_account_rejected: "模型服务拒绝了这个部署的账号",
  budget_exceeded: "超出了这次任务的步数或 token 预算",
  tool_timeout: "工具执行超时",
  tool_failed: "工具执行失败",
  policy_denied: "被权限策略拒绝",
  approval_required: "需要人工批准才能继续",
  output_too_large: "产出内容超出上限",
  invalid_tool_input: "工具参数不合法",
  unknown_tool: "调用了这个部署没有注册的工具",
  not_found: "需要的资源不存在",
  stale_execution: "这次执行已被更新的执行接管",
  incompatible_schema: "数据结构版本不兼容",
  cancelled: "已被取消",
  internal_error: "内部错误",
};

/**
 * Codes whose remedy is neither of the two this file otherwise offers.
 *
 * The default pair splits on `retryable`: try again, or change the task. Both
 * are wrong for `provider_account_rejected` (ADR-0084) -- it is not retryable,
 * and nothing about the task or this deployment's config is what is broken.
 * Sending a reader to re-read their own YAML when their account has no credit
 * left is the same misdirection the code was split out of `provider_error` to
 * end, so the sentence has to move with it.
 */
export const CODE_REMEDIES: Readonly<Record<string, string>> = {
  provider_account_rejected:
    "重试没有用，要先去模型服务商那边充值或者换一把密钥。",
};

/**
 * One `ErrorCode` in words, or the code itself.
 *
 * Returned verbatim when unrecognised, on the same reasoning as
 * `explainFailure`: a code this table has not learned yet is still the most
 * specific thing anybody has, and replacing it with 未知原因 would throw away
 * the one token an operator could grep the log for.
 */
export function errorCodeLabel(code: string): string {
  return CODE_LABELS[code] ?? code;
}

/**
 * Why one *run* stopped, in words, from `RunFailed.error.message`.
 *
 * A sibling of `explainFailure`, which explains a whole Task's `status_detail`.
 * Both read a closed vocabulary of stable English sentences the server writes;
 * this one covers the sentences a single run ends on.
 *
 * **Why the panel needs it rather than showing the message raw.** A row read
 * `超出了这次任务的步数或 token 预算：the model stopped at its output token
 * ceiling` beside the figures `17.2k/30.0k` -- a code label vague enough to
 * cover two different ceilings, an untranslated tail carrying the only precise
 * part, and a fraction that is not full. Three fragments the reader has to
 * reconcile, and the apparent contradiction was the wrong reading: the run did
 * not exhaust the 30k total it is shown against, it hit `max_output_tokens`,
 * which is a different ceiling and one this panel cannot show at all
 * (`RunStarted.budget` does not carry it). Naming the ceiling precisely is what
 * dissolves that -- once the sentence says *which* limit, the figure beside it
 * stops looking like a contradiction and becomes what it is: an unrelated
 * number that happens to be nearby.
 */
const RUN_FAILURE_LABELS: Readonly<Record<string, string>> = {
  "the model stopped at its output token ceiling":
    "模型写满了「单次回答」的输出上限（max_output_tokens）而停下——不是这个运行的 token 总额用完了，是它一次话说得太长。",
};

/** `the run passed its ceiling: <stop_reason>` */
const RUN_CEILING = /^the run passed its ceiling: (\w+)$/;
/** `the model call exceeded the runtime's <N>s envelope` */
const MODEL_ENVELOPE = /^the model call exceeded the runtime's ([\d.]+)s envelope$/;
/** `sub-agent <name> ended as failed (<reason>)` */
const SUB_AGENT_FAILED = /^sub-agent (\S+) ended as failed \((\w+)\)$/;

const CEILING_NAMES: Readonly<Record<string, string>> = {
  token_budget: "token 预算",
  cost_budget: "费用预算",
  max_steps: "步数",
  max_tool_calls: "工具调用次数",
  deadline: "时限",
};

/**
 * One run's failure in words, or `null` when this table has not learned it.
 *
 * `null` rather than a guess: the caller falls back to showing the server's
 * own sentence, which is the most specific thing anybody has for a message
 * nobody has written a phrase for yet -- the same rule `explainFailure` keeps.
 */
/**
 * A space before a phrase that starts with Latin, and none before one that
 * does not.
 *
 * `用完了它自己的token 预算` sets the Latin word hard against the CJK glyph
 * before it; `用完了它自己的步数` needs no space at all. Rather than baking a
 * leading space into some table entries and not others -- where it is invisible
 * in review and lost by the first person who edits the table -- the rule lives
 * here, once.
 */
function spaced(phrase: string): string {
  return /^[A-Za-z0-9]/.test(phrase) ? ` ${phrase}` : phrase;
}

export function explainRunFailure(message: string): string | null {
  const trimmed = message.trim();

  const known = RUN_FAILURE_LABELS[trimmed];
  if (known !== undefined) return known;

  const ceiling = RUN_CEILING.exec(trimmed);
  if (ceiling !== null) {
    const reason = ceiling[1] ?? "";
    return `这次运行用完了它自己的${spaced(CEILING_NAMES[reason] ?? reason)}。`;
  }

  const envelope = MODEL_ENVELOPE.exec(trimmed);
  if (envelope !== null) {
    return `一次模型调用超过了运行时给单次调用的 ${envelope[1]} 秒上限。`;
  }

  const sub = SUB_AGENT_FAILED.exec(trimmed);
  if (sub !== null) {
    const reason = sub[2] ?? "";
    return `子代理 ${sub[1]} 没跑完（${CEILING_NAMES[reason] ?? reason}）。`;
  }

  return null;
}
