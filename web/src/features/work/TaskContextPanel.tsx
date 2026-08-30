import { useMemo } from "react";
import { Check, ChevronRight } from "lucide-react";

import type { EventEnvelope } from "../../api/types";
import { TurnUsage, type PartialTurnUsage } from "../../components/TurnUsage";
import { flattenRuns, totalTokens, type RunNode } from "../../components/runTree";

/**
 * 这个任务跑到哪了、出了什么、读过什么、花了多少——一栏之内。
 *
 * **它替掉的是一条只装文件名的产物栏。** 那一栏答得很好的问题只有一个（「产出了
 * 哪些文件」），而读者盯着一个还在跑的任务时问的是另外三个：还有几步、有没有人
 * 卡住、这一趟要花多少。那三个此前的答案分别在时间线里、在事件流里、和哪儿都没有。
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
      <h3 className="aw-taskctx-head">进度</h3>
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
      <button className="aw-taskctx-head is-link" onClick={onOpen} type="button">
        <span>子代理</span>
        <span className="aw-taskctx-count">{agents.length}</span>
        <ChevronRight aria-hidden="true" size={14} />
      </button>
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
      <h3 className="aw-taskctx-head">上下文</h3>
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
    <div className="aw-taskctx-block">
      <h3 className="aw-taskctx-head">用量</h3>
      <TurnUsage usage={usage} />
      <p className="aw-taskctx-note">
        每一步那一笔在左边各自那一行上；这里是这个任务主运行的合计。
      </p>
    </div>
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
  return (
    <aside aria-label="这个任务的进度与产出" className="aw-taskctx">
      {stages.length === 0 ? null : <ProgressDots stages={stages} />}
      <AgentPips onOpen={onOpenAgents} roots={roots} />
      {outputs}
      <Touched events={events} />
      <Spend roots={roots} />
    </aside>
  );
}
