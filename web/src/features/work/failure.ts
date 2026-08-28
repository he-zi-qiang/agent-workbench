/**
 * What a failed Task's `status_detail` means, in words.
 *
 * The server writes a stable English sentence built from a closed error
 * vocabulary -- it is the same string operators read in the event log, and it
 * deliberately never quotes the provider. This turns the part a reader cares
 * about into plain language and, where it matters, says whether running the
 * Task again could plausibly work.
 *
 * Unrecognised details are shown verbatim rather than replaced. A detail this
 * table has not learned yet is still the most specific thing anyone has.
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
const CODE_LABELS: Readonly<Record<string, string>> = {
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

const STEP_LABELS: Readonly<Record<string, string>> = {
  understand: "理解目标",
  plan: "制定计划",
  research_internal: "检索内部资料",
  research_external: "检索外部资料",
  synthesize: "撰写草稿",
  critic: "检查草稿",
  export: "生成报告",
};

/** `the <step> step failed with <code> (retryable|not retryable) during <action>` */
const STEP_FAILURE =
  /^the (\w+) step failed with (\w+) \((retryable|not retryable)\) during/;
/** `the <step> step did not produce usable output during <action>` */
const STEP_EMPTY = /^the (\w+) step did not produce usable output during/;

/**
 * Whole-sentence details, which the two regexes above cannot match because
 * they are not step-shaped. Both graphs write their own wording for the same
 * two endings, so both spellings are listed rather than folded into one
 * pattern -- a router's sentence is not a format, and matching it loosely is
 * how the wrong ending gets the wrong explanation.
 *
 * The rejection entries stay after the export gate was made to honour
 * `export_requires_approval` in v1 too: a deployment that turns the gate back
 * on still reaches them, and every Task that already failed this way keeps its
 * history readable.
 */
const WHOLE_DETAIL_LABELS: Readonly<Record<string, string>> = {
  "a human rejected the approval required before export":
    "你拒绝了这次导出，任务在生成文件前停下了。",
  "export was rejected by a reviewer":
    "审阅者拒绝了这次导出，任务在生成文件前停下了。",
  "the critic requested another revision after the revision budget was exhausted":
    "检查环节仍要求修改，但这次任务的修改次数已经用完了。",
};

/** `review still requires changes after <n> revisions of the work node` */
const REVIEW_EXHAUSTED = /^review still requires changes after (\d+) revisions/;

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
const CODE_REMEDIES: Readonly<Record<string, string>> = {
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

export interface FailureExplanation {
  text: string;
  /** True when the server classified the cause as worth another attempt. */
  retryable: boolean;
}

export function explainFailure(detail: string | null): FailureExplanation | null {
  if (detail === null || detail.trim() === "") return null;

  const whole = WHOLE_DETAIL_LABELS[detail.trim()];
  if (whole !== undefined) return { text: whole, retryable: false };

  const reviewExhausted = REVIEW_EXHAUSTED.exec(detail);
  if (reviewExhausted !== null) {
    return {
      text: `审阅环节仍要求修改，但这次任务的 ${reviewExhausted[1]} 次修改机会已经用完了。`,
      retryable: false,
    };
  }

  const failed = STEP_FAILURE.exec(detail);
  if (failed !== null) {
    const [, step, code, retryable] = failed;
    const stepLabel = STEP_LABELS[step ?? ""] ?? step ?? "某一步";
    const codeLabel = CODE_LABELS[code ?? ""] ?? code ?? "未知原因";
    const isRetryable = retryable === "retryable";
    const remedy =
      CODE_REMEDIES[code ?? ""] ??
      (isRetryable
        ? "这类问题通常是暂时的，重新提交一次多半就好了。"
        : "重试大概率还是同样的结果，需要先改动任务或配置。");
    return {
      text: `在“${stepLabel}”这一步${codeLabel}。${remedy}`,
      retryable: isRetryable,
    };
  }

  const empty = STEP_EMPTY.exec(detail);
  if (empty !== null) {
    const step = empty[1] ?? "";
    return {
      text: `“${STEP_LABELS[step] ?? step}”这一步没有产出可用内容，任务无法继续。`,
      retryable: false,
    };
  }

  return { text: detail, retryable: false };
}
