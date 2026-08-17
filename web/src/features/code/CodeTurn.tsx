/**
 * One turn of a coding session, as the transcript of what happened under it.
 *
 * In order, and the order is the whole argument: the instruction, then a
 * timeline of steps -- **each thought sitting directly above the action it
 * caused** -- then the files it produced, the report, and the raw events folded
 * away. Nothing between the reader and "what did it do, and why" is behind a
 * disclosure.
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

import { FileCode2, FileText, Image as ImageIcon } from "lucide-react";
import { useState } from "react";
import type { PrincipalIdentity, WorkspaceEntryView } from "../../api/types";
import { MarkdownContent } from "../../components/MarkdownContent";
import { mediaLabel, previewKind } from "../../components/media";
import type { StepGroup } from "../../components/stepGroups";
import { formatSize } from "../../components/ui";
import { FilePreview } from "./FilePreview";
import type { CodeTurnBlock, ProducedFile, TurnStep } from "./turnBlocks";

/**
 * How large a produced file may be and still be previewed *without being asked*.
 *
 * The click has no ceiling -- `TextPreview` will show the head of a 5 MB file
 * and say it was cut, which is a view worth having. This bounds the other
 * case: a preview the reader did not ask for, whose fetch they did not choose
 * to spend. 64 KB is about 1,500 lines of source, already many times what the
 * 360px inline box shows, so anything past it was being transferred for bytes
 * nobody was going to read.
 */
const AUTO_PREVIEW_MAX_BYTES = 64 * 1024;

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
  identity,
  liveThinking,
  liveThinkingCallId,
  onOpen,
  openedName,
  sessionId,
}: {
  block: CodeTurnBlock;
  /** The current listing, for sizes, media types, and whether a name still exists. */
  files: WorkspaceEntryView[];
  identity: PrincipalIdentity;
  /** Non-empty only on the live block, and only while a call is reasoning. */
  liveThinking: string;
  /** Which model call that live text belongs to, so it lands on its own step. */
  liveThinkingCallId: string;
  onOpen: (name: string) => void;
  openedName: string | null;
  sessionId: string;
}) {
  // One auto-opened inline preview per turn: the last previewable thing it
  // produced. Not gated on `live` -- a reader scrolling back to a turn is
  // asking what it made, and answering with a filename they have to click is
  // answering a different question. One per turn keeps a five-file turn from
  // becoming five stacked frames.
  const autoPreview = lastPreviewable(block.produced, files);

  // The live thought's fallback. `ModelStarted` is durable and arrives before
  // the first delta, so a step to land on normally exists; a truncated stream
  // that lost it must not make the text the reader is watching disappear.
  const liveOrphan =
    block.live &&
    liveThinking !== "" &&
    !block.steps.some((step) => step.modelCallId === liveThinkingCallId);

  return (
    <li className="aw-code-turn">
      <div className="aw-code-said">
        <h3>你</h3>
        <p>{block.instruction}</p>
      </div>

      {block.steps.length === 0 && !liveOrphan ? null : (
        <ol aria-label="这一轮做了什么" className="aw-code-steps">
          {block.steps.map((step) => (
            <TurnStepRow
              // Keyed by the model call, not the group. The same call is
              // `model:mc_2` while it is open and `tool:call_x` once its
              // ModelCompleted merges it into the call it named -- keying by
              // the group would remount at exactly that moment and reset the
              // disclosure the reader is mid-sentence in.
              key={step.modelCallId ?? step.key}
              live={step.modelCallId !== "" && step.modelCallId === liveThinkingCallId}
              liveThinking={liveThinking}
              step={step}
            />
          ))}
          {liveOrphan ? (
            <li className="aw-code-step is-live">
              <Thought live text={liveThinking} />
            </li>
          ) : null}
        </ol>
      )}

      {block.produced.length === 0 ? null : (
        <ul aria-label="这一轮产出的文件" className="aw-code-outputs">
          {block.produced.map((file) => (
            <FileCard
              autoPreview={file.name === autoPreview}
              entry={files.find((held) => held.name === file.name)}
              file={file}
              identity={identity}
              key={file.toolCallId + file.name}
              onOpen={onOpen}
              opened={file.name === openedName}
              sessionId={sessionId}
            />
          ))}
        </ul>
      )}

      {block.report === null ? null : (
        <div className="aw-code-report">
          <h3>报告</h3>
          {/* The agent's own prose, and it arrives as Markdown -- lists, file
              names in backticks, occasionally a fenced diff. Rendered as a
              paragraph it was one run-on block with the syntax still in it. */}
          <MarkdownContent text={block.report} />
        </div>
      )}

      {block.events.length === 0 ? null : (
        <details className="aw-code-raw">
          <summary>原始事件（{block.events.length}）</summary>
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
}: {
  live: boolean;
  liveThinking: string;
  step: TurnStep;
}) {
  // While a call is streaming, the durable excerpt does not exist yet -- the
  // live text is the only text there is. When `ModelCompleted` lands, the two
  // swap without the row moving.
  const text = live && liveThinking !== "" ? liveThinking : step.thinking;

  return (
    <li className={`aw-code-step${live ? " is-live" : ""}`}>
      {text === "" ? null : <Thought live={live} text={text} />}
      {step.group === null ? null : (
        <div className={`aw-code-action is-${step.group.outcome}`}>
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
    </li>
  );
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
 */
function Thought({ live, text }: { live: boolean; text: string }) {
  const { head, body } = splitThought(text);
  // Initialised from `live` and never synced back. A thought the reader opened
  // while it streamed stays open when the turn settles -- the same reason
  // `FileCard`'s `inlineOpen` is state and not a controlled prop.
  const [open, setOpen] = useState(live);
  const className = live
    ? "aw-code-step-thought is-live"
    : "aw-code-step-thought";

  if (body === "") return <p className={className}>{head}</p>;
  return (
    <details
      className={className}
      onToggle={(event) => {
        setOpen(event.currentTarget.open);
      }}
      open={open}
    >
      <summary>{head}</summary>
      <p className="aw-code-step-thought-body">{body}</p>
    </details>
  );
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
    end = trimmed.length <= THOUGHT_HEAD_MAX ? trimmed.length : THOUGHT_HEAD_MAX;
  }
  return { head: trimmed.slice(0, end).trim(), body: trimmed.slice(end).trim() };
}

function FileCard({
  autoPreview,
  entry,
  file,
  identity,
  onOpen,
  opened,
  sessionId,
}: {
  autoPreview: boolean;
  /** Undefined when the name is no longer in the workspace listing. */
  entry: WorkspaceEntryView | undefined;
  file: ProducedFile;
  identity: PrincipalIdentity;
  onOpen: (name: string) => void;
  opened: boolean;
  sessionId: string;
}) {
  const [inlineOpen, setInlineOpen] = useState(autoPreview);
  const kind = entry === undefined ? "none" : previewKind(entry.media_type);
  const Icon =
    kind === "html" ? FileCode2 : kind === "image" ? ImageIcon : FileText;

  return (
    <li className="aw-code-output">
      <button
        aria-current={opened ? "true" : undefined}
        className="aw-code-output-open"
        disabled={entry === undefined}
        onClick={() => {
          onOpen(file.name);
        }}
        type="button"
      >
        <Icon aria-hidden size={16} />
        <span className="aw-code-output-name">{file.name}</span>
        <span className="aw-code-output-meta">{metaOf(file, entry)}</span>
      </button>

      {/* Text is in this list, and leaving it out was the bug this fixes: a
          coding session's product is usually a `.py` or a `.ts`, and a card
          that showed only its name meant the code the agent had just written
          was the one thing not in the conversation. It is also the cheapest
          kind here -- a `<pre>`, against an iframe or a decoded blob -- so the
          cost argument that justified the image/html gate never applied to it.

          PDF and .docx stay out. Both are paged documents whose viewer wants
          the height the panel gives it; in a 360px box they are a postage
          stamp, and the card's click already routes there.

          Mounted only when open, so a turn with six cards is not six fetches.
          Each preview component keeps its own size ceiling, judged from the
          listing's byte count before any transfer. */}
      {entry !== undefined &&
      (kind === "text" || kind === "image" || kind === "html") ? (
        <details
          className="aw-code-output-inline"
          onToggle={(event) => {
            setInlineOpen(event.currentTarget.open);
          }}
          open={inlineOpen}
        >
          <summary>就地预览</summary>
          {inlineOpen ? (
            <div className="aw-code-output-inline-body">
              <FilePreview
                identity={identity}
                viewing={{
                  sessionId,
                  name: file.name,
                  mediaType: entry.media_type,
                  sizeBytes: entry.size_bytes,
                }}
              />
            </div>
          ) : null}
        </details>
      ) : null}
    </li>
  );
}

/**
 * The card's second line: what this call did, and what the reader gets if they
 * click.
 *
 * The verb states what the *call* did and never what the file *is*. "新建" is
 * not on offer: the event window has a beginning, and a file written before it
 * cannot be told apart from one that never existed. "覆盖" appears only when
 * this stream actually watched the earlier write.
 *
 * The last clause is the honest part. A workspace route serves the *current*
 * bytes of a name -- there is no way to ask for the version a turn produced,
 * and `tests/architecture/test_a_workspace_version_is_never_asked_for.py` is
 * the reason there never will be. So a card on turn 1 whose file turn 3
 * rewrote says so before the click rather than showing turn 3's bytes under
 * turn 1's heading and letting the reader draw the wrong conclusion
 * (known-gaps F-13).
 */
function metaOf(
  file: ProducedFile,
  entry: WorkspaceEntryView | undefined,
): string {
  const verb =
    file.action === "edit"
      ? "修改"
      : file.action === "run"
        ? "运行时写出"
        : file.overwrote
          ? "覆盖"
          : "写入";
  if (entry === undefined) return `${verb} · 已不在工作区`;
  const parts = [verb, formatSize(entry.size_bytes), mediaLabel(entry.media_type)];
  if (file.supersededByTurn !== null) {
    parts.push(`第 ${String(file.supersededByTurn)} 轮又改过，预览的是最新内容`);
  }
  return parts.join(" · ");
}

/**
 * The last file this turn produced that can be shown in the conversation.
 *
 * "Last" rather than "first": a turn that writes a file and then rewrites it
 * ends on the version it meant, and a turn that writes a script and then a
 * report ends on the thing it was building toward.
 */
function lastPreviewable(
  produced: ProducedFile[],
  files: WorkspaceEntryView[],
): string | null {
  let found: string | null = null;
  for (const file of produced) {
    const entry = files.find((held) => held.name === file.name);
    if (entry === undefined) continue;
    if (entry.size_bytes > AUTO_PREVIEW_MAX_BYTES) continue;
    const kind = previewKind(entry.media_type);
    if (kind === "text" || kind === "html" || kind === "image") {
      found = file.name;
    }
  }
  return found;
}
