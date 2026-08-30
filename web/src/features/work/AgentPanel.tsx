import { useMemo } from "react";
import { ArrowLeft, ChevronRight, PanelRightClose } from "lucide-react";

import type { EventEnvelope } from "../../api/types";
import { errorCodeLabel, explainRunFailure } from "../../components/errorVocabulary";
import {
  ACTIVITY_LABEL,
  PAUSE_LABEL,
  STATUS_LABEL,
  STOP_REASON_LABEL,
  StatusIcon,
  formatDuration,
  formatTokens,
  fractionOf,
} from "../../components/runVocabulary";
import {
  flattenRuns,
  runDurationMs,
  totalTokens,
  type RunNode,
  type RunStatus,
} from "../../components/runTree";
import { shortId } from "../../components/ui";

/**
 * 子代理这一堆，和其中一个。
 *
 * **为什么是一块副面板，而不是时间线里的一段。** 一个运行的子代理是**后台在跑的
 * 活**：它们不占着正文，但读者随时要能瞥一眼，而且瞥的时候要一次看见全部——哪个
 * 还在跑、哪个死了、各烧了多少。时间线做不到这件事，它是按时间排的，六个子代理
 * 的事件在里面是交织的；`StepStream` 把外来的 run 折成一块之后可读了，但要读到
 * 第六个仍然得把前五个都翻过去。
 *
 * **两层，不是一层。** 这一块面此前试过两种一层的画法，两种都在同一个取舍上失
 * 败：把每个子代理都摊开（卡片/表格），一次看得见全部但占掉一屏；把它们缩成几
 * 行，不占地方但要一个一个展开才读得到。分成集合与详情之后两边都成立——集合只
 * 回答「谁怎么样了」，一屏读完；详情回答「它到底做了什么、凭什么」，进去才付
 * 那个篇幅。
 *
 * **它说的每一句都来自这一页已经持有的事件。** 没有为它加接口：`buildRunTree`
 * 早就把每个 run 的花销、上限、失败和耗时算好了，`AgentPanel` 只是换一个排法。
 * 这也划出了它的边界——`AgentDelegated` 不带子代理的**目标**，所以详情里那一块
 * 讲的是「它被给了什么」（哪一步派的、哪个模型档、几个工具、多少 token 上限），
 * 不讲「它被要求做什么」。后者要么加事件字段，要么不说；编一句出来最省事，也最
 * 坏。
 *
 * **没有「重试」。** 设计稿上那颗按钮这里没有实现，因为控制平面没有对应的动作：
 * `api/client.ts` 有 `cancelTask` 和 `deleteTask`，没有任何一条能重跑某一步。画
 * 一颗点不动的按钮，比不画那颗按钮更坏——它把「这件事做不到」伪装成「这件事你
 * 还没点」。
 */

/** 分组的顺序就是读者的关切顺序：先要动手的，再在跑的，最后已经落定的。 */
const GROUP_ORDER: readonly RunStatus[] = [
  "failed",
  "running",
  "unknown",
  "completed",
  "cancelled",
];

/**
 * 一个子代理此刻在做什么，用一句话。
 *
 * 和 `RunPanel` 的 `RunRow` 是同一套判断，顺序也一样：暂停压过在跑（暂停着的
 * 运行**也是** running，而这时候转的圈说的是反话——没有东西在算，能推动它的是
 * 读者）；失败给出它自己的说法；跑完了只在停止原因值得一说时才出声。
 *
 * 返回 `null` 而不是一句「已完成」：状态那一列已经说过了，再说一遍是把一行里
 * 唯一有信息量的位置让给了重复。
 */
function activityOf(node: RunNode): { text: string; tone: "" | "bad" } | null {
  if (node.pausedFor !== null) {
    return { text: PAUSE_LABEL[node.pausedFor] ?? "已暂停", tone: "" };
  }
  if (node.failure !== null) {
    const explained = explainRunFailure(node.failure.message);
    return {
      text:
        explained ??
        `${errorCodeLabel(node.failure.code)}：${node.failure.message}`,
      tone: "bad",
    };
  }
  if (node.status === "running" && node.latestEventType !== null) {
    const label = ACTIVITY_LABEL[node.latestEventType];
    return label === undefined ? null : { text: label, tone: "" };
  }
  if (node.stopReason !== null) {
    const label = STOP_REASON_LABEL[node.stopReason];
    return label === undefined ? null : { text: label, tone: "bad" };
  }
  return null;
}

/**
 * 名字，按和 `RunRow` 完全相同的规则取。
 *
 * 三级回退，最后一级刻意不好看：一个既没有子代理定义名、也没有图节点名的运行，
 * 显示的是截断的 id 加「运行」两个字，而不是把那截 id 打扮成一个名字。
 */
function nameOf(node: RunNode): string {
  return node.definitionName ?? node.nodeId ?? `运行 ${shortId(node.runId, 8)}`;
}

/** `74.1k/120k`，没有上限时就只有花掉的那个数——不是 `74.1k/0`。 */
function spendText(node: RunNode): string {
  const spent = totalTokens(node.spend);
  const ceiling = node.ceiling.maxTotalTokens;
  if (ceiling === null || ceiling <= 0) return formatTokens(spent);
  return `${formatTokens(spent)}/${formatTokens(ceiling)}`;
}

/**
 * 一条度量串：几次工具 · 花了多少 · 用了多久。
 *
 * 缺哪一项就少哪一段，不补占位符——`0 次工具` 和「这一页没收到工具事件」是两回
 * 事，而一个占位符会把后者说成前者。
 */
function metricsOf(node: RunNode): string {
  const parts: string[] = [];
  if (node.spend.toolCalls > 0) {
    parts.push(`${String(node.spend.toolCalls)} 次工具`);
  }
  parts.push(spendText(node));
  const elapsed = runDurationMs(node);
  const duration = elapsed === null ? null : formatDuration(elapsed);
  if (duration !== null) parts.push(duration);
  return parts.join(" · ");
}

/** 额度条只在有分母的时候画。没有上限的运行不画条，只写数。 */
function Meter({ node }: { node: RunNode }) {
  const fill = fractionOf(totalTokens(node.spend), node.ceiling.maxTotalTokens);
  if (fill === null) return null;
  const tone = fill >= 0.85 ? " is-over" : fill >= 0.6 ? " is-warn" : "";
  return (
    <span aria-hidden="true" className={`aw-agent-meter${tone}`}>
      <i style={{ width: `${String(Math.round(fill * 100))}%` }} />
    </span>
  );
}

function AgentEntry({
  node,
  onOpen,
}: {
  node: RunNode;
  onOpen: (runId: string) => void;
}) {
  const activity = activityOf(node);
  return (
    <li>
      <button
        className="aw-agent-entry"
        onClick={() => {
          onOpen(node.runId);
        }}
        type="button"
      >
        <span className="aw-agent-status" title={STATUS_LABEL[node.status]}>
          <StatusIcon paused={node.pausedFor !== null} status={node.status} />
        </span>
        <span className="aw-agent-name">
          <strong>{nameOf(node)}</strong>
          {activity !== null && (
            <span className={activity.tone === "bad" ? "is-bad" : undefined}>
              {activity.text}
            </span>
          )}
        </span>
        <ChevronRight aria-hidden="true" className="aw-agent-chevron" size={13} />
        <span className="aw-agent-metrics">
          {metricsOf(node)}
          <Meter node={node} />
        </span>
      </button>
    </li>
  );
}

/**
 * 这个运行做过的事，压成几行。
 *
 * 只取这一页手上有的事件，按顺序，末尾优先——一个还在跑的子代理，读者要看的是
 * 它**刚刚**做了什么，不是它开头做了什么。带工具名的带上工具名：`正在调用工具`
 * 少了后面那个名字，六行看起来是同一行。
 */
function activityLog(
  events: readonly EventEnvelope[],
  runId: string,
): { key: string; text: string; failed: boolean }[] {
  const rows: { key: string; text: string; failed: boolean }[] = [];
  for (const event of events) {
    if (event.run_id !== runId) continue;
    const label = ACTIVITY_LABEL[event.event_type];
    if (label === undefined) continue;
    const payload = event.payload as { tool_name?: unknown };
    const tool =
      typeof payload.tool_name === "string" && payload.tool_name !== ""
        ? payload.tool_name
        : null;
    rows.push({
      key: `${String(event.sequence)}`,
      text: tool === null ? label : `${label} ${tool}`,
      failed: event.event_type === "ToolFailed",
    });
  }
  return rows.slice(-8);
}

function AgentDetail({
  events,
  node,
  parent,
  onBack,
  onInspect,
}: {
  events: readonly EventEnvelope[];
  node: RunNode;
  parent: RunNode | undefined;
  onBack: () => void;
  onInspect: (runId: string) => void;
}) {
  const log = useMemo(() => activityLog(events, node.runId), [events, node.runId]);
  const elapsed = runDurationMs(node);
  const duration = elapsed === null ? null : formatDuration(elapsed);
  const failure = activityOf(node);
  const spent = totalTokens(node.spend);
  const ceiling = node.ceiling.maxTotalTokens;
  const fill = fractionOf(spent, ceiling);

  return (
    <>
      <header className="aw-agent-head">
        <button
          aria-label="回到子代理列表"
          className="aw-icon-button"
          onClick={onBack}
          type="button"
        >
          <ArrowLeft aria-hidden="true" size={15} />
        </button>
        <span className="aw-agent-head-title">
          <strong>{nameOf(node)}</strong>
          <code dir="ltr">
            {shortId(node.runId, 10)}
            {duration === null ? "" : ` · ${duration}`}
          </code>
        </span>
        <span className={`aw-agent-pill is-${node.status}`}>
          {STATUS_LABEL[node.status]}
        </span>
      </header>

      <div className="aw-agent-body">
        {node.failure !== null && failure !== null ? (
          <section className="aw-agent-section">
            <div className="aw-agent-failure">
              <p>{failure.text}</p>
              {/* 「不是什么」这三句是这一块最值钱的部分。这三个失败都长得像另一
                  个问题，少了它们，读者会去改一个与这次失败无关的旋钮，然后以为
                  改了没用。只在有分母时才敢说「不是额度用尽」——没有上限的运行，
                  这句话无从判断。 */}
              <div className="aw-agent-nots">
                <i>不是任务超时</i>
                <i>不是模型拒答</i>
                {fill !== null && fill < 0.9 ? (
                  <i>不是额度用尽，还剩 {String(Math.round((1 - fill) * 100))}%</i>
                ) : null}
              </div>
              <button
                className="aw-button"
                onClick={() => {
                  onInspect(node.runId);
                }}
                type="button"
              >
                只看这个运行的记录
              </button>
            </div>
          </section>
        ) : null}

        <section className="aw-agent-section">
          <h3>它做过什么</h3>
          {log.length === 0 ? (
            <p className="aw-agent-note">
              这一页没有收到这个运行的事件。它仍然在日志里。
            </p>
          ) : (
            <ul className="aw-agent-log">
              {log.map((row) => (
                <li className={row.failed ? "is-bad" : undefined} key={row.key}>
                  {row.text}
                </li>
              ))}
            </ul>
          )}
        </section>

        <section className="aw-agent-section">
          <h3>用量</h3>
          <p className="aw-agent-spend">
            <strong>{spendText(node)}</strong>
          </p>
          <Meter node={node} />
          <p className="aw-agent-note">
            子代理这一笔不占主运行的额度：它有自己的上限，主运行的预算看不见它。
          </p>
        </section>

        <section className="aw-agent-section">
          <h3>它被派出去时拿到的</h3>
          {/* 目标不在这里，因为 `AgentDelegated` 不带它。这一节讲的是「它被给了
              什么」，不是「它被要求做什么」——两者差一个字段，而把后者编出来是这
              块面唯一能犯的不可挽回的错。 */}
          <dl className="aw-agent-facts">
            <dt>派它的</dt>
            <dd>
              {parent === undefined
                ? "这一页没有收到派它的那个运行"
                : (parent.nodeId ?? `运行 ${shortId(parent.runId, 8)}`)}
            </dd>
            <dt>模型档</dt>
            <dd>{node.modelProfile ?? "—"}</dd>
            <dt>工具</dt>
            <dd>
              {node.toolCount === null
                ? "—"
                : node.toolCount === 0
                  ? "一个都没有——它的工具是定义的上限与任务授权信封的交集，交集可以是空的"
                  : `${String(node.toolCount)} 个`}
            </dd>
            <dt>token 上限</dt>
            <dd>{ceiling === null ? "没有声明上限" : formatTokens(ceiling)}</dd>
          </dl>
        </section>
      </div>
    </>
  );
}

export function AgentPanel({
  events,
  onClose,
  onInspect,
  onOpen,
  openRunId,
  roots,
}: {
  events: readonly EventEnvelope[];
  onClose: () => void;
  /** 把步骤流收窄到这一个运行——这是「看这次调用」今天唯一做得到的事。 */
  onInspect: (runId: string) => void;
  onOpen: (runId: string | null) => void;
  openRunId: string | null;
  roots: readonly RunNode[];
}) {
  // 读 `parentRunId` 这个字段，而不是「它在树里是不是根」。
  //
  // 两者今天大部分时候一样，但有一种情况不一样，而那一种正是要显示的：孩子的
  // 事件在这一页里、派它的那个运行的事件不在（事件窗口只留最近一段）。那时
  // `buildRunTree` 会把这个孩子**提升成根**——它的父亲不在表里——而它的
  // `parentRunId` 仍然写着。按树形判断会把它当成一个主运行漏掉，按字段判断它
  // 仍然是子代理，只是详情里那句「派它的」会诚实地说这一页没收到。
  //
  // 反过来那一种不成立：`parentRunId` 只由 `AgentDelegated` 写入
  // （`runTree.ts` 里那条规则），所以一个连委派事件都没进窗口的孩子，这一页
  // 无从知道它是谁的孩子，也就不会出现在这里。那是缺数据，不是这块面的判断错。
  const agents = useMemo(
    () => flattenRuns(roots).filter((run) => run.parentRunId !== null),
    [roots],
  );
  const open = agents.find((run) => run.runId === openRunId);
  const parent = useMemo(
    () =>
      open === undefined
        ? undefined
        : flattenRuns(roots).find((run) => run.runId === open.parentRunId),
    [roots, open],
  );

  const groups = useMemo(
    () =>
      GROUP_ORDER.map((status) => ({
        status,
        runs: agents.filter((run) => run.status === status),
      })).filter((group) => group.runs.length > 0),
    [agents],
  );

  const total = agents.reduce((sum, run) => sum + totalTokens(run.spend), 0);

  return (
    <aside aria-label="子代理" className="aw-agent-panel">
      {open === undefined ? (
        <>
          <header className="aw-agent-head">
            <span className="aw-agent-head-title">
              <strong>子代理</strong>
              <code dir="ltr">{agents.length} 个 · 后台在跑</code>
            </span>
            <button
              aria-label="收起子代理"
              className="aw-icon-button"
              onClick={onClose}
              type="button"
            >
              <PanelRightClose aria-hidden="true" size={15} />
            </button>
          </header>
          <div className="aw-agent-body">
            {groups.length === 0 ? (
              <p className="aw-agent-note aw-agent-empty">
                这个任务还没有派出子代理。
              </p>
            ) : (
              groups.map((group) => (
                <div key={group.status}>
                  <h3 className="aw-agent-group">
                    {STATUS_LABEL[group.status]}
                    <span>{group.runs.length}</span>
                  </h3>
                  <ul className="aw-agent-list">
                    {group.runs.map((run) => (
                      <AgentEntry
                        key={run.runId}
                        node={run}
                        onOpen={(runId) => {
                          onOpen(runId);
                        }}
                      />
                    ))}
                  </ul>
                </div>
              ))
            )}
          </div>
          {agents.length > 0 ? (
            <footer className="aw-agent-foot">
              <span>
                合计 <strong>{formatTokens(total)}</strong>
              </span>
              <span>不占这个任务的额度</span>
            </footer>
          ) : null}
        </>
      ) : (
        <AgentDetail
          events={events}
          node={open}
          onBack={() => {
            onOpen(null);
          }}
          onInspect={onInspect}
          parent={parent}
        />
      )}
    </aside>
  );
}

/**
 * 运行记录里那一行：子代理这件事在正文里的**全部**篇幅。
 *
 * 它替掉了 Work 页上的 `RunPanel`。那块面画的是整棵运行树，而它的三样内容现在
 * 各自有了更近的去处：子代理归副面板；同一个图节点的第二、第三次运行早就由
 * `StepStream` 折成带标题的块，标题上就写着那一次烧了多少；根运行的花销属于任务
 * 而不属于某一个 run。留着它等于把同一件事讲两遍，而讲两遍正是这一版要改掉的
 * 毛病。`RunPanel` 本身没有删——Code 会话页仍然在用它，那里没有副面板。
 *
 * 没有子代理时整行不渲染。一行「0 个子代理」既不能点也不能用，它唯一的作用是让
 * 每个没派过人的任务都多出一行。
 */
export function AgentEntryLine({
  incomplete,
  onOpen,
  open,
  roots,
}: {
  /**
   * 这条事件流有没有送达的分页。
   *
   * 这一条从 `RunPanel` 搬过来，而且必须搬：这一行在没有子代理时整个不渲染，
   * 于是「这个任务没派过人」和「派过，但那条委派落在缺口里」在屏幕上长得一模
   * 一样。少了这句话，一个不完整的流会安静地给出一个错的答案——这正是这个仓库
   * 最不能接受的那一种错。
   */
  incomplete: boolean;
  onOpen: () => void;
  /** 副面板此刻是不是正开着这一堆——开着时这一行亮起来，说明两块面在讲同一件事。 */
  open: boolean;
  roots: readonly RunNode[];
}) {
  const agents = useMemo(
    () => flattenRuns(roots).filter((run) => run.parentRunId !== null),
    [roots],
  );
  if (agents.length === 0) {
    return incomplete ? <IncompleteHint /> : null;
  }

  const running = agents.filter((run) => run.status === "running").length;
  const failed = agents.filter((run) => run.status === "failed").length;
  const total = agents.reduce((sum, run) => sum + totalTokens(run.spend), 0);
  const breakdown = [
    running > 0 ? `${String(running)} 个在跑` : null,
    failed > 0 ? `${String(failed)} 个失败` : null,
  ].filter((part): part is string => part !== null);

  return (
    <button
      className={`aw-agent-entryline${open ? " is-open" : ""}`}
      onClick={onOpen}
      type="button"
    >
      <span className="aw-agent-entryline-head">
        <strong>子代理 {agents.length} 个</strong>
        {breakdown.length > 0 && <span>{breakdown.join(" · ")}</span>}
        <span className="aw-agent-entryline-open">
          {open ? "在侧栏" : "在侧栏打开"}
        </span>
      </span>
      <span className="aw-agent-entryline-sum">
        合计 {formatTokens(total)} · 不占这个任务的额度
      </span>
    </button>
  );
}

/** 见 `AgentEntryLine` 的 `incomplete`：措辞按新的落点改过，说的是同一件事。 */
function IncompleteHint() {
  return (
    <p className="aw-run-hint is-incomplete">
      这条流有没有送达的分页，所以这里数出来的子代理<strong>可能不全</strong>
      ——一次落在缺口里的委派，看起来和「没有派过子代理」一模一样。
    </p>
  );
}

/**
 * 步骤流被收窄到一个运行时，把它放开的那个控件。
 *
 * 这一段也是从 `RunPanel` 搬过来的，理由写在那份测试的开头：那块面**持有唯一
 * 一个解除收窄的控件**，所以「没有东西值得画」和「什么都不画」不是同一条指令。
 * 副面板替掉它之后这条仍然成立——副面板可以是收起的，而收窄是记在 URL 上的，
 * 一个带 `?run=` 的链接打开时副面板并不在。
 */
export function StreamNarrowNotice({
  onClear,
  narrowedToMissingRun,
  selectedRunId,
}: {
  onClear: () => void;
  /** 收窄到了一个这条流里没有的运行——那时下面是空的，而空不等于没发生。 */
  narrowedToMissingRun: boolean;
  selectedRunId: string | null;
}) {
  if (selectedRunId === null) return null;
  return (
    <p className="aw-run-hint">
      {narrowedToMissingRun
        ? "下面的执行过程被收窄到了一个不在这条流里的运行，所以它是空的。"
        : "下面的执行过程只显示这一个运行。"}
      <button
        className="aw-run-clear"
        onClick={onClear}
        type="button"
      >
        看全部记录
      </button>
    </p>
  );
}
