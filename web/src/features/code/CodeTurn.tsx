/**
 * One turn of a coding session, as the transcript of what happened under it.
 *
 * In order, and the order is the whole argument: the instruction, then a
 * timeline of steps -- **each thought sitting directly above the action it
 * caused** -- then the files it produced and the report. Raw protocol data is
 * kept in one quiet diagnostic disclosure rather than mixed into that reading
 * order.
 *
 * ## Why the thought is on the step and not in a list of its own
 *
 * The previous shape collected every excerpt of a turn into one 想过什么
 * disclosure at the foot of the block. Both that list and the action list were
 * built from the same ordered array of events, and nothing put them back
 * together -- so a reader got a column of paragraphs that could not answer the
 * one question reasoning exists to answer: *why did it run that command*. It
 * also cost two clicks to see a single command.
 *
 * The ordering was never missing. `groupSteps` already files a tool-calling
 * model turn **ahead of** the first call it named, so the `ModelCompleted`
 * carrying `thinking_preview` is already in the right group, in the right
 * position (`stepGroups.ts`, pinned by `stepGroups.test.ts`). `buildTurnBlocks`
 * simply stopped throwing that away.
 *
 * This is the shape Codex renders in its scrollback -- reasoning dim and
 * italic, immediately above the action it produced -- and the shape a Claude
 * Code transcript stores, where records alternate thinking, tool_use, tool_use,
 * thinking. It is a coding session's natural reading order, not a borrowed one.
 *
 * ## The reasoning is still rendered exactly once
 *
 * That invariant did not go away; its implementation changed from "two disjoint
 * sets" to "a promotion in place". `useCodeStream` clears the live thought the
 * moment that call's `ModelCompleted` arrives, and the step for that same
 * `model_call_id` renders the durable excerpt in the very position the live
 * text occupied -- same React key, so the element is not remounted and a
 * disclosure the reader had opened stays open. What used to be an erase
 * followed by an append somewhere else is now one row that settles.
 *
 * Before any of this, one call's excerpt could be on screen three times at
 * once: streaming at the top of the page, formatted inside its step, and
 * verbatim again in that step's raw JSON dump.
 *
 * ## Why a produced file is a card here and not only a row over there
 *
 * "生成的文件应该在对话生成中" is a claim about *when*, not only about where.
 * The card appears as soon as the `ToolCompleted` that wrote it arrives -- the
 * turn is still running, there is no report yet -- because that is the moment
 * the file became a fact. A file list in a side panel answers "what is in the
 * workspace"; it cannot answer "what did that instruction make", which is the
 * question a reader has while watching.
 */

import {
  Check,
  FilePenLine,
  FileSearch,
  FolderOpen,
  MoreHorizontal,
  Search,
  Sparkles,
  TerminalSquare,
  Wrench,
  X,
} from "lucide-react";
import { useEffect, useRef, useState } from "react";
import type { WorkspaceEntryView } from "../../api/types";
import { CommandTrace } from "../../components/CommandTrace";
import { LiveActivity, type LiveActivityKind } from "../../components/LiveActivity";
import { MarkdownContent } from "../../components/MarkdownContent";
import { TurnUsage } from "../../components/TurnUsage";
import { presentActivity } from "../../components/activityPresentation";
import type { StepGroup } from "../../components/stepGroups";
import type { ToolProgressView } from "./useCodeStream";
import { FileCard } from "./FileCard";
import type { CodeTurnBlock, TurnStep } from "./turnBlocks";

/** Success is the unmarked case: a reader scans for the one that did not. */
const OUTCOME_LABELS: Readonly<Record<StepGroup["outcome"], string>> = {
  ok: "",
  failed: "失败",
  denied: "被拒绝",
  running: "进行中",
};

export function CodeTurn({
  block,
  files,
  liveAnswer,
  liveThinking,
  liveThinkingCallId,
  onOpen,
  toolProgress,
  openedName,
}: {
  block: CodeTurnBlock;
  /** The current listing, for sizes, media types, and whether a name still exists. */
  files: WorkspaceEntryView[];
  /** The report as it is being written. Non-empty only on the live block. */
  liveAnswer: string;
  /** Non-empty only on the live block, and only while a call is reasoning. */
  liveThinking: string;
  /** Which model call that live text belongs to, so it lands on its own step. */
  liveThinkingCallId: string;
  onOpen: (name: string) => void;
  openedName: string | null;
  /** Live tool progress by `tool_call_id`. Empty except on the live block. */
  toolProgress: ReadonlyMap<string, ToolProgressView>;
}) {
  const activeStep = [...block.steps]
    .reverse()
    .find(
      (step) =>
        step.modelCallId === liveThinkingCallId ||
        (step.group !== null && step.group.outcome === "running"),
    );
  const activeProgress =
    activeStep?.group === null || activeStep?.group === undefined
      ? undefined
      : toolProgress.get(toolCallIdOf(activeStep.group));
  const liveStatus = codeLiveStatus({
    activeProgress,
    activeStep,
    answer: liveAnswer,
    blockLive: block.live,
    thinking: liveThinking,
  });

  return (
    <li className="aw-code-turn">
      <div className="aw-code-said">
        <h3>你</h3>
        <p>{block.instruction}</p>
      </div>

      {liveStatus === null ? null : (
        <LiveActivity
          detail={liveStatus.detail}
          kind={liveStatus.kind}
          {...(liveStatus.meta === null ? {} : { meta: liveStatus.meta })}
          title={liveStatus.title}
        />
      )}

      {block.steps.length === 0 ? null : (
        <ol aria-label="这一轮做了什么" className="aw-code-steps">
          {block.steps.map((step) => (
            <TurnStepRow
              // Keyed by the model call, not the group. The same call is
              // `model:mc_2` while it is open and `tool:call_x` once its
              // ModelCompleted merges it into the call it named -- keying by
              // the group would remount at exactly that moment and reset the
              // disclosure the reader is mid-sentence in.
              key={step.modelCallId ?? step.key}
              live={
                step.modelCallId !== "" &&
                step.modelCallId === liveThinkingCallId
              }
              liveThinking={liveThinking}
              step={step}
              toolProgress={toolProgress}
            />
          ))}
        </ol>
      )}

      {block.produced.length === 0 ? null : (
        <ul aria-label="这一轮产出的文件" className="aw-code-outputs">
          {block.produced.map((file) => {
            const entry = files.find((held) => held.name === file.name);
            return (
              <FileCard
                entry={entry}
                file={file}
                key={file.toolCallId + file.name}
                onOpen={onOpen}
                opened={file.name === openedName}
              />
            );
          })}
        </ul>
      )}

      {/* The durable report wins the moment it exists; until then the live
          stream is the only copy, and it is what the reader watches arrive.
          Writing a report is the longest stretch of a turn -- ten to twenty
          seconds -- and before this the console showed nothing moving for all
          of it and then pasted the finished text in whole.

          Plain text while streaming, Markdown once settled: half a fenced
      block renders as garbage, and the report reliably contains fences. */}
      {block.report !== null ? (
        <section aria-label="回答" className="aw-code-report">
          <MarkdownContent text={block.report} />
        </section>
      ) : liveAnswer === "" ? null : (
        <section aria-label="正在回答" className="aw-code-report is-streaming">
          <p>{liveAnswer}</p>
        </section>
      )}

      {/* 在答案下面、原始事件上面。和 Chat 那一处同一个位置、同一个零件：三个
          模式对同一个数说同一句话，是这个脚注唯一要守住的东西。 */}
      <TurnUsage usage={block.usage} />

      {block.events.length === 0 ? null : (
        <details className="aw-code-raw">
          <summary title="查看这一轮的原始事件">
            <MoreHorizontal aria-hidden="true" size={14} />
            <span>原始事件</span>
          </summary>
          <pre>{JSON.stringify(block.events, null, 2)}</pre>
        </details>
      )}
    </li>
  );
}

/** How long a folded thought's first line may be before it stops being a line. */
const THOUGHT_HEAD_MAX = 120;

/**
 * One step: what it thought, then what it did.
 *
 * The thought is above the action inside one `<li>`, which is the entire
 * change. A step with no thought is just its action row; a step with no action
 * is either the answering turn (the report follows it) or a call still in
 * flight (the live text lands here).
 */
function TurnStepRow({
  live,
  liveThinking,
  step,
  toolProgress,
}: {
  live: boolean;
  liveThinking: string;
  step: TurnStep;
  toolProgress: ReadonlyMap<string, ToolProgressView>;
}) {
  // While a call is streaming, the durable excerpt does not exist yet -- the
  // live text is the only text there is. When `ModelCompleted` lands, the two
  // swap without the row moving.
  const text = live && liveThinking !== "" ? liveThinking : step.thinking;
  // Only for a step that has not come back. The map is keyed by call and is
  // emptied when one returns, so this is almost always already undefined --
  // the `running` check is what makes it *never* possible for a settled row to
  // carry a moving line, rather than merely unlikely.
  const progress =
    step.group !== null && step.group.outcome === "running"
      ? toolProgress.get(toolCallIdOf(step.group))
      : undefined;
  const presentation = step.group === null ? null : presentActivity(step.group);

  return (
    <li
      aria-current={step.group?.outcome === "running" ? "step" : undefined}
      className={`aw-code-step${live ? " is-live" : ""}${
        step.group?.outcome === "running" ? " is-running" : ""
      }`}
    >
      {text === "" ? null : <Thought live={live} text={text} />}
      {step.group === null ? null : (
        <div className={`aw-code-action is-${step.group.outcome}`}>
          <ActionIcon
            outcome={step.group.outcome}
            toolName={presentation?.toolName ?? null}
          />
          <span className="aw-code-action-title">{step.group.title}</span>
          {step.group.subject === null ? null : (
            <span className="aw-code-action-subject" title={step.group.subject}>
              {step.group.subject}
            </span>
          )}
          <span className="aw-code-action-outcome">
            {OUTCOME_LABELS[step.group.outcome]}
          </span>
        </div>
      )}
      {presentation?.command === null || presentation?.command === undefined ? null : (
        <CommandTrace
          command={presentation.command}
          running={step.group?.outcome === "running"}
        />
      )}
      {progress === undefined ? null : <Progress progress={progress} />}
    </li>
  );
}

function ActionIcon({
  outcome,
  toolName,
}: {
  outcome: StepGroup["outcome"];
  toolName: string | null;
}) {
  const Icon =
    outcome === "failed" || outcome === "denied"
      ? X
      : outcome === "ok"
        ? Check
        : toolName === "sandbox_run"
          ? TerminalSquare
          : toolName === "workspace_read"
            ? FileSearch
            : toolName === "workspace_list"
              ? FolderOpen
              : toolName === "workspace_grep"
                ? Search
                : toolName === "workspace_write" || toolName === "workspace_edit"
                  ? FilePenLine
                  : Wrench;
  return (
    <span className="aw-code-action-icon" aria-hidden="true">
      <Icon size={13} />
    </span>
  );
}

/**
 * What a still-running call is doing, and for how long.
 *
 * `aria-live="polite"`, because this is the row a reader is watching to decide
 * whether to keep waiting, and a reader using a screen reader is the one who
 * cannot glance at it. `polite` rather than `assertive`: it updates every few
 * seconds, and interrupting a reader that often to say "still running" would
 * make the console unusable for exactly the people the attribute is for.
 */
function Progress({ progress }: { progress: ToolProgressView }) {
  return (
    <div className="aw-code-action-progress">
      {progress.lines.length === 0 ? null : (
        // `aria-live` on the block rather than on each line, so a screen
        // reader announces what was added instead of re-reading the window
        // every time it scrolls.
        <ol aria-live="polite" className="aw-code-action-progress-lines">
          {progress.lines.map((line, index) => (
            // Indexed keys, which is right here for the reason they are
            // usually wrong: this is a fixed-length window over an append-only
            // stream, so position *is* the identity -- and the same line of
            // output can legitimately repeat, which makes the text itself no
            // key at all. No eslint suppression: the rule that would object is
            // not configured in this project, and a disable comment naming a
            // rule that does not exist is itself an error here.
            // The live region is the LAST line and nothing else. Index keys
            // make that node stable while its text changes, which is exactly
            // what a polite region wants: the newest line is the only thing
            // that is news. A duplicate sr-only copy would have worked too,
            // and was wrong for a duller reason -- the same sentence would
            // then be on the page twice, which is a thing tests and readers
            // both trip over.
            <li
              aria-live={
                index === progress.lines.length - 1 ? "polite" : undefined
              }
              className={index === progress.lines.length - 1 ? "is-latest" : undefined}
              key={index}
            >
              {line}
            </li>
          ))}
        </ol>
      )}
      <p className="aw-code-action-progress-meta">
        {progress.elapsedMs === null ? null : (
          <span className="aw-code-action-progress-clock">
            已运行 {formatElapsed(progress.elapsedMs)}
          </span>
        )}
        {progress.percent === null ? null : (
          <span className="aw-code-action-progress-percent">
            {progress.percent}%
          </span>
        )}
      </p>
    </div>
  );
}

/**
 * The call a tool group is about.
 *
 * Read out of the events rather than parsed off `group.key`. The key's
 * `tool:` prefix is `groupSteps`'s private business, and a reader of this file
 * would have no way to know that slicing four characters off a string is
 * load-bearing.
 */
function toolCallIdOf(group: StepGroup): string {
  for (const event of group.events) {
    const held = (event.payload as Record<string, unknown>).tool_call_id;
    if (typeof held === "string") return held;
  }
  return "";
}

/**
 * Milliseconds as a reader says them.
 *
 * Whole seconds under a minute, then minutes and seconds. No tenths: the beat
 * that produces this arrives every few seconds, so a tenths digit would be
 * precision the number does not have -- and a figure that reads as exact while
 * being five seconds stale is worse than a coarser one that is honest.
 */
function formatElapsed(ms: number): string {
  const seconds = Math.max(0, Math.floor(ms / 1000));
  if (seconds < 60) return `${seconds} 秒`;
  return `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒`;
}

/**
 * A thought, folded to its first sentence.
 *
 * Head as the summary and the rest as the body, rather than truncation with an
 * ellipsis: truncating throws away the half a reader came for, folding only
 * puts it behind one click. The unit is one thought, not a whole turn's worth
 * -- opening the reasoning behind one command should not unroll five others.
 *
 * A thought short enough to have no body is a plain `<p>`, not a `<details>`
 * with nothing in it: a caret that opens onto emptiness is worse than no caret.
 *
 * ## 为什么它在落定的那一刻收回去
 *
 * 上一版是 `useState(live)`，注释写的是「读者在它流的时候打开过，落定之后就
 * 让它开着」——理由本身是对的，认错的是**谁打开的**。没有人打开它：它是因为
 * 正在流才展开的，而 `live` 从来不是一次表态。于是一轮十步走完，屏幕上留下
 * 十段全文展开的推理，中间夹着十行动作——这正是「思考过程太乱、还长」说的
 * 那件事，而且它随轮次变长而变差。
 *
 * 现在两件事分开记：`open` 仍然由 `live` 起头，但多一个 `touched`，只有
 * `onToggle` 会把它置真。落定时，碰过的保持原样，没碰过的收回摘要。读者的
 * 表态因此仍然被尊重——只是「它自己展开过」不再算作表态。
 *
 * 这也是 Codex 的 scrollback 与 Claude Code 的记录采取的形状：推理默认是一
 * 行暗色的摘要，展开是读者要来的，不是它自己留下的。
 */
function Thought({ live, text }: { live: boolean; text: string }) {
  const { head, body } = splitThought(text);
  const [open, setOpen] = useState(live);
  // 记的是「有人动过这一段」，不是「这一段现在是开的」。两者在流式展开的那一
  // 刻看起来一样，在落定的那一刻才分开，而分开的那一刻正是下面唯一要判断的。
  //
  // 挂在 `<summary>` 的 click 上，不是挂在 `<details>` 的 toggle 上，而这不是
  // 口味问题——第一版挂在 toggle 上，于是这整段代码什么也没做。React 是先建出
  // `<details>` 再把 `open` 设成 true 的，而设这个属性本身就会派发一次
  // `toggle`：每一段流式推理在挂载的那一刻都「被人动过」了，落定时便一段也不
  // 收。click 没有这个问题，因为没有人合成它——鼠标点摘要是 click，键盘在摘要
  // 上按 Enter／空格也是 click，而 React 同步 DOM 属性不是。
  const touched = useRef(false);
  // 落定即收，除非碰过。`live` 从真变假是这个 effect 唯一的触发条件——
  // 它不该在读者每次开合时再跑一遍，那会把刚打开的那一段立刻关掉。
  useEffect(() => {
    if (live || touched.current) return;
    setOpen(false);
  }, [live]);
  const className = live
    ? "aw-code-step-thought is-live"
    : "aw-code-step-thought";

  if (body === "") {
    return (
      <p className={className}>
        <ThoughtLabel live={live} />
        <span className="aw-code-thought-head">{head}</span>
      </p>
    );
  }
  return (
    <details
      className={className}
      onToggle={(event) => {
        setOpen(event.currentTarget.open);
      }}
      open={open}
    >
      <summary
        onClick={() => {
          touched.current = true;
        }}
      >
        <ThoughtLabel live={live} />
        {/* 一个 span，因为它要能被钳住。`THOUGHT_HEAD_MAX` 是 120 个**字符**，
            而屏幕关心的是**行**：120 个汉字在一栏 820px 的正文里是两到三行，
            一轮四步就是八到十二行灰斜体——**在全部收起的状态下**。折叠解决了
            「正文有多长」，没解决「摘要有多长」，而后者才是读者扫这一列时看
            见的东西。 */}
        <span className="aw-code-thought-head">{head}</span>
      </summary>
      <p className="aw-code-step-thought-body">{body}</p>
    </details>
  );
}

/**
 * 谁在说这一行。
 *
 * 落定之后不再写「思考摘要」四个字，只留那颗星。一轮里有几步就有几段推理，
 * 而每一段都顶着同一个词，屏幕上就多出一列重复的标签——它对第一段是说明，
 * 对后面每一段都只是噪声。图标已经把「这是推理，不是动作」说清楚了，而且是
 * 在不占一行文字的前提下说的。
 *
 * 「正在思考」留着，因为它不是标签是状态：它回答的是「它此刻在干什么」，
 * 而那个问题只在一个地方成立，也只有一段会顶着它。
 *
 * 落定那一段的 `aria-label` 补在 sr-only 里，不靠图标。图标是 `aria-hidden`，
 * 去掉可见文字之后，屏幕阅读器听到的会是一段没有出处的话。
 */
function ThoughtLabel({ live }: { live: boolean }) {
  return (
    <span className="aw-code-thought-label">
      <Sparkles aria-hidden="true" size={12} />
      {live ? "正在思考" : <span className="aw-sr-only">思考摘要</span>}
    </span>
  );
}

function codeLiveStatus({
  activeProgress,
  activeStep,
  answer,
  blockLive,
  thinking,
}: {
  activeProgress: ToolProgressView | undefined;
  activeStep: TurnStep | undefined;
  answer: string;
  blockLive: boolean;
  thinking: string;
}): { title: string; detail: string; kind: LiveActivityKind; meta: string | null } | null {
  if (!blockLive && activeProgress === undefined) return null;
  // 有思考在流的时候，这里什么也不说。
  //
  // ADR-064 把「正在思考」搬到了它促成的那一步上：那一行**就是**指示灯，它带
  // 着模型此刻写出来的字。这里曾经在它正上方再画一条「正在思考下一步 / 分析目
  // 标并选择接下来的动作」——同一件事说两遍，而且第二遍是一句通用的旁白，压在
  // 一句具体的话上面。ADR-064 删掉过它，它又回来了：`CodePage.test.tsx` 里那条
  // 测试只正向断言「思考是一行」，没有反向钉住「上面没有那条横幅」，于是回归
  // 时无人报警。这一次连反向断言一起补上。
  //
  // `return null` 而不是往下落：往下会落到 `activeStep.group` 那一支（第一次
  // 工具调用之前它是 null），最后落到「正在处理任务 / 等待下一条执行记录」——
  // 一句比它替掉的那句更空的话，压在同一段思考上面。
  if (thinking !== "") return null;
  if (activeStep?.group !== null && activeStep?.group !== undefined) {
    return {
      title: activeStep.group.title,
      // The terminal block directly below owns stdout/stderr and elapsed time.
      // Repeating its newest line here makes one piece of output look like two
      // events and causes screen readers to announce it twice.
      detail: activeStep.group.subject ?? "命令正在执行",
      kind: "tool",
      meta: null,
    };
  }
  if (answer !== "") {
    return {
      title: "正在整理结果",
      detail: "回答正在逐字生成",
      kind: "answer",
      meta: null,
    };
  }
  return {
    title: "正在处理任务",
    detail: "等待下一条执行记录",
    kind: "workflow",
    meta: null,
  };
}

/**
 * Split a thought into the line that stands for it and the rest.
 *
 * A newline first, because a model writing reasoning uses its own line breaks
 * and the first line is the heading it wrote for itself. Then a Chinese
 * sentence mark. An ASCII full stop only when whitespace follows it -- bare
 * `.` would cut `notes.md` and `0.5` in half.
 */
function splitThought(text: string): { head: string; body: string } {
  const trimmed = text.trim();
  const window = trimmed.slice(0, THOUGHT_HEAD_MAX);
  let end = window.indexOf("\n");
  if (end === -1) {
    for (const mark of ["。", "！", "？"]) {
      const at = window.indexOf(mark);
      if (at !== -1 && (end === -1 || at < end)) end = at;
    }
    if (end !== -1) end += 1;
  }
  if (end === -1) {
    const match = /\.\s/.exec(window);
    if (match !== null) end = match.index + 1;
  }
  // No ellipsis when it is cut hard: the disclosure triangle already says
  // there is more underneath.
  if (end === -1) {
    end =
      trimmed.length <= THOUGHT_HEAD_MAX ? trimmed.length : THOUGHT_HEAD_MAX;
  }
  return {
    head: trimmed.slice(0, end).trim(),
    body: trimmed.slice(end).trim(),
  };
}
