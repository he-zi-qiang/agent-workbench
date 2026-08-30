import {
  Check,
  CircleDashed,
  LoaderCircle,
  PauseCircle,
  Slash,
  X,
} from "lucide-react";

import type { RunStatus } from "./runTree";

/**
 * 一次运行怎么说话：状态、此刻在做什么、为什么停、花了多少。
 *
 * 这些常量和格式化函数原来长在 `RunPanel` 里，那时它们只有一个读者。现在有两个
 * ——`RunPanel` 画的是 Code 会话里那棵树，`features/work/AgentPanel` 画的是 Task
 * 右边那块副面板——而两个面对同一个 `RunStatus` 必须说同一个词。
 *
 * 抽出来而不是抄一份，理由就是上面那句。抄一份的代价不会立刻显现：两边今天都把
 * `cancelled` 念成「已取消」，直到某一天有人在其中一处改成「已中止」，另一处不会
 * 跟着变，也不会有任何测试红。这个仓库里已经有一处**故意**重复的
 * `formatTokens`（`DelegationScope` 那一份），它旁边写着为什么它不该合并——一个
 * 格式化配置常量，和一个每两秒变一次的花销，不为同一件事负责。这一份不是那种
 * 情况：两个面格式化的是同一个数，同一个来源。
 */

export const STATUS_LABEL: Readonly<Record<RunStatus, string>> = {
  running: "进行中",
  completed: "已完成",
  failed: "失败",
  cancelled: "已取消",
  unknown: "等待中",
};

/**
 * What each row shows while it is doing something.
 *
 * Only the verbs a reader would recognise. An event type with no entry falls
 * back to nothing rather than to its own name: `ToolProposed` on a row would
 * tell somebody watching a Task exactly as much as a raw id does.
 */
export const ACTIVITY_LABEL: Readonly<Record<string, string>> = {
  RunStarted: "刚开始",
  ContextBuilt: "正在准备上下文",
  ModelStarted: "正在作答",
  ModelCompleted: "作答完成",
  ToolStarted: "正在调用工具",
  ToolCompleted: "工具已返回",
  ToolFailed: "工具失败",
  ToolProgress: "工具进行中",
  // A tool waiting on a person is the one activity a spinner alone actively
  // misleads about: nothing is being computed, and the thing that will unblock
  // it is the reader.
  PermissionRequested: "正在等你授权",
  ToolApprovalDecided: "授权已裁定",
  AgentDelegated: "正在等子代理",
  AgentCompleted: "子代理已回来",
  ContextCompacted: "已压缩上下文",
};

/** `RunPaused.reason` is a closed set of two (`domain/events.py:112`). */
export const PAUSE_LABEL: Readonly<Record<string, string>> = {
  approval: "已暂停，等待你确认",
  migration: "已暂停，等待迁移",
};

/**
 * The stop reasons worth putting on a row, and nothing else.
 *
 * A run that simply finished says `stop`, and repeating that beside 已完成 is
 * noise. The entries here are the ones that change what the reader should do
 * next -- a run stopped by a ceiling is not the same event as a run that
 * answered, even though both end `completed`.
 */
export const STOP_REASON_LABEL: Readonly<Record<string, string>> = {
  max_steps: "步数用尽",
  max_tool_calls: "工具调用次数用尽",
  token_budget: "token 预算用尽",
  cost_budget: "费用预算用尽",
  deadline: "超时",
  cancelled: "被取消",
};

export function StatusIcon({
  status,
  paused,
}: {
  status: RunStatus;
  paused: boolean;
}) {
  const size = 14;
  // Checked before `running`, because a paused run *is* running and a spinner
  // on it says the opposite of what is true: nothing is turning, and the thing
  // that will move it is a person.
  if (paused) return <PauseCircle aria-hidden="true" size={size} strokeWidth={2.2} />;
  if (status === "running")
    return (
      <LoaderCircle
        aria-hidden="true"
        className="aw-spin"
        size={size}
        strokeWidth={2.2}
      />
    );
  if (status === "completed")
    return <Check aria-hidden="true" size={size} strokeWidth={2.6} />;
  if (status === "failed")
    return <X aria-hidden="true" size={size} strokeWidth={2.6} />;
  if (status === "cancelled")
    return <Slash aria-hidden="true" size={size} strokeWidth={2.2} />;
  return <CircleDashed aria-hidden="true" size={size} strokeWidth={2} />;
}

export function formatTokens(value: number): string {
  if (value < 1000) return String(value);
  return `${(value / 1000).toFixed(1)}k`;
}

/**
 * A run's span, at the coarsest resolution that is still true.
 *
 * Seconds below a minute and whole minutes above it, with no third tier: an
 * hour-long research Task reported as `1h03m` and one reported as `63 分钟`
 * lead to the same next action, and the first spends a reader's attention on
 * arithmetic. Under a second is dropped rather than rounded to `0 秒`, which
 * would read as a run that did nothing.
 */
export function formatDuration(ms: number): string | null {
  if (ms < 1000) return null;
  const seconds = Math.round(ms / 1000);
  if (seconds < 60) return `${String(seconds)} 秒`;
  return `${String(Math.round(seconds / 60))} 分钟`;
}

/**
 * `spent/ceiling`, or just `spent` where the run declared no ceiling.
 *
 * Never `spent/0` and never a percentage of nothing: `max_total_tokens` is
 * `None` in every profile this repository ships except the delegated one, so
 * the no-ceiling case is the common one and has to read as an ordinary number
 * rather than as a missing denominator.
 */
export function formatAgainstCeiling(
  spent: number,
  ceiling: number | null,
  format: (value: number) => string,
): string {
  if (ceiling === null || ceiling <= 0) return format(spent);
  return `${format(spent)}/${format(ceiling)}`;
}

/** How full a ceiling is, or `null` where there is nothing to be full of. */
export function fractionOf(spent: number, ceiling: number | null): number | null {
  if (ceiling === null || ceiling <= 0) return null;
  return Math.min(1, spent / ceiling);
}
