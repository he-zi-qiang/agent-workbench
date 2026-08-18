/**
 * One produced file, as a card: its name, what the call did to it, and -- when
 * showing it is cheap -- the file itself.
 *
 * Lifted out of `CodeTurn` when it gained a second caller. A turn's card and a
 * run's card are the same object in the reader's head ("this thing was just
 * written"), and two implementations of it would drift on the questions this
 * component exists to answer: which kinds open inline, what the second line
 * says, whether an unknown type is silent or speaks. The drift would show as
 * the same file behaving differently depending on which half of the session
 * produced it -- which is the very asymmetry ADR-066 is about.
 *
 * **The preview body arrives as a render prop, and that is structural.** A
 * card can show a file, and the thing that shows a file (`FilePreview`) now
 * shows cards for what a run wrote. Importing each other would be a module
 * cycle; handing the body in as a function leaves this file importing nothing
 * from the viewer, exactly the way `PythonPreview` already takes its `source`.
 * The recursion that remains is bounded and intentional: a run's card can
 * inline a text/image/html preview, and none of those produce cards.
 */

import { FileCode2, FileText, Image as ImageIcon } from "lucide-react";
import type { ReactNode } from "react";
import { useState } from "react";
import type { WorkspaceEntryView } from "../../api/types";
import {
  type CheckCost,
  type SurfaceAbilities,
  checkCost,
  mediaLabel,
  previewKind,
} from "../../components/media";
import { formatSize } from "../../components/ui";
import type { ProducedFile } from "./turnBlocks";

/** What a coding session can do to a file it holds: run it, but not convert it. */
export const CODE_ABILITIES = { canRun: true, canConvert: false } as const;

export function FileCard({
  abilities = CODE_ABILITIES,
  autoPreview,
  entry,
  file,
  onOpen,
  opened,
  renderPreview,
}: {
  /**
   * What the surface holding this card can do, defaulting to a full coding
   * session. Overridden in exactly one place: the cards under a run's output,
   * which pass `canRun: false` so that a `.py` a script just wrote does not
   * grow its own 运行 button. That is the loop gate -- one click has to mean
   * one container, and a card that could start another inside the output of
   * the last one makes "how many did I start" unanswerable.
   */
  abilities?: SurfaceAbilities;
  autoPreview: boolean;
  /** Undefined when the name is no longer in the workspace listing. */
  entry: WorkspaceEntryView | undefined;
  file: ProducedFile;
  onOpen: (name: string) => void;
  opened: boolean;
  /** The viewer for this file, built by the caller and mounted only when the
      fold is open -- a turn with six cards must not be six fetches. */
  renderPreview: () => ReactNode;
}) {
  const [inlineOpen, setInlineOpen] = useState(autoPreview);
  // Followed rather than only seeded. `useState(autoPreview)` reads its
  // argument once, so a card mounted before the turn's last previewable file
  // existed kept whatever answer was true then: a file that *became* the
  // auto-preview choice after its card had mounted never opened -- which on a
  // streaming console is the ordinary case, because the card for a file
  // appears the moment its `ToolCompleted` lands and the turn keeps going.
  //
  // Adjusted during render against a remembered previous value rather than in
  // an effect. The effect version is what this was first written as, and the
  // lint rule that rejected it is right: it renders once with the stale fold,
  // then again with the right one, and on a card holding an iframe that is a
  // visible flash. This is React's documented "adjusting state when a prop
  // changes" shape and settles before anything paints.
  //
  // One-directional on purpose: it opens a fold, never closes one. `autoPreview`
  // turning false means some *other* file became the turn's last previewable
  // one, which is no reason to shut a preview the reader may be mid-sentence in.
  const [autoPreviewWas, setAutoPreviewWas] = useState(autoPreview);
  if (autoPreview !== autoPreviewWas) {
    setAutoPreviewWas(autoPreview);
    if (autoPreview) setInlineOpen(true);
  }

  const kind = entry === undefined ? "none" : previewKind(entry.media_type);
  const cost: CheckCost =
    entry === undefined
      ? "unchecked"
      : checkCost(entry.media_type, file.name, abilities);
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
        <span className="aw-code-output-meta">{metaOf(file, entry, cost)}</span>
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
            <div className="aw-code-output-inline-body">{renderPreview()}</div>
          ) : null}
        </details>
      ) : null}

      {/* Said, rather than left as an absence, and said differently for the
          two situations that used to share one silence. A card for a .xlsx
          used to end at its name -- no fold, no sentence, nothing telling
          "this console has no viewer" apart from "this card is still loading"
          -- and the silence read as the feature being broken, the same
          complaint a `.py` drew before ADR-065 gave it a 运行.

          `elsewhere` is the other one, and conflating it with `unchecked` is
          the more expensive mistake: it tells a reader that something *is*
          viewable, just not from this card, and sending them away with that
          knowledge is worth a sentence of its own. */}
      {entry === undefined ? null : cost === "unchecked" ? (
        <p className="aw-code-output-note">
          这个控制台没有能显示它的查看器，下载后用别的程序打开。
        </p>
      ) : cost === "elsewhere" ? (
        <p className="aw-code-output-note">{elsewhereNote(entry.media_type)}</p>
      ) : null}
    </li>
  );
}

/**
 * Where a file this card cannot check *can* be checked.
 *
 * Two cases reach here and they point in opposite directions, which is why the
 * sentence is not one generic "看不了". A .docx has a layout viewer in this
 * console, just not over a working set (the conversion endpoints address an
 * artifact id and the workspace listing deliberately hands out none --
 * known-gaps F-11). A .py has a runner in this console, just not inside the
 * output of another run.
 */
function elsewhereNote(mediaType: string): string {
  if (previewKind(mediaType) === "docx") {
    return "Word 的版面预览目前只在任务产出里有；这里可以下载后打开。";
  }
  return "这个文件也能跑，在工作区里打开它就有运行按钮。";
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
 *
 * The cost clause is the ADR-066 half, and it is only ever added for
 * `one-action`. `free` and `reader` files are already on screen by the time
 * anyone reads this line, so annotating them would be the card describing what
 * the reader is looking at; `unchecked` gets a full sentence below instead.
 */
function metaOf(
  file: ProducedFile,
  entry: WorkspaceEntryView | undefined,
  cost: CheckCost,
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
  if (cost === "one-action") parts.push("点开可以运行");
  if (file.supersededByTurn !== null) {
    parts.push(`第 ${String(file.supersededByTurn)} 轮又改过，预览的是最新内容`);
  }
  return parts.join(" · ");
}
