import type { ArtifactRef } from "../api/types";
import { errorCodeLabel } from "./errorVocabulary";
import { describeEvent, type StepBody, type StepFact } from "./stepDetail";
import type { StepGroup } from "./stepGroups";

/**
 * 一步（一次工具调用，或一次模型作答）作为**一件事**说出来的样子。
 *
 * `describeEvent` 回答的是「这一条事件说了什么」；一次工具调用是五条事件——提出、
 * 权限、开始、完成（或失败）——每一条各答各的，而读者打开一步想问的只有三句：
 * 传了什么进去、回来了什么、要是没成是为什么。这里把五条的答案并成那三句，按
 * 读的顺序排。原始事件一条不丢，仍在这一步底下（`StepStream` 与 `CodeTurn` 各自
 * 把它们折在一层「事件记录」里）；这里只是不再要读者从五行簿记里把那三句自己
 * 拼出来。
 *
 * **并的规则很少，而且每一条都是为了少说一遍。** 事实按标签去重、先到先得——一次
 * 调用的「工具」在提出和开始两条上各写了一遍，读者要一次。四个只对簿记有意义的
 * 标签不进这份视图（参数摘要那个 sha、调用 ID、工具调用 ID、参数大小）：它们仍
 * 在原始事件里，而在一份说「它做了什么」的视图里，一个十六进制串只是噪声。正文
 * 按标签去重后再按固定顺序排——提示词、思考、参数、返回、错误、模型输出——因为
 * 那是事情发生的顺序，而事件到达的顺序在两条模型事件被折进工具组之后已经不是了。
 *
 * **失败单独一句。** `ToolFailed.error` 与 `PermissionResolved.effect = deny` 各有
 * 自己的事实行，但读者扫到一行红色的「失败」之后要的是一句话，不是一张表。这一句
 * 从错误码的词表（`errorVocabulary`）取名，再接服务端自己的那句英文——后者不翻
 * 译也不丢：它是唯一能拿去对日志的东西。
 */
export interface GroupDetail {
  facts: StepFact[];
  bodies: StepBody[];
  artifact: ArtifactRef | null;
  /** 没成的那一句。成功的组是 `null`——成功靠不说话来说。 */
  failure: string | null;
}

/** 只对簿记有意义、在合并视图里只是噪声的那几个标签。 */
const BOOKKEEPING_LABELS: ReadonlySet<string> = new Set([
  "参数摘要",
  "调用 ID",
  "工具调用 ID",
  "参数大小",
]);

/** 正文的阅读顺序：事情发生的顺序，不是事件到达的顺序。 */
const BODY_ORDER: readonly string[] = [
  "发给模型的提示词",
  "思考摘要",
  "调用参数",
  "工具返回",
  "错误信息",
  "模型输出",
];

export function describeGroup(
  group: StepGroup,
  options: {
    /**
     * 不带正文。给已经用别的形状画过参数和返回的调用方——`CommandTrace` 把一条
     * 命令和它的输出画成终端的样子，再在下面画一遍「调用参数 / 工具返回」是同
     * 一件事说两遍。
     */
    bodies?: boolean;
  } = {},
): GroupDetail {
  const facts: StepFact[] = [];
  const seenFacts = new Set<string>();
  const bodies = new Map<string, StepBody>();
  let artifact: ArtifactRef | null = null;
  // 一个工具组里折着提出它的那次模型调用（`stepGroups` 的 modelTurnHome）。那次
  // 调用的事实（模型、档位、token）和它的提示词是**上下文**，不是这一步做的事——
  // 实测一个 `project_grep` 的组展开之后，第一屏是六行模型事实加整段系统提示词，
  // 工具自己的参数和返回被推到了下面。所以工具组只取模型事件里的「思考摘要」：
  // 那是唯一回答「为什么调它」的部分。模型组（模型作答）照旧全部要。
  const toolGroup = group.key.startsWith("tool:");

  for (const event of group.events) {
    const detail = describeEvent(event);
    const modelEvent =
      event.event_type === "ModelStarted" || event.event_type === "ModelCompleted";
    if (toolGroup && modelEvent) {
      const thought = detail.bodies.find((body) => body.label === "思考摘要");
      if (thought !== undefined && !bodies.has(thought.label)) {
        bodies.set(thought.label, thought);
      }
      continue;
    }
    for (const fact of detail.facts) {
      if (BOOKKEEPING_LABELS.has(fact.label) || seenFacts.has(fact.label)) continue;
      seenFacts.add(fact.label);
      facts.push(fact);
    }
    for (const body of detail.bodies) {
      if (!bodies.has(body.label)) bodies.set(body.label, body);
    }
    if (artifact === null && detail.artifact !== null) artifact = detail.artifact;
  }

  const ordered = [...bodies.values()].sort(
    (left, right) => rank(left.label) - rank(right.label),
  );

  return {
    facts,
    bodies: options.bodies === false ? [] : ordered,
    artifact,
    failure: failureOf(group),
  };
}

function rank(label: string): number {
  const at = BODY_ORDER.indexOf(label);
  return at === -1 ? BODY_ORDER.length : at;
}

/**
 * 为什么没成，一句话。
 *
 * 先看有没有被拒——拒绝在前、失败在后，而失败往往只是拒绝的后果（`stepGroups`
 * 的 `outcomeAfter` 说的是同一件事）。被拒的理由码原样给：`policy_denied` 之类
 * 是词表里有的，人工不批（`nobody answered within its 120s bound`）是服务端那
 * 句话里才有的，两者都要到达屏幕。
 */
function failureOf(group: StepGroup): string | null {
  if (group.outcome !== "failed" && group.outcome !== "denied") return null;

  let deniedReason: string | null = null;
  let failedCode: string | null = null;
  let failedMessage: string | null = null;
  for (const event of group.events) {
    const payload = event.payload as Record<string, unknown>;
    if (event.event_type === "PermissionResolved" && payload.effect === "deny") {
      deniedReason = str(payload.reason_code) ?? deniedReason;
    }
    if (event.event_type === "ToolApprovalDecided" && payload.decision === "deny") {
      deniedReason = deniedReason ?? "有人拒绝了这次调用";
    }
    if (event.event_type === "ToolFailed") {
      const error = payload.error as Record<string, unknown> | undefined;
      failedCode = str(error?.code) ?? failedCode;
      failedMessage = str(error?.message) ?? failedMessage;
    }
  }

  if (group.outcome === "denied") {
    const why = failedMessage ?? deniedReason;
    return why === null ? "这次调用被拒绝了。" : `这次调用被拒绝了：${why}`;
  }
  if (failedCode === null && failedMessage === null) return "这次调用失败了。";
  const label = failedCode === null ? "失败" : errorCodeLabel(failedCode);
  return failedMessage === null ? `${label}。` : `${label}：${failedMessage}`;
}

function str(value: unknown): string | null {
  if (typeof value !== "string") return null;
  const trimmed = value.trim();
  return trimmed === "" ? null : trimmed;
}
