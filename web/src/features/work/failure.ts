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

const CODE_LABELS: Readonly<Record<string, string>> = {
  provider_error: "调用模型服务失败",
  provider_unavailable: "模型服务暂时不可用",
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

export interface FailureExplanation {
  text: string;
  /** True when the server classified the cause as worth another attempt. */
  retryable: boolean;
}

export function explainFailure(detail: string | null): FailureExplanation | null {
  if (detail === null || detail.trim() === "") return null;

  const failed = STEP_FAILURE.exec(detail);
  if (failed !== null) {
    const [, step, code, retryable] = failed;
    const stepLabel = STEP_LABELS[step ?? ""] ?? step ?? "某一步";
    const codeLabel = CODE_LABELS[code ?? ""] ?? code ?? "未知原因";
    const isRetryable = retryable === "retryable";
    return {
      text: isRetryable
        ? `在“${stepLabel}”这一步${codeLabel}。这类问题通常是暂时的，重新提交一次多半就好了。`
        : `在“${stepLabel}”这一步${codeLabel}。重试大概率还是同样的结果，需要先改动任务或配置。`,
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
