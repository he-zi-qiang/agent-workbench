import {
  Check,
  ChevronRight,
  Circle,
  Clock3,
  LoaderCircle,
  Minus,
  Sparkles,
  X,
} from "lucide-react";
import type { ArtifactRef, EventEnvelope } from "../api/types";
import { CommandTrace } from "./CommandTrace";
import { LiveActivity } from "./LiveActivity";
import { StepDisclosure } from "./StepDisclosure";
import { TurnUsage, type PartialTurnUsage } from "./TurnUsage";
import { presentActivity } from "./activityPresentation";
import { foldForeignRuns, hasForeignRun, splitByRun } from "./runSections";
import { shortId } from "./ui";
import {
  groupSteps,
  summariseGroups,
  type GateStep,
  type StepGroup,
  type StepOutcome,
} from "./stepGroups";

/**
 * A run, as the stages it went through, each openable to its real steps.
 *
 * Shared between Work and Chat because the reader's question is the same in
 * both -- what did it do, and what actually went in and out of each step --
 * and answering it two different ways taught the same thing twice.
 *
 * What is *not* shared is where the stages come from. Work has a graph and
 * groups by node; a Chat turn has no `graph_node_id` at all and groups by what
 * its events mean. Those are genuinely different readings of different data,
 * so each caller derives its own stages and this renders them.
 */

export type StreamStageState =
  | "done"
  | "active"
  | "failed"
  | "waiting"
  | "pending"
  | "skipped";

export interface StreamStage {
  id: string;
  title: string;
  state: StreamStageState;
  /** Right-aligned text: a time once finished, a status while not. */
  note: string;
  /**
   * The graph nodes behind the title, when the caller has a graph.
   *
   * Absent for Chat, which has none -- its stages are derived from what the
   * events mean, so there is no node id to print and a made-up one would be
   * worse than the blank.
   */
  nodes?: string;
  /** How long the stage took, once it is over. */
  duration?: string;
  /**
   * 这一段烧了多少 token，调用方算好给。
   *
   * 由调用方给而不是这里从 `events` 里加，和 `nodes`/`note` 是同一条分工：这个
   * 组件负责画，不负责判断哪些事件该计进来。钱不在里面——费用随终止事件按运行
   * 一次写下，一段步骤加不出它，见 `PartialTurnUsage`。
   */
  usage?: PartialTurnUsage | null;
  events: EventEnvelope[];
}

export interface StreamMeta {
  title: string;
  events: EventEnvelope[];
}

/**
 * 一个**不属于这个阶段自己**的运行，在读者眼里叫什么、处在什么状态。
 *
 * 由调用方给，不由这个组件推。切段是机械的（`event.run_id` 变了就是换人了），
 * 命名不是：只有 Work 知道「这个 run_id 是一次委派，被派出去的是 analyst」——那要
 * 读 `AgentDelegated`，而 Chat 连这个事件都不会有。这条分工和文件头那句
 * 「阶段由调用方推导，这里只负责画」是同一条。
 */
export interface StreamRunLabel {
  title: string;
  /**
   * 标题前面那枚小标签，没有就不画。
   *
   * 由调用方给，因为它是一句**断言**：「子代理」这三个字说的是这个运行被谁派出去
   * 的，而组件只知道「这一段的 run_id 和上一段不一样」。同一个图节点跑第二次也
   * 会走到这里，而它不是任何人的子代理。
   */
  badge?: string;
  /** 决定这一段折不折、以及它的颜色。 */
  outcome: "running" | "done" | "failed" | "unknown";
  /** 右侧一行小字：花了多少、几条事件。没有就不画。 */
  note?: string;
}

/**
 * Success is the unmarked case. A step that worked says nothing on its line --
 * the reader is scanning for the one that did not, and a column of green ticks
 * is what makes a single failure hard to find.
 */
const OUTCOME_LABELS: Readonly<Record<StepOutcome, string>> = {
  ok: "",
  failed: "失败",
  denied: "被拒绝",
  running: "进行中",
};

function StreamMarker({ state }: { state: StreamStageState }) {
  const Icon =
    state === "done"
      ? Check
      : state === "failed"
        ? X
        : state === "active"
          ? LoaderCircle
          : state === "waiting"
            ? Clock3
            : state === "skipped"
              ? Minus
              : Circle;
  return (
    <span className="aw-stream-dot" aria-hidden="true">
      <Icon className={state === "active" ? "aw-spin" : undefined} size={10} />
    </span>
  );
}

/** One sentence on the primary timeline; the complete preview stays below. */
function reasoningSummary(text: string): string {
  const collapsed = text.replace(/\s+/g, " ").trim();
  const sentence = /^.{1,176}?[。！？.!?](?:\s|$)/u.exec(collapsed)?.[0];
  if (sentence !== undefined) return sentence.trim();
  return collapsed.length <= 176 ? collapsed : `${collapsed.slice(0, 175)}…`;
}

/**
 * A tool call's authorization sequence, on the collapsed line.
 *
 * On the line rather than inside the disclosure because the whole claim is
 * that the *order* is the argument: proposed, authorized, started, finished,
 * and a call that stopped shows which of the four it stopped at. Folded away,
 * it would only be found by a reader who already suspected something.
 *
 * Deliberately quieter than the design sketch, which paints a completed bead
 * green. Four green pills per row, on a stage that read twelve pages, is the
 * column of ticks `OUTCOME_LABELS` exists to avoid -- it makes the one denied
 * call harder to find, not easier. So a bead that went through is neutral and
 * only 被拒 / 失败 carry colour: this is a progress track, not a status column.
 */
function GateBeads({ steps }: { steps: GateStep[] | null }) {
  if (steps === null) return null;

  return (
    <span className="aw-step-gate">
      {steps.map((step) => (
        <span className={`aw-step-bead is-${step.state}`} key={step.key}>
          <span aria-hidden="true" className="aw-step-bead-dot" />
          {step.label}
        </span>
      ))}
    </span>
  );
}

export function StepStream({
  ariaLabel,
  eventTitle,
  isKnownEvent,
  meta,
  onOpenArtifact,
  running,
  runLabel,
  stages,
}: {
  ariaLabel: string;
  eventTitle: (event: EventEnvelope) => string;
  /** Unknown event types stay visible, dimmed, rather than being dropped. */
  isKnownEvent?: (event: EventEnvelope) => boolean;
  /** Run- or task-level bookkeeping, folded away under the stages. */
  meta?: StreamMeta;
  onOpenArtifact?: (artifact: ArtifactRef) => void;
  running: boolean;
  /**
   * 怎么称呼一个不属于这个阶段自己的运行，`null` 表示叫不出名字。
   *
   * 不给这个 prop（Chat 就不给）时，一个阶段的事件照旧一次性交给 `groupSteps`，
   * 渲染结果与从前逐字节相同——Chat 的一轮里只有一个运行，分段对它是无意义的一层。
   *
   * 给了、而且这个阶段里真的出现了第二个运行时，才按段渲染。返回 `null` 的段也
   * 照样装进框里，用 `运行 xxxxxxxx` 兜底：一页缺失的 `AgentDelegated` 会让页面
   * 说不出这个子运行叫什么，而那时候最不该做的事是把它的事件画成父运行干的。
   */
  runLabel?: (runId: string) => StreamRunLabel | null;
  stages: StreamStage[];
}) {
  const step = (event: EventEnvelope) => (
    <li
      className={
        isKnownEvent === undefined || isKnownEvent(event) ? "" : "is-unknown"
      }
      key={event.event_id}
    >
      <StepDisclosure
        event={event}
        title={eventTitle(event)}
        {...(onOpenArtifact === undefined ? {} : { onOpenArtifact })}
      />
    </li>
  );

  /**
   * One step as the reader names it, opening to the events it was folded from.
   *
   * A group holding a single event is rendered exactly as it always was: there
   * is nothing to fold, and wrapping it would put a second caret in front of a
   * row that already has one.
   */
  const groupStep = (group: StepGroup) => {
    const only = group.events.length === 1 ? group.events[0] : undefined;
    const presentation = presentActivity(group);
    const reasoning =
      presentation.reasoning === null ? null : (
        <p className="aw-activity-reasoning">
          <Sparkles aria-hidden="true" size={13} />
          <span>{`思路摘要 · ${reasoningSummary(presentation.reasoning)}`}</span>
        </p>
      );
    const command =
      presentation.command === null ? null : (
        <CommandTrace
          command={presentation.command}
          running={group.outcome === "running"}
        />
      );

    // A single durable event still gets the readable command/thought
    // projection. Chat deliberately keeps one activity per logical call, so a
    // live ToolProgress or its completion often is the whole group; returning
    // the raw disclosure here would hide exactly the progress we just kept.
    if (only !== undefined) {
      return (
        <li
          className={
            isKnownEvent === undefined || isKnownEvent(only) ? "" : "is-unknown"
          }
          key={group.key}
        >
          {reasoning}
          <StepDisclosure
            event={only}
            title={eventTitle(only)}
            {...(onOpenArtifact === undefined ? {} : { onOpenArtifact })}
          />
          {command}
        </li>
      );
    }

    return (
      <li key={group.key}>
        {reasoning}
        <details
          className={`aw-step-group is-${group.outcome}`}
          open={group.outcome === "running" ? true : undefined}
        >
          <summary className="aw-step-group-head">
            <ChevronRight
              aria-hidden="true"
              className="aw-step-caret"
              size={13}
            />
            <span className="aw-step-group-title">{group.title}</span>
            {group.subject === null ? null : (
              <span className="aw-step-group-subject" title={group.subject}>
                {group.subject}
              </span>
            )}
            <span className="aw-step-group-outcome">
              {OUTCOME_LABELS[group.outcome]}
            </span>
            <GateBeads steps={group.gate} />
          </summary>
          {command}
          <ol className="aw-stream-events">{group.events.map(step)}</ol>
        </details>
      </li>
    );
  };

  // The same vocabulary the rows use, so a digest cannot read half-translated:
  // without this a stage summarised as "RunStarted · 模型作答 · RunCompleted".
  const groupsOf = (events: EventEnvelope[]) => groupSteps(events, eventTitle);

  /**
   * 一个阶段的内容：要么照旧一列步骤，要么按运行切成几段。
   *
   * 切段的两个前提都要成立：调用方给了 `runLabel`（Chat 不给），并且这个阶段里
   * 真的出现了第二个运行。都不成立时走的是从前那一行代码，结果逐字节相同。
   *
   * **每段各自调 `groupSteps`，而不是先分组再按组切段。** 组的 key 是
   * `tool:${tool_call_id}`，不含 run_id；父子两个运行如果产出同号的 tool_call_id，
   * 先分组会把它们折成一个组，然后这个组只能落在某一段里——一次调用凭空归给了
   * 另一个 agent。逐段分组时这件事不可能发生。
   */
  /**
   * 一个阶段分完段之后的样子：要按段画的那几段，或者 `null` 表示照旧一列画。
   *
   * 摘要和正文都从这里取，所以那句 `summariseGroups` 数的是和正文里同一批组。
   * 分开算的话，摘要走「父子合在一起分组」、正文走「逐段分组」，两边对 tool_call_id
   * 撞号的处理不同，一个阶段可以显示「3 步」而展开之后是 4 行。
   */
  const sectionsOf = (events: EventEnvelope[]) => {
    if (runLabel === undefined) return null;
    const sections = splitByRun(events);
    // 先切再并：切是机械的，并只动别人的运行。并发的两个子代理在事件层面交错，
    // 只切不并会把四个子代理摊成十个一两条事件的小块——那是调度器的痕迹，不是
    // 任何人做过的决定，见 `runSections.ts` 里的实测数字。
    return hasForeignRun(sections) ? foldForeignRuns(sections) : null;
  };

  const stageBody = (
    events: EventEnvelope[],
    sections: ReturnType<typeof sectionsOf>,
  ) => {
    if (sections === null) {
      return <ol className="aw-stream-events">{groupsOf(events).map(groupStep)}</ol>;
    }
    return (
      <ol className="aw-stream-events">
        {sections.map((section, index) => {
          const groups = groupsOf(section.events);
          if (section.own) {
            return groups.map(groupStep);
          }
          const label = runLabel?.(section.runId) ?? null;
          // 叫不出名字也要装进框里。这是这一层取代「子代理 X：」前缀之后唯一
          // 不能丢的那半：`readDelegations` 读不到这一页的 `AgentDelegated` 时
          // 说不出这个运行是谁，而把它的事件画成父运行干的是这里唯一错的答案。
          const title = label?.title ?? `运行 ${shortId(section.runId, 8)}`;
          const outcome = label?.outcome ?? "unknown";
          return (
            // key 带上序号：同一个子运行被穿插两次就是两段，而它们的 runId 相同。
            <li key={`${section.runId}#${String(index)}`}>
              <details
                className={`aw-run-section is-${outcome}`}
                open={outcome === "running" ? true : undefined}
              >
                <summary className="aw-run-section-head">
                  <ChevronRight
                    aria-hidden="true"
                    className="aw-step-caret"
                    size={13}
                  />
                  {/* 徽标是调用方给的，因为它是一句断言。叫不出名字的那一段
                      什么都不挂：它只知道「这些事件来自另一个运行」，而那可能是
                      一次委派，也可能是这一页没收到的别的什么。 */}
                  {label?.badge === undefined ? null : (
                    <span className="aw-run-section-badge">{label.badge}</span>
                  )}
                  <span className="aw-run-section-title">{title}</span>
                  <span className="aw-run-section-count">
                    {summariseGroups(groups)}
                  </span>
                  {label?.note === undefined ? null : (
                    <span className="aw-run-section-note">{label.note}</span>
                  )}
                </summary>
                <ol className="aw-stream-events">{groups.map(groupStep)}</ol>
              </details>
            </li>
          );
        })}
      </ol>
    );
  };
  const activeStage = stages.find((stage) => stage.state === "active");
  const activeGroups = activeStage === undefined ? [] : groupsOf(activeStage.events);
  const activeGroup =
    activeGroups.find((group) => group.outcome === "running") ?? activeGroups.at(-1);
  const activePresentation =
    activeGroup === undefined ? null : presentActivity(activeGroup);
  const activeKind =
    activePresentation?.toolName !== null && activePresentation?.toolName !== undefined
      ? "tool"
      : activePresentation?.reasoning !== null &&
          activePresentation?.reasoning !== undefined
        ? "thinking"
        : "workflow";
  const completedStages = stages.filter(
    (stage) => stage.state === "done" || stage.state === "skipped",
  ).length;

  return (
    <section className="aw-stream" aria-label={ariaLabel}>
      {!running || activeStage === undefined ? null : (
        <LiveActivity
          detail={
            activeGroup?.subject === null || activeGroup?.subject === undefined
              ? activeStage.title
              : `${activeStage.title} · ${activeGroup.subject}`
          }
          kind={activeKind}
          meta={`${String(completedStages)} / ${String(stages.length)} 阶段`}
          title={activeGroup?.title ?? activeStage.title}
        />
      )}
      <ol className="aw-stream-steps">
        {stages.map((stage) => {
          const sections = sectionsOf(stage.events);
          const groups =
            sections === null
              ? groupsOf(stage.events)
              : sections.flatMap((section) => groupsOf(section.events));
          return (
          <li className={`aw-stream-state is-${stage.state}`} key={stage.id}>
            <StreamMarker state={stage.state} />
            {stage.events.length === 0 ? (
              <div className="aw-stream-head is-empty">
                <span className="aw-stream-title">{stage.title}</span>
                <span className="aw-stream-note">{stage.note}</span>
              </div>
            ) : (
              // Only the stage that is moving is open. A finished run collapses
              // to one line per stage, and opening one shows the prompts, tool
              // calls and outputs that produced it.
              <details
                className="aw-stream-step"
                open={running && stage.state === "active"}
              >
                <summary className="aw-stream-head">
                  <ChevronRight
                    aria-hidden="true"
                    className="aw-step-caret"
                    size={13}
                  />
                  <span className="aw-stream-title">{stage.title}</span>
                  {/* 图里的节点名。标题是读者的话，这是日志的话——手里拿着一条
                      trace、一份 ADR 或某个事件的 graph_node_id 的人，没有别的
                      地方能把「收集资料」和 research_internal 对上。 */}
                  {stage.nodes === undefined ? null : (
                    <code className="aw-stream-nodes">{stage.nodes}</code>
                  )}
                  {/* What it did, not how much of it. A collapsed stage is the
                      only thing on screen until somebody clicks, and "16 步"
                      spends that line on a quantity -- the count is still there
                      inside the digest, as the ×N on each kind. */}
                  <span className="aw-stream-count" title={`${groups.length} 步`}>
                    {summariseGroups(groups)}
                  </span>
                  {stage.usage === undefined || stage.usage === null ? null : (
                    <TurnUsage usage={stage.usage} />
                  )}
                  {stage.duration === undefined ? null : (
                    <span className="aw-stream-duration">{stage.duration}</span>
                  )}
                  <span className="aw-stream-note">{stage.note}</span>
                </summary>
                {stageBody(stage.events, sections)}
              </details>
            )}
          </li>
          );
        })}
      </ol>
      {meta === undefined || meta.events.length === 0 ? null : (
        <details className="aw-stream-meta">
          <summary>
            <ChevronRight aria-hidden="true" className="aw-step-caret" size={13} />
            {meta.title}
            <span>{meta.events.length} 条</span>
          </summary>
          {/* Counted as events, not steps: this block is the bookkeeping, and
              a reader who opened it came for the rows themselves. */}
          <ol className="aw-stream-events">{meta.events.map(step)}</ol>
        </details>
      )}
    </section>
  );
}
