import type { EventEnvelope } from "../api/types";

/**
 * 运行层事件的中文名——三个模式共用的那一份。
 *
 * 这些事件（RunStarted、ModelCompleted、ToolProposed……）由同一个运行时发出，
 * Chat、Task、Code 都会收到。此前只有 Work 有一张表（`workTimeline.ts`），Code 的
 * 「原始事件」直接把整轮 `JSON.stringify` 出来，一个类型名都没翻译；再给 Code 抄
 * 一张表，两张表会在下一个新事件加进来那天开始不一样。所以运行层的那部分搬到这
 * 里，Work 那张表把它展开进去，再加上只有任务才有的那几条（TaskSubmitted 那一族）。
 *
 * 表里没有的类型原样落地，不落成「未知事件」：一个这个控制台还不认识的类型，
 * 仍然是读者该看见的事实，而且是他唯一能拿去 grep 日志的那个词。
 */
export const RUN_EVENT_TITLES: Readonly<Record<string, string>> = {
  RunStarted: "运行已开始",
  RunPaused: "运行已暂停",
  RunCompleted: "运行已完成",
  RunFailed: "运行失败",
  RunCancelled: "运行已取消",
  ContextBuilt: "上下文已构建",
  ModelStarted: "模型调用已开始",
  ModelCompleted: "模型调用已完成",
  ToolProposed: "工具调用已提出",
  PermissionRequested: "权限检查已请求",
  PermissionResolved: "权限检查已完成",
  ToolApprovalDecided: "工具审批已裁定",
  ToolStarted: "工具调用已开始",
  ToolProgress: "工具调用有新进展",
  ToolCompleted: "工具调用已完成",
  ToolFailed: "工具调用失败",
  ContextCompacted: "上下文已压缩",
  AgentDelegated: "子代理已委派",
  AgentCompleted: "子代理已完成",
};

export function runEventTitle(event: EventEnvelope): string {
  return RUN_EVENT_TITLES[event.event_type] ?? event.event_type;
}
