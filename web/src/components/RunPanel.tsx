import { useState } from "react";
import { ChevronDown, ChevronRight } from "lucide-react";

import { shortId } from "./ui";
import { errorCodeLabel, explainRunFailure } from "./errorVocabulary";
import {
  ACTIVITY_LABEL,
  PAUSE_LABEL,
  STATUS_LABEL,
  STOP_REASON_LABEL,
  StatusIcon,
  formatAgainstCeiling,
  formatDuration,
  formatTokens,
  fractionOf,
} from "./runVocabulary";
import {
  flattenRuns,
  runDurationMs,
  totalSpend,
  totalTokens,
  type RunNode,
} from "./runTree";

/**
 * Who is working on this Task, and how far each of them has got.
 *
 * The step stream beside this one answers "what happened, in order". Since a
 * run can delegate (ADR-082), that stream interleaves two agents' model calls
 * and tool calls, and no amount of ordering makes "how is the sub-agent doing"
 * readable from it -- the reader would have to hold the interleaving in their
 * head. This panel answers the other question: **one row per run, whatever the
 * order things happened in.**
 *
 * Four decisions worth stating, because each had an easier alternative.
 *
 * **A tree, not a list.** The rows are nested under the run that started them.
 * A flat list with a "parent" column would be smaller code and would make the
 * one thing this exists to show -- that this agent was started *by* that one --
 * something the reader reconstructs by matching ids.
 *
 * **The nesting is in the DOM, not only in the indent.** The first version drew
 * the whole tree as one flat `<ul>` and expressed depth as left padding, which
 * meant a screen reader was read a list of siblings: the panel's entire reason
 * to exist was carried by a CSS custom property. Real `<ul role="group">`
 * nesting fixes that. It is deliberately **not** `role="tree"` -- that role
 * promises arrow-key navigation between items, and announcing "tree" to
 * somebody who then finds the arrow keys do nothing is worse than the nested
 * list, which browsers already announce with the level. The disclosure buttons
 * are ordinary buttons with `aria-expanded`, a pattern that is complete rather
 * than half-kept.
 *
 * **Each run reports its own spend, and the total is separate.** Folding a
 * child's tokens into its parent's row would read as a statement about the
 * parent's budget, which never saw them. The total is shown once, at the top,
 * where it is a total and cannot be mistaken for anybody's ceiling.
 *
 * **No progress bar, but the ceiling is shown.** These are not the same claim,
 * and the first version of this comment ran them together. A run does not know
 * how many steps it *will* take, so a bar against completion would be a
 * fraction over an invented denominator -- that stands. What it does know is
 * what it was **allowed** to spend: `RunStarted.budget` is written by the run
 * itself, and for a delegated run it carries
 * `multi_agent.max_tokens_per_agent_invocation`, the number that will actually
 * stop it. Spend against that is a measured fraction of a first-hand
 * denominator, and it is the one the reader of a sub-agent that died holding
 * "额度不足" needed to see.
 */


function RunRow({
  node,
  depth,
  selectedRunId,
  onSelect,
  collapsed,
  onToggle,
}: {
  node: RunNode;
  depth: number;
  selectedRunId: string | null;
  onSelect: (runId: string | null) => void;
  collapsed: ReadonlySet<string>;
  onToggle: (runId: string) => void;
}) {
  const selected = selectedRunId === node.runId;
  const hasChildren = node.children.length > 0;
  const open = hasChildren && !collapsed.has(node.runId);
  // A delegated run is named by the sub-agent it was started as; a graph node's
  // run is named by its node. Neither is guessed from the id -- a run with
  // neither says so rather than being labelled with a truncated identifier
  // dressed up as a name.
  const name =
    node.definitionName ?? node.nodeId ?? `运行 ${shortId(node.runId, 8)}`;
  const paused = node.pausedFor !== null;
  const activity = paused
    ? (PAUSE_LABEL[node.pausedFor ?? ""] ?? "已暂停")
    : node.status === "running" && node.latestEventType !== null
      ? ACTIVITY_LABEL[node.latestEventType]
      : undefined;
  const spent = totalTokens(node.spend);
  const fill = fractionOf(spent, node.ceiling.maxTotalTokens);
  const elapsed = runDurationMs(node);
  const duration = elapsed === null ? null : formatDuration(elapsed);
  // A delegated run whose toolbox came out empty, which is a documented way to
  // fail quietly rather than a hypothetical. A child's tools are its
  // definition's ceiling **intersected with the parent Task's envelope**, so a
  // Task submitted without search authority delegates a `researcher` that can
  // search nothing -- `application/sub_agents.py` says so in as many words and
  // calls it better than refusing the delegation. It is better, and it is also
  // invisible: the run starts, calls nothing, and reports that it could not
  // find anything. `toolCount` has been computed here since the module was
  // written and rendered nowhere, so this is the field finding its reader.
  //
  // Only for delegated runs. A graph node's run legitimately holds no tools --
  // `understand` is a model call and nothing else -- and flagging those would
  // put a warning on the majority of rows in every Task that never delegated.
  const emptyToolbox = node.definitionName !== null && node.toolCount === 0;
  const stopped =
    node.stopReason === null ? undefined : STOP_REASON_LABEL[node.stopReason];
  // Its own account where it has one. `AgentCompleted` carries no error, so a
  // child whose `RunFailed` is not in this page keeps 失败 with nothing after
  // it -- second-hand that it failed is not second-hand knowledge of why.
  // The server's own sentence where this repository has not learned it yet,
  // and a precise Chinese one where it has. The code label is the prefix only
  // in the fallback: `超出了这次任务的步数或 token 预算` covers two different
  // ceilings, so pairing it with a sentence that already names one just makes
  // the reader read past it.
  const explained = node.failure === null ? null : explainRunFailure(node.failure.message);
  const failure =
    node.failure === null
      ? null
      : (explained ??
        `${errorCodeLabel(node.failure.code)}：${node.failure.message}`);

  return (
    <li className={`aw-run-row${selected ? " is-selected" : ""} is-${node.status}`}>
      <div
        className="aw-run-line"
        style={{ "--aw-run-depth": depth } as React.CSSProperties}
      >
        {hasChildren ? (
          <button
            aria-expanded={open}
            aria-label={`${open ? "折叠" : "展开"} ${name} 派生的子代理`}
            className="aw-run-disclosure"
            onClick={() => {
              onToggle(node.runId);
            }}
            type="button"
          >
            {open ? (
              <ChevronDown aria-hidden="true" size={14} />
            ) : (
              <ChevronRight aria-hidden="true" size={14} />
            )}
          </button>
        ) : (
          <span aria-hidden="true" className="aw-run-disclosure is-empty" />
        )}
        <button
          aria-current={selected ? "true" : undefined}
          className="aw-run-row-button"
          onClick={() => {
            onSelect(selected ? null : node.runId);
          }}
          type="button"
        >
          <span className="aw-run-status" title={STATUS_LABEL[node.status]}>
            <StatusIcon paused={paused} status={node.status} />
          </span>
          <span className="aw-run-name">
            {node.definitionName !== null && (
              <span className="aw-run-badge">子代理</span>
            )}
            <span>{name}</span>
            {node.modelProfile !== null && (
              <span className="aw-run-profile">{node.modelProfile}</span>
            )}
          </span>
          <span className="aw-run-meta">
            {activity !== undefined && (
              <span className={`aw-run-activity${paused ? " is-paused" : ""}`}>
                {activity}
              </span>
            )}
            {stopped !== undefined && (
              <span className="aw-run-stop">{stopped}</span>
            )}
            {node.spend.steps > 0 && (
              <span
                title={
                  node.ceiling.maxSteps === null
                    ? undefined
                    : `这次运行自己声明的步数上限是 ${String(node.ceiling.maxSteps)}`
                }
              >
                {formatAgainstCeiling(
                  node.spend.steps,
                  node.ceiling.maxSteps,
                  String,
                )}{" "}
                步
              </span>
            )}
            {node.spend.toolCalls > 0 && (
              <span>
                {formatAgainstCeiling(
                  node.spend.toolCalls,
                  node.ceiling.maxToolCalls,
                  String,
                )}{" "}
                次工具
              </span>
            )}
            {spent > 0 && (
              <span
                title={
                  node.ceiling.maxTotalTokens === null
                    ? "这次运行没有声明 token 上限"
                    : `这次运行自己声明的 token 上限是 ${String(node.ceiling.maxTotalTokens)}`
                }
              >
                {formatAgainstCeiling(
                  spent,
                  node.ceiling.maxTotalTokens,
                  formatTokens,
                )}
              </span>
            )}
            {duration !== null && (
              <span
                className="aw-run-duration"
                title={
                  node.status === "running"
                    ? "从它第一条事件到最近一条事件；它还在跑，所以这个数还会长"
                    : "从它第一条事件到最后一条事件"
                }
              >
                {duration}
              </span>
            )}
            {node.eventCount > 0 && (
              <span className="aw-run-events">{node.eventCount} 条事件</span>
            )}
          </span>
        </button>
      </div>
      {/* Drawn only where the run itself named a ceiling. Everywhere else the
          bar would need a denominator this page does not have, and inventing
          one is the thing this panel refuses to do. */}
      {fill !== null && (
        <div
          className={`aw-run-fuel${fill >= 0.8 ? " is-tight" : ""}`}
          style={{ "--aw-run-fill": fill } as React.CSSProperties}
        >
          <span aria-hidden="true" />
        </div>
      )}
      {emptyToolbox && (
        <p
          className="aw-run-note"
          style={{ "--aw-run-depth": depth } as React.CSSProperties}
        >
          这个子代理一件工具都没拿到——它能用的工具是它自己的上限与这个任务授权的交集，交出来是空的。它照样会跑完，只是查不到任何东西。
        </p>
      )}
      {failure !== null && (
        <p
          className="aw-run-failure"
          style={{ "--aw-run-depth": depth } as React.CSSProperties}
        >
          {failure}
        </p>
      )}
      {hasChildren && open && (
        <ul className="aw-run-list is-group">
          {node.children.map((child) => (
            <RunRow
              collapsed={collapsed}
              depth={depth + 1}
              key={child.runId}
              node={child}
              onSelect={onSelect}
              onToggle={onToggle}
              selectedRunId={selectedRunId}
            />
          ))}
        </ul>
      )}
    </li>
  );
}

export function RunPanel({
  roots,
  selectedRunId,
  onSelect,
  incomplete = false,
}: {
  roots: RunNode[];
  /** Which run the step stream is currently narrowed to, if any. */
  selectedRunId: string | null;
  onSelect: (runId: string | null) => void;
  /**
   * Whether the stream this tree was built from is known to have holes.
   *
   * ADR-083 invariant 5 -- "a tree that is not complete says so" -- was only
   * ever kept on the server, whose response carries `complete`. This is the
   * client's half of it, and it is not decoration: this tree is rebuilt from
   * whatever the page holds, so one skipped page containing an
   * `AgentDelegated` removes a whole branch, and the panel's silence would
   * report "nobody delegated" in exactly the same words as "the page that said
   * so never arrived".
   */
  incomplete?: boolean;
}) {
  // Which subtrees the reader has folded away. Held as the *closed* set rather
  // than the open one so that a child arriving mid-run appears: a delegation
  // announced after the reader last touched this panel is new information, and
  // defaulting it to hidden would make the panel quietly stop reporting.
  const [collapsed, setCollapsed] = useState<ReadonlySet<string>>(
    () => new Set(),
  );

  // Rendered only where there is something a flat stream cannot show. On a Task
  // that never delegated, every run is a graph node and the stage list above
  // already says all of this -- a second panel repeating it would be furniture.
  const all = flattenRuns(roots);
  const delegated = all.filter((node) => node.parentRunId !== null);
  // ...with one exception, and it is a trap the first version walked into.
  // **This panel owns the only control that undoes a narrowing.** So on any
  // Task where a narrowing is live, it has to render whether or not it has a
  // tree worth drawing -- otherwise a link carrying `?run=` to a Task with no
  // delegations, or a page whose delegation fell in a skipped stretch, shows
  // an empty stream with no way back and nothing saying why it is empty.
  const narrowedToMissingRun =
    selectedRunId !== null &&
    !all.some((node) => node.runId === selectedRunId);
  if (delegated.length === 0 && selectedRunId === null) return null;

  if (delegated.length === 0) {
    // 收窄活着，但没有树可画。摆一个写着「0 个是子代理」的表头是在回答没人问的
    // 问题；这里需要的只有那句话和那个出口。
    return (
      <section aria-label="参与这次任务的 Agent" className="aw-run-panel">
        <p className="aw-run-hint is-lonely">
          {narrowedToMissingRun
            ? "下面的执行过程被收窄到了一个不在这条流里的运行，所以它是空的。"
            : "下面的执行过程只显示这一个运行。"}
          <button
            className="aw-run-clear"
            onClick={() => {
              onSelect(null);
            }}
            type="button"
          >
            显示全部
          </button>
        </p>
      </section>
    );
  }

  const total = totalSpend(roots);
  const running = all.filter((node) => node.status === "running").length;

  return (
    <section aria-label="参与这次任务的 Agent" className="aw-run-panel">
      <header className="aw-run-panel-head">
        <h3>
          参与的 Agent
          <span className="aw-run-count">
            {all.length} 个运行 · 其中 {delegated.length} 个是子代理
            {running > 0 ? ` · ${running} 个进行中` : ""}
          </span>
        </h3>
        <span className="aw-run-total">
          合计 {formatTokens(total.inputTokens)}↓{" "}
          {formatTokens(total.outputTokens)}↑
        </span>
      </header>
      <ul className="aw-run-list">
        {roots.map((node) => (
          <RunRow
            collapsed={collapsed}
            depth={0}
            key={node.runId}
            node={node}
            onSelect={onSelect}
            onToggle={(runId) => {
              setCollapsed((held) => {
                const next = new Set(held);
                if (!next.delete(runId)) next.add(runId);
                return next;
              });
            }}
            selectedRunId={selectedRunId}
          />
        ))}
      </ul>
      {incomplete && (
        <p className="aw-run-hint is-incomplete">
          这条流有没有送达的分页，所以这棵树<strong>可能不全</strong>
          ——一次落在缺口里的委派，在这里和「没有派过子代理」长得一模一样。
        </p>
      )}
      {selectedRunId !== null && (
        <p className="aw-run-hint">
          {narrowedToMissingRun
            ? "下面的执行过程被收窄到了一个不在这条流里的运行，所以它是空的。"
            : "下面的执行过程只显示这一个运行。"}
          <button
            className="aw-run-clear"
            onClick={() => {
              onSelect(null);
            }}
            type="button"
          >
            显示全部
          </button>
        </p>
      )}
    </section>
  );
}
