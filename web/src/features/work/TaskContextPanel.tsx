import { useMemo, useState } from "react";
import { Check, ChevronRight } from "lucide-react";

import type { EventEnvelope } from "../../api/types";
import { PanelTabs } from "../../components/PanelTabs";
import { TurnUsage, type PartialTurnUsage } from "../../components/TurnUsage";
import { flattenRuns, totalTokens, type RunNode } from "../../components/runTree";

/**
 * 这个任务跑到哪了、出了什么、读过什么、花了多少——一栏之内，分几张标签。
 *
 * **它替掉的是一条只装文件名的产物栏。** 那一栏答得很好的问题只有一个（「产出了
 * 哪些文件」），而读者盯着一个还在跑的任务时问的是另外三个：还有几步、有没有人
 * 卡住、这一趟要花多少。那三个此前的答案分别在时间线里、在事件流里、和哪儿都没有。
 *
 * **为什么是标签页，不是往下堆的四节。** 上一版是后者，而它在一条 260px 的栏里
 * 撑到了四节：第四节要滚两屏才看得见，于是它等于不存在。标签页把「有哪些」和
 * 「现在看哪个」拆开，前者一眼看完（见 `PanelTabs`）。
 *
 * **用量不进标签，留在页脚。** 另外四件事读者是**挑着看**的，而这一趟花了多少是
 * 那种「一直在余光里」的数——把它收进一张要点开的标签，等于要求读者先怀疑它贵，
 * 才看得到它有多贵。
 *
 * **每一节都只说这一页手上已经有的事实。** 没有为它加接口：进度来自
 * `deriveLifecycle` 已经算好的阶段，子代理来自 `buildRunTree`，产物就是原来那条
 * 栏，用量把根运行的花销加起来。它是重新编排，不是新的数据源。
 *
 * **没有百分比。** 一个任务在跑之前不知道自己要跑几步，所以任何完成度都是一个
 * 编出来的分母。进度那一节画的是「第几步，共几步」和一排点——那两个数是真的。
 */

/**
 * 只要三个字段，不要 `StreamStage`。
 *
 * `StreamStage` 是给 `StepStream` 画时间线用的，带着事件、节点名和右侧那行状态
 * 文字。这一节只需要「这段叫什么、走到没走到」——按结构声明，这块面就不会因为
 * 时间线那边加一个字段而跟着动。
 */
interface StageMark {
  id: string;
  title: string;
  state: string;
}

function ProgressDots({ stages }: { stages: readonly StageMark[] }) {
  const done = stages.filter((stage) => stage.state === "done").length;
  const active = stages.findIndex((stage) => stage.state === "active");
  // 「第几步」按**已完成的那些**数，不按 active 的下标：一个跳过的阶段
  // （`skipped`）也占一个下标，用下标会把「跳过了两段」说成「已经走到第五步」。
  const at = active >= 0 ? done + 1 : done;
  // 失败的那一段单独说。实测一个在「动手做事」死掉的任务，按上面那个数是
  // 「第 1 步，共 4 步」——读起来像它还在第一步慢慢走，而它已经停了。一个进度
  // 数字在一个停住的任务上是句错话，哪怕每一位都算对了。
  const failed = stages.find((stage) => stage.state === "failed");

  return (
    <div className="aw-taskctx-block">
      <div className="aw-taskctx-dots">
        {stages.map((stage, index) => (
          <span key={stage.id}>
            {index === 0 ? null : (
              <u className={stage.state === "done" ? "is-done" : undefined} />
            )}
            <i
              className={
                stage.state === "done"
                  ? "is-done"
                  : stage.state === "active"
                    ? "is-active"
                    : stage.state === "failed"
                      ? "is-failed"
                      : undefined
              }
              title={stage.title}
            >
              {stage.state === "done" ? (
                <Check aria-hidden="true" size={10} strokeWidth={3} />
              ) : null}
            </i>
          </span>
        ))}
      </div>
      {failed === undefined ? (
        <p className="aw-taskctx-note">
          第 {at} 步，共 {stages.length} 步。没有百分比：跑之前不知道要跑几步，一个编出来的分母比没有更坏。
        </p>
      ) : (
        <p className="aw-taskctx-note">
          停在「{failed.title}」，共 {stages.length} 步。后面几步没有跑，不是还没轮到。
        </p>
      )}
    </div>
  );
}

function AgentPips({
  roots,
  onOpen,
}: {
  roots: readonly RunNode[];
  onOpen: () => void;
}) {
  const agents = useMemo(
    () => flattenRuns(roots).filter((run) => run.parentRunId !== null),
    [roots],
  );
  if (agents.length === 0) return null;

  const running = agents.filter((run) => run.status === "running").length;
  const failed = agents.filter((run) => run.status === "failed").length;
  const spent = agents.reduce((sum, run) => sum + totalTokens(run.spend), 0);

  return (
    <div className="aw-taskctx-block">
      <div className="aw-taskctx-pips">
        {agents.map((run) => (
          <i
            className={`is-${run.status}`}
            key={run.runId}
            title={run.definitionName ?? run.runId}
          />
        ))}
      </div>
      <p className="aw-taskctx-note">
        {running > 0 ? `${running} 个在跑` : "都已回来"}
        {failed > 0 ? `，${failed} 个失败` : ""}
        。它们烧掉的 {formatK(spent)} 不占这个任务的额度，两个数不相加。
      </p>
      {/* 这一格只答「谁怎么样了」。谁读了什么、说了什么、为什么停——那些在
          `AgentPanel` 里，而它是一整块面：塞进一条 300px 的标签页里，每一行
          都得截断成一个认不出来的名字。 */}
      <button className="aw-taskctx-more" onClick={onOpen} type="button">
        <span>逐个看它们做了什么</span>
        <ChevronRight aria-hidden="true" size={14} />
      </button>
    </div>
  );
}

function formatK(value: number): string {
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(2)}M`;
  if (value >= 1_000) return `${(value / 1_000).toFixed(1)}k`;
  return String(value);
}

/**
 * 这个任务碰过什么。
 *
 * 数的是**工具调用**和**用过的工具**，不是「读了几个文件」。后者要认得哪些工具算
 * 「读」，而那份名单会随每一个新工具过期一次，过期的样子是一个偏小的数字——一个
 * 看起来正常的错。
 */
function Touched({ events }: { events: readonly EventEnvelope[] }) {
  const { calls, tools } = useMemo(() => {
    const names = new Set<string>();
    let count = 0;
    for (const event of events) {
      // 名字从**所有**工具事件上收，次数只数结束的那些。实测 `ToolCompleted`
      // 的载荷里没有 `tool_name`（它在 `ToolStarted` 上），只认结束事件的话
      // 名字那一行永远是空的，而次数是对的——一个只错了一半的显示。
      const payload = event.payload as { tool_name?: unknown };
      if (
        event.event_type === "ToolStarted" ||
        event.event_type === "ToolCompleted" ||
        event.event_type === "ToolFailed"
      ) {
        if (typeof payload.tool_name === "string" && payload.tool_name !== "") {
          names.add(payload.tool_name);
        }
      }
      if (event.event_type === "ToolCompleted" || event.event_type === "ToolFailed") {
        count += 1;
      }
    }
    return { calls: count, tools: [...names].sort() };
  }, [events]);

  if (calls === 0) return null;

  return (
    <div className="aw-taskctx-block">
      <p className="aw-taskctx-fact">
        调用了 <strong>{calls}</strong> 次工具
      </p>
      {tools.length === 0 ? null : (
        <div className="aw-taskctx-chips">
          {tools.map((name) => (
            <i key={name}>{name}</i>
          ))}
        </div>
      )}
      <p className="aw-taskctx-note">
        授权信封在提交那一刻冻住，上面这些就是它允许的全部。
      </p>
    </div>
  );
}

/**
 * 这一趟花了多少，钉在栏底。
 *
 * 不进标签页：另外几格是挑着看的，这个数是余光里的。收进一张要点开的标签等于
 * 要求读者先怀疑它贵，才看得到它有多贵。
 *
 * 加的是**根运行**，每一步那一笔在左边各自那一行上。
 */
function Spend({ roots }: { roots: readonly RunNode[] }) {
  const main = useMemo(
    () => flattenRuns(roots).filter((run) => run.parentRunId === null),
    [roots],
  );
  const usage: PartialTurnUsage | null = useMemo(() => {
    if (main.length === 0) return null;
    return main.reduce<PartialTurnUsage>(
      (sum, run) => ({
        input_tokens: sum.input_tokens + run.spend.inputTokens,
        output_tokens: sum.output_tokens + run.spend.outputTokens,
        cache_read_tokens: 0,
        cache_write_tokens: sum.cache_write_tokens + run.spend.cacheWriteTokens,
        // 钱按运行写下，而这里是把几次根运行加起来——加法在 token 上成立，在
        // 「当时按哪份价目表算的」上不成立。所以这一格只报 token。
        cost_micro_usd: null,
      }),
      {
        input_tokens: 0,
        output_tokens: 0,
        cache_read_tokens: 0,
        cache_write_tokens: 0,
        cost_micro_usd: null,
      },
    );
  }, [main]);

  if (usage === null) return null;

  return (
    <footer className="aw-taskctx-spend">
      <TurnUsage label="这个任务" usage={usage} />
    </footer>
  );
}

export function TaskContextPanel({
  events,
  onOpenAgents,
  outputs,
  roots,
  stages,
}: {
  events: readonly EventEnvelope[];
  onOpenAgents: () => void;
  /** 原来那条产物栏，原样嵌进来——它答的那个问题没有变。 */
  outputs: React.ReactNode;
  roots: readonly RunNode[];
  stages: readonly StageMark[];
}) {
  const [tab, setTab] = useState<string | null>(null);
  const agentCount = useMemo(
    () => flattenRuns(roots).filter((run) => run.parentRunId !== null).length,
    [roots],
  );
  const touched = <Touched events={events} />;

  // 没表过态时落在哪一格，随任务的状态变。
  //
  // 一个还在跑的任务，读者问的是「到哪了」；一个跑完的任务，问的是「出了什么」。
  // 固定落在第一格（进度）会让后一种情况下的产物藏在一次点击后面——而那正是这
  // 一栏此前唯一装着的东西，把它藏起来是这次改动能犯的最大的错。
  //
  // 只在 `tab` 还是 `null` 时生效：读者一旦点过任何一格，这个默认就再也不动，
  // 否则任务跑完的那一刻会把他正在读的那一格换掉。
  // 还在跑、或者已经停住了——两种情况读者问的都是「到哪了」。只有一个**跑完了**
  // 的任务，那个问题才换成「出了什么」。
  //
  // 失败这一支是看着真界面加的：一个 `work` 步骤烧完额度死掉的任务，默认落在
  // 「产物」上，而那一栏里只有一句「这个任务没有产出可下载的文件」——屏幕上最
  // 没用的一句话，出现在最该说明情况的位置。
  const settled = !stages.some(
    (stage) => stage.state === "active" || stage.state === "failed",
  );
  const fallback =
    outputs !== null && settled ? "outputs" : stages.length > 0 ? "progress" : null;

  return (
    <aside aria-label="这个任务的进度与产出" className="aw-taskctx">
      <PanelTabs
        active={tab ?? fallback}
        entries={[
          {
            id: "progress",
            label: "进度",
            available: stages.length > 0,
            body: <ProgressDots stages={stages} />,
          },
          {
            id: "agents",
            label: "子代理",
            count: agentCount,
            available: agentCount > 0,
            body: <AgentPips onOpen={onOpenAgents} roots={roots} />,
          },
          {
            id: "outputs",
            label: "产物",
            available: outputs !== null,
            body: outputs,
          },
          {
            // `Touched` 在没有工具调用时自己返回 null，而这一栏要在**画标签之前**
            // 就知道那一格是不是空的——所以这里判的是那个元素渲染出来是不是 null，
            // 而不是再数一遍事件。判错的样子是一枚点进去空白的标签。
            id: "touched",
            label: "上下文",
            available: events.some(
              (event) =>
                event.event_type === "ToolCompleted" ||
                event.event_type === "ToolFailed",
            ),
            body: touched,
          },
        ]}
        label="这个任务的几栏"
        onSelect={setTab}
      />
      <Spend roots={roots} />
    </aside>
  );
}
