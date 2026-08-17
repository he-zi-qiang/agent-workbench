/**
 * One turn of a coding session, as the six things that happened under it.
 *
 * In order, and the order is the argument: the instruction, the thought in
 * flight, what it did, **what it produced**, the report, and -- folded away --
 * what it thought along the way. A reader scrolling back to a report they do
 * not believe finds the evidence directly beneath the sentence that prompted
 * it, rather than in a stage tree keyed by a run id they never saw.
 *
 * ## The reasoning is rendered once, and that is structural
 *
 * `useCodeStream` clears the live thought the moment the `ModelCompleted` for
 * that `model_call_id` arrives (`useCodeStream.ts`), and `buildTurnBlocks`
 * fills `reasonings` **only** from `ModelCompleted` events. A model call is
 * therefore in exactly one of the two sets at any instant, and no timing
 * accident can put it in both. Before this, the same excerpt could be on
 * screen three times at once: streaming at the top of the page, formatted as a
 * `思考过程` body inside its step, and again verbatim inside that step's raw
 * JSON payload dump. Measured on a four-call turn: four steps, each carrying
 * the same text twice, under a live block carrying a fifth copy.
 *
 * The raw events are still reachable -- one disclosure per turn instead of one
 * per event. The claim `StepDisclosure` defends (a curated line is not
 * authority, the reader must be able to check it) is unchanged; only the
 * granularity moved, from every event to every turn.
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
import { summariseGroups, type StepGroup } from "../../components/stepGroups";
import { formatSize } from "../../components/ui";
import { FilePreview } from "./FilePreview";
import type { CodeTurnBlock, ProducedFile } from "./turnBlocks";

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
  onOpen: (name: string) => void;
  openedName: string | null;
  sessionId: string;
}) {
  // Initialised from `live` and never synced back to it. A reader who opened
  // this while the turn ran keeps it open when the answer lands; the previous
  // shape passed `open={running && …}` straight into the element, which is a
  // controlled prop -- so arriving at the finish line slammed shut the very
  // disclosure the reader was reading.
  const [actionsOpen, setActionsOpen] = useState(block.live);

  // At most one auto-opened inline preview per turn, and only while it is
  // live: the last runnable thing this turn produced. N cards each mounting a
  // frame is a real cost, and the moment worth spending it on is the one where
  // a page the agent just wrote starts running in front of you.
  const autoPreview = block.live
    ? lastRunnable(block.produced, files)
    : null;

  return (
    <li className="aw-code-turn">
      <div className="aw-code-said">
        <h3>你</h3>
        <p>{block.instruction}</p>
      </div>

      {/* Above everything the turn has settled, because it is the only thing
          happening *now*. Plain text rather than Markdown, the way chat renders
          its live text: half a fenced block mid-stream renders as garbage. */}
      {block.live && liveThinking !== "" ? (
        <details className="aw-code-thinking" open>
          <summary>正在思考…</summary>
          <p>{liveThinking}</p>
        </details>
      ) : null}

      {block.groups.length === 0 ? null : (
        <details
          className="aw-code-actions"
          onToggle={(event) => {
            setActionsOpen(event.currentTarget.open);
          }}
          open={actionsOpen}
        >
          <summary>
            <span className="aw-code-actions-label">做了什么</span>
            <span className="aw-code-actions-digest">
              {summariseGroups(block.groups)}
            </span>
          </summary>
          <ol className="aw-code-action-list">
            {block.groups.map((group) => (
              <li className={`aw-code-action is-${group.outcome}`} key={group.key}>
                <span className="aw-code-action-title">{group.title}</span>
                {group.subject === null ? null : (
                  <span className="aw-code-action-subject" title={group.subject}>
                    {group.subject}
                  </span>
                )}
                <span className="aw-code-action-outcome">
                  {OUTCOME_LABELS[group.outcome]}
                </span>
              </li>
            ))}
          </ol>
        </details>
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

      {block.reasonings.length === 0 ? null : (
        <details className="aw-code-trace">
          {/* "想过什么" and not "思考过程": the granularity differs -- this is a
              whole turn, that was one model call -- and reusing the word would
              suggest they are the same object. */}
          <summary>想过什么</summary>
          <ol className="aw-code-trace-list">
            {block.reasonings.map((entry) => (
              <li key={entry.callId}>
                {/* One item per model call, never concatenated: a turn is
                    think → call a tool → think again, and joining them reads
                    as one continuous argument that was never made. */}
                <p>{entry.text}</p>
              </li>
            ))}
          </ol>
          {block.events.length === 0 ? null : (
            <details className="aw-code-raw">
              <summary>原始事件（{block.events.length}）</summary>
              <pre>{JSON.stringify(block.events, null, 2)}</pre>
            </details>
          )}
        </details>
      )}
    </li>
  );
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

      {/* Mounted only when open, so a turn with six cards is not six frames
          and six fetches. `HtmlPreview`/`BlobPreview` keep their own size
          ceilings, which is what stops a large page from being transferred to
          be refused. */}
      {entry !== undefined && (kind === "image" || kind === "html") ? (
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
                  loading: false,
                  text: null,
                  truncated: false,
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

/** The last file this turn produced that a frame can actually run or paint. */
function lastRunnable(
  produced: ProducedFile[],
  files: WorkspaceEntryView[],
): string | null {
  let found: string | null = null;
  for (const file of produced) {
    const entry = files.find((held) => held.name === file.name);
    if (entry === undefined) continue;
    const kind = previewKind(entry.media_type);
    if (kind === "html" || kind === "image") found = file.name;
  }
  return found;
}
