/**
 * The way back to a coding session, as a column that is always there.
 *
 * It used to be a `<details>` folded into the top of the transcript, opened by
 * a 12px "会话" summary. That shape was chosen on the argument that switching
 * sessions is a rare act and a permanent list steals height from the
 * conversation -- true of height, wrong about the act: a coding session is the
 * unit of work here, and a person arriving at this page is at least as likely
 * to be resuming one as to be starting one. Folded away, the list also had to
 * exist twice, unfolded on the start page and folded inside a session, which
 * is two renderings of one thing that could disagree.
 *
 * Deliberately *not* `.aw-local-badge` and not chat's storage note: chat's
 * sessions live in this browser, these live on the server (ADR-047), and
 * `docs/frontend-design.md` names that contrast specifically. Borrowing the
 * badge would be a lie about where the work is.
 *
 * Rename-by-double-click and delete-behind-confirm are carried over unchanged.
 * The one edit is the rename field's `id`, which was the literal
 * `aw-code-rename`: harmless while the list was folded away and mounted once,
 * a duplicate id the moment two of them can be on screen.
 */

import { Plus, Trash2, X } from "lucide-react";
import type { CodeSessionView } from "../../api/types";
import { IconButton, shortId } from "../../components/ui";

export function CodeSessionRail({
  known,
  mobileOpen,
  onCloseMobile,
  onDelete,
  onNew,
  onOpen,
  onRename,
  renaming,
  sessionId,
  setRenaming,
}: {
  known: CodeSessionView[];
  mobileOpen: boolean;
  onCloseMobile: () => void;
  onDelete: (sessionId: string) => void;
  onNew: () => void;
  onOpen: (sessionId: string) => void;
  onRename: (sessionId: string, title: string) => void;
  /** Which row is being renamed, if any. */
  renaming: string | null;
  /** The session on screen, or undefined on the start page. */
  sessionId: string | undefined;
  setRenaming: (sessionId: string | null) => void;
}) {
  return (
    <nav
      aria-label="最近的编码会话"
      className={`aw-code-sessions ${mobileOpen ? "is-mobile-open" : ""}`}
    >
      <header className="aw-code-sessions-header">
        <strong>会话</strong>
        <div className="aw-code-sessions-actions">
          {/* Goes to the start page rather than POSTing an empty session: the
              first sentence is what names a session (ADR-047), and one created
              by a bare click sits unnamed in this list forever. */}
          <IconButton label="新建会话" onClick={onNew}>
            <Plus aria-hidden size={17} />
          </IconButton>
          <IconButton
            className="aw-code-sessions-close"
            label="关闭会话列表"
            onClick={onCloseMobile}
          >
            <X aria-hidden size={17} />
          </IconButton>
        </div>
      </header>

      <div className="aw-code-session-list">
        {known.length === 0 ? (
          <p className="aw-code-sessions-empty">
            还没有会话。说一句要做的事就开一个。
          </p>
        ) : (
          <ul>
            {known.map((held) => (
              <li key={held.session_id}>
                {renaming === held.session_id ? (
                  <form
                    onSubmit={(event) => {
                      event.preventDefault();
                      const field = new FormData(event.currentTarget).get("title");
                      // A FormData entry is a string *or a File*, and
                      // `String(File)` is "[object File]" -- a name nobody typed.
                      onRename(
                        held.session_id,
                        typeof field === "string" ? field : "",
                      );
                    }}
                  >
                    <label
                      className="aw-sr-only"
                      htmlFor={`aw-code-rename-${held.session_id}`}
                    >
                      会话名字
                    </label>
                    <input
                      autoFocus
                      defaultValue={held.title ?? ""}
                      id={`aw-code-rename-${held.session_id}`}
                      name="title"
                      onBlur={() => {
                        setRenaming(null);
                      }}
                    />
                  </form>
                ) : (
                  <div className="aw-code-recent-row">
                    <button
                      aria-current={
                        held.session_id === sessionId ? "page" : undefined
                      }
                      className="aw-code-recent-link"
                      onClick={() => {
                        onOpen(held.session_id);
                      }}
                      onDoubleClick={() => {
                        setRenaming(held.session_id);
                      }}
                      // Named after the first instruction, so most rows have
                      // one. The id is the fallback for a session opened and
                      // never used.
                      title={held.title ?? held.session_id}
                      type="button"
                    >
                      {held.title ?? shortId(held.session_id)}
                    </button>
                    {/* Always rendered, not revealed on hover: a control that
                        only exists under a pointer is one a keyboard cannot
                        reach and a touch screen never shows. CSS dims it until
                        the row is hovered or the button focused. */}
                    <button
                      aria-label={`删除会话 ${held.title ?? held.session_id}`}
                      className="aw-code-recent-delete"
                      onClick={() => {
                        onDelete(held.session_id);
                      }}
                      title="删除"
                      type="button"
                    >
                      <Trash2 aria-hidden size={13} />
                    </button>
                  </div>
                )}
              </li>
            ))}
          </ul>
        )}
      </div>
    </nav>
  );
}
