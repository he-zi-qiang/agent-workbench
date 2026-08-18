/**
 * The right column: one thing, the thing you clicked.
 *
 * It used to be "工作区" -- a permanent file browser that appeared as soon as
 * the workspace had any file at all. Two costs, and the second is the one that
 * made this a redesign rather than a tidy-up:
 *
 * * It was mounted on `files.length > 0`, so from the first turn onward it
 *   took `clamp(320px, 40%, 560px)` unconditionally. On a 1280px window the
 *   conversation was left with 768px whether or not anybody had asked to look
 *   at anything. Mounting on "you clicked something" instead is the single
 *   largest width gain in this change.
 * * A list of every file in the workspace answers "what is in here", which is
 *   a question about the session. The question a reader has while reading a
 *   turn is "what did *that* make" -- and that one is now answered by the
 *   cards inside the turn, where the turn is.
 *
 * The full listing does not disappear: it folds up at the bottom, and its
 * heading says the full count out loud. That is not a decoration. Four kinds
 * of file have no card and this is their only route -- files uploaded rather
 * than produced, files from turns older than the stream's `KEPT_EVENTS`
 * window, files written before ADR-063 whose name was truncated out of the
 * argument preview, and files from runs dropped by the tail-anchored pairing.
 * Calling the fold "其他文件" would imply the cards covered the rest.
 */

import type { PrincipalIdentity, WorkspaceEntryView } from "../../api/types";
import { formatSize } from "../../components/ui";
import { FilePreview, type OpenedFile } from "./FilePreview";

export function PreviewPanel({
  directoryOpen,
  files,
  identity,
  onClose,
  onDownload,
  onOpen,
  onWrote,
  orphanRuns,
  setDirectoryOpen,
  viewing,
}: {
  directoryOpen: boolean;
  files: WorkspaceEntryView[];
  identity: PrincipalIdentity;
  onClose: () => void;
  onDownload: () => void;
  onOpen: (file: WorkspaceEntryView) => void;
  /** A run in here can write files; the page owns the listing they land in. */
  onWrote: (names: string[]) => void;
  /** Runs the pairing could not attribute; surfaced rather than swallowed. */
  orphanRuns: number;
  setDirectoryOpen: (open: boolean) => void;
  viewing: OpenedFile | null;
}) {
  return (
    <aside aria-label="预览" className="aw-code-preview">
      <header className="aw-code-preview-header">
        <h2>{viewing?.name ?? "工作区"}</h2>
        <div className="aw-code-preview-actions">
          {viewing === null ? null : (
            <button className="aw-button" onClick={onDownload} type="button">
              下载
            </button>
          )}
          <button className="aw-button" onClick={onClose} type="button">
            关闭
          </button>
        </div>
      </header>

      {viewing === null ? null : (
        <section
          aria-label={`文件 ${viewing.name}`}
          className="aw-code-file-view"
        >
          <FilePreview identity={identity} onWrote={onWrote} viewing={viewing} />
        </section>
      )}

      <details
        className="aw-code-directory"
        onToggle={(event) => {
          setDirectoryOpen(event.currentTarget.open);
        }}
        open={directoryOpen}
      >
        <summary>
          工作区全部文件（{files.length}）
          {/* Said out loud rather than left as a silent gap. Non-zero means
              another tab ran turns in this session, so the stream holds runs
              this transcript has no instruction for and their cards were
              dropped rather than guessed onto the wrong turn. */}
          {orphanRuns === 0 ? null : (
            <span className="aw-code-value">
              （有 {orphanRuns} 轮的产出没能归位）
            </span>
          )}
        </summary>
        {files.length === 0 ? (
          <p className="aw-code-workspace-empty">还没有文件。</p>
        ) : (
          <ul>
            {files.map((file) => (
              <li key={file.name}>
                <button
                  aria-current={file.name === viewing?.name ? "true" : undefined}
                  className="aw-code-file-open"
                  onClick={() => {
                    onOpen(file);
                  }}
                  type="button"
                >
                  <span className="aw-code-file-name">{file.name}</span>
                  <span className="aw-code-value">
                    {formatSize(file.size_bytes)}
                  </span>
                </button>
              </li>
            ))}
          </ul>
        )}
      </details>
    </aside>
  );
}
