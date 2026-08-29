import type { EventEnvelope } from "../api/types";

/**
 * 一个阶段里的事件，按「是谁写的」切成连续的几段。
 *
 * 委派之后，一个图节点的阶段里同时躺着两个 agent 的事件：父运行发起委派，子运行
 * 在它下面开始作答、调工具、写报告，父运行再接着往下走。它们共用同一个
 * `graph_node_id`（`adapters/delegation.py` 造子 scope 时逐字段沿用父的，只换
 * `run_id`），所以它们本来就落在同一个阶段里——缺的一直是「这几条是**别人**写的」
 * 这个记号。
 *
 * 在此之前那个记号是给每一行标题加前缀「子代理 analyst：」。
 * `features/work/delegations.ts` 写这段代码时就说明白了它是个替身：
 * 「Grouping the stream into foldable sub-agent sections would be better, and it
 * means changing the shared step component every stage renders through.」
 * 这个模块加上 `StepStream` 的 `runLabel` 就是那件更好的事，那个共用组件也确实改了。
 *
 * **一个子运行一块，位置在它第一条事件那里；父运行的事件一条都不挪。**
 *
 * 这一条是被真数据改过的。最初的实现是纯连续分段——`run_id` 一变就切一段，谁也不
 * 合并——论证是「归堆会重写发生过的顺序」。然后拿 `task_75cd1e0c` 实测（四个
 * `analyst`，`max_parallel_child_invocations = 2`）：
 *
 * ```
 * P×15 → c1×1 → c2×1 → c1×1 → c2×1 → c1×2 → c3×1 → P×1 → c3×1 → P×1 → c2×2 → …
 * 连续分段        子代理块 10 个，块内事件 [1,1,1,1,2,1,1,2,2,4]
 * 兄弟可跨、父为屏障   子代理块 7 个
 * 同一阶段内全合并    子代理块 4 个，块内事件 [4,4,4,4]
 * ```
 *
 * 四个子代理各写了四条事件，而连续分段把它们摊成十个一两条的小块。**并发的两个
 * agent 之间没有顺序可读**——c1 的第二步排在 c2 的第一步前面，是调度器的产物，不是
 * 任何人做过的决定。把它画成十个交替的块，是在暗示一场它们之间并不存在的对话。
 *
 * 所以只合并**别人的**运行，父运行（`own`）的事件一条都不动、相对顺序原样保留。
 * 代价说清楚：一个子代理的块代表的是**一段时间**而不是一个瞬间，块里最后一条事件
 * 可能晚于它下面那些父运行事件。这一点由块头上的时长明说，而不是让读者自己猜。
 *
 * **一条事件都不丢。** 和 `stepGroups` 的承诺一样：进去多少条，各段加起来还是多少
 * 条，顺序不变。`splitByRun` 的测试里有一条专门钉这个——一个认不出 `run_id` 的
 * 实现最容易的错法就是把它悄悄扔掉。
 */

export interface RunSection {
  /** 这一段的事件属于哪个运行。 */
  runId: string;
  /**
   * 它是不是这个阶段自己的运行。
   *
   * 判据是「第一条事件属于谁」，而不是「哪个运行的事件最多」。一次委派只可能发生
   * 在父运行**已经开始之后**，所以一个阶段里最早的那条事件必然是父运行的——
   * 数量则不是：一个子代理搜了二十次，事件数可以轻松盖过派它出去的那个运行。
   *
   * 有一种情况这个判据会答错：这个阶段最前面那一页事件没送到，页面手里最早的一条
   * 恰好是子运行的。那时子运行被当成「自己的」，父运行反而被装进框里。这仍然是
   * 一句真话（那些事件确实来自另一个运行），只是主次颠倒；而它需要的前提——缺页——
   * 在别处已经被说出来了（时间线上方的缺口提示）。
   */
  own: boolean;
  events: EventEnvelope[];
}

/**
 * 把一串事件切成连续的运行段。
 *
 * 空进空出。全部属于同一个运行时返回一段，且 `own` 为真——调用方据此渲染成和从前
 * 完全一样的样子，这也是「没有委派的任务看不出任何变化」这条的实现方式。
 */
export function splitByRun(events: readonly EventEnvelope[]): RunSection[] {
  if (events.length === 0) return [];
  const ownRunId = events[0]?.run_id;
  const sections: RunSection[] = [];
  for (const event of events) {
    const last = sections.at(-1);
    if (last !== undefined && last.runId === event.run_id) {
      last.events.push(event);
      continue;
    }
    sections.push({
      runId: event.run_id,
      own: event.run_id === ownRunId,
      events: [event],
    });
  }
  return sections;
}

/**
 * 把属于同一个别人运行的几段并成一段，位置取它第一次出现的地方。
 *
 * 只并**不属于这个阶段自己**的那些。父运行的段一个都不动——它的事件之间有真正的
 * 先后（它委派、它等、它拿到结果、它接着做），而两个并发子代理之间没有。
 *
 * 并完之后段数只会变少，事件总数不变：被并走的那些事件接在同名段的后面，各自内部
 * 的顺序原样保留。
 */
export function foldForeignRuns(sections: readonly RunSection[]): RunSection[] {
  const folded: RunSection[] = [];
  const seen = new Map<string, RunSection>();
  for (const section of sections) {
    if (section.own) {
      folded.push({ ...section, events: [...section.events] });
      continue;
    }
    const held = seen.get(section.runId);
    if (held !== undefined) {
      held.events.push(...section.events);
      continue;
    }
    const fresh = { ...section, events: [...section.events] };
    seen.set(section.runId, fresh);
    folded.push(fresh);
  }
  return folded;
}

/**
 * 这个阶段里有没有出现过第二个运行。
 *
 * 调用方用它决定「要不要按段渲染」：只有一个运行时，分段渲染和从前的渲染是同一个
 * 结果，但走的是不同的代码路径——而这个仓库里绝大多数任务从来没有委派过，让它们
 * 走原来那条路，是让「新加的这一层没画错」这件事只在真的有第二个运行时才需要成立。
 */
export function hasForeignRun(sections: readonly RunSection[]): boolean {
  return sections.some((section) => !section.own);
}
