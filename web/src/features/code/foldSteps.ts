/**
 * 连续做同一件事的那几步，折成一行（ADR-0121）。
 *
 * 用户贴回来的那一轮：「查看项目目录」「读取项目目录」几行之后，是 113 行一模一样
 * 的「在本机执行命令」，然后才是报告。原话是「如果就是有这么长的很影响感官，需要
 * 折叠和优化流式输出」。一行一步的列表回答的是「它每一步做了什么」，而 113 行同样
 * 的动作回答不了任何问题——读者要翻过一整屏才知道它其实只做了一件事。
 *
 * Claude Code 的做法是这里照搬的形状：两段话之间连着的工具调用收成一行摘要
 * （「Ran 113 commands」），点开才是逐条；正在跑的那一条始终看得见。
 *
 * 规则写死在这里，不交给渲染层判断：
 *
 * - **只折同一个动作标题**（`group.title`）。标题带着「（沿用上次结果）」这种后缀
 *   的是另一件事，不和普通的混在一起。
 * - **有思考文字的一步不折。** 那是模型在这一步说的话，折起来就是把正文藏了；它也是
 *   两串动作之间天然的分隔。
 * - **成败分开折。** 成功和进行中算一类，失败和被拒算另一类——失败是读者翻这张列表
 *   的首要原因，不能和一百次成功混进同一个数字里。
 * - **至少三步才折。** 两行一样的动作排在一起本身不碍眼，折成一行反而多一次点击。
 */

import {
  summariseGroups,
  type StepGroup,
  type StepOutcome,
} from "../../components/stepGroups";
import type { TurnStep } from "./turnBlocks";

/** 连续几步起折。 */
export const FOLD_MIN = 3;

/**
 * 同一动作折完之后，一轮最后留在眼前的行数（第二层折叠，ADR-0121）。
 *
 * 同一动作的折叠对「一个动作做一百遍」有效，对「很多种动作交替做」无效：那一轮成功
 * 重写马里奥的回合 101 次调用，折完同一动作之后还有 59 行——编辑、求值、打开页面、
 * 求值、编辑，没有哪一种连着三次以上。所以再往上一层：只留最后几行，前面的收成一行
 * 「前面 N 步」加上每种做了几次。跑着的时候，留下的这几行就是正在发生的事。
 */
export const VISIBLE_TAIL = 6;

export type FoldedStep =
  | {
      kind: "head";
      /** 由第一项派生：一轮往下长的时候，这一行不重新挂载。 */
      key: string;
      /** 被收起来的那些项，同一动作的折叠照样在里面。 */
      items: FoldedStep[];
      /** 这些项底下一共多少步，而不是多少行。 */
      stepCount: number;
      /** 「在页面里求值 ×24 · 修改项目目录文件 ×12」，失败的种类排在最前。 */
      summary: string;
    }
  | { kind: "step"; step: TurnStep }
  | {
      kind: "fold";
      /**
       * 由第一步的 key 派生，轮询之间不变：一串正在跑的命令每多一步，这一行还是
       * 同一行，读者展开过它就不会因为长了一步而被收回去。
       */
      key: string;
      title: string;
      /** 成功与进行中合为一类；有一步在跑，这一行就是进行中。 */
      outcome: StepOutcome;
      steps: TurnStep[];
    };

type Settled = "fine" | "bad";

function classOf(outcome: StepOutcome): Settled {
  return outcome === "failed" || outcome === "denied" ? "bad" : "fine";
}

/**
 * 这一步能不能进一串折叠：有动作、没有自己的话，而且不是正在往外流思考的那一步——
 * 那一步的思考还没落成 `thinking`，折起来就把正在出现的字藏了。
 */
function foldable(
  step: TurnStep,
  liveCallId: string,
): step is TurnStep & { group: StepGroup } {
  return (
    step.group !== null &&
    step.thinking === "" &&
    !(liveCallId !== "" && step.modelCallId === liveCallId)
  );
}

export function foldSteps(
  steps: readonly TurnStep[],
  options: { liveCallId?: string } = {},
): FoldedStep[] {
  const liveCallId = options.liveCallId ?? "";
  const out: FoldedStep[] = [];
  let index = 0;
  while (index < steps.length) {
    const first = steps[index];
    if (first === undefined) break;
    if (!foldable(first, liveCallId)) {
      out.push({ kind: "step", step: first });
      index += 1;
      continue;
    }
    const title = first.group.title;
    const settled = classOf(first.group.outcome);
    let end = index + 1;
    while (end < steps.length) {
      const next = steps[end];
      if (
        next === undefined ||
        !foldable(next, liveCallId) ||
        next.group.title !== title ||
        classOf(next.group.outcome) !== settled
      ) {
        break;
      }
      end += 1;
    }
    const run = steps.slice(index, end);
    if (run.length >= FOLD_MIN) {
      out.push({
        kind: "fold",
        key: `fold:${first.key}`,
        title,
        outcome: outcomeOf(run),
        steps: run,
      });
    } else {
      for (const step of run) out.push({ kind: "step", step });
    }
    index = end;
  }
  return out;
}

/** 一串的状态：有一步在跑就是在跑；否则是这一类里第一个结局。 */
function outcomeOf(run: readonly TurnStep[]): StepOutcome {
  const outcomes = run.map((step) => step.group?.outcome ?? "ok");
  if (outcomes.includes("running")) return "running";
  return outcomes.find((outcome) => outcome === "failed" || outcome === "denied") ?? "ok";
}


/** 一项底下的所有步骤，按出现的顺序。 */
function stepsOf(item: FoldedStep): TurnStep[] {
  if (item.kind === "step") return [item.step];
  if (item.kind === "fold") return item.steps;
  return item.items.flatMap(stepsOf);
}

/**
 * 一轮太长时，把最后 `tail` 项之前的收成一行（第二层，ADR-0121）。
 *
 * 只在收得起至少三项时才收：前面只有一两行的话，把它们收起来反而多一次点击。
 */
export function foldHead(
  items: readonly FoldedStep[],
  options: { tail?: number } = {},
): FoldedStep[] {
  const tail = options.tail ?? VISIBLE_TAIL;
  const headLength = items.length - tail;
  if (headLength < FOLD_MIN) return [...items];
  const head = items.slice(0, headLength);
  const first = head[0];
  if (first === undefined) return [...items];
  const steps = head.flatMap(stepsOf);
  const groups = steps
    .map((step) => step.group)
    .filter((group): group is StepGroup => group !== null);
  const firstKey = first.kind === "step" ? first.step.key : first.key;
  return [
    {
      kind: "head",
      key: `head:${firstKey}`,
      items: head,
      stepCount: steps.length,
      summary: summariseGroups(groups),
    },
    ...items.slice(headLength),
  ];
}
