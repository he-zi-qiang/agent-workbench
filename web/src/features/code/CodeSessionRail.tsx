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
 * Rename is an explicit row action for pointer, keyboard and touch users. The
 * field id includes the session id so the list can never render duplicate
 * labels.
 */

import { Pencil, Trash2, X } from "lucide-react";
import { useCallback, useRef, useState } from "react";
import type { CodeSessionView } from "../../api/types";
import {
  ErrorNotice,
  formatDateTime,
  IconButton,
  NewSessionAction,
  SidebarSection,
  shortId,
} from "../../components/ui";

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
  onRename: (sessionId: string, title: string) => Promise<void>;
  /** Which row is being renamed, if any. */
  renaming: string | null;
  /** The session on screen, or undefined on the start page. */
  sessionId: string | undefined;
  setRenaming: (sessionId: string | null) => void;
}) {
  const [renamePending, setRenamePending] = useState<string | null>(null);
  const [renameError, setRenameError] = useState<{
    sessionId: string;
    message: string;
  } | null>(null);
  const renameInputRef = useRef<HTMLInputElement | null>(null);
  const renameActionRefs = useRef(new Map<string, HTMLButtonElement>());
  const renameAttempt = useRef(0);

  const focusRenameAction = useCallback((target: string) => {
    window.requestAnimationFrame(() => {
      renameActionRefs.current.get(target)?.focus();
    });
  }, []);

  // Whether the reader is still sitting in the field they submitted. A rename
  // is separated from its outcome by a round trip: on the way out the caret is
  // in a field about to unmount and has to be handed somewhere deliberate, but
  // on the way back the reader may have opened another session or moved to the
  // composer, and pulling them back to a row they left is worse than leaving
  // focus alone. `onBlur` cannot answer this -- it declines to cancel while a
  // rename is pending, exactly so the request keeps its row.
  const renameFieldHasFocus = () =>
    renameInputRef.current !== null &&
    renameInputRef.current === document.activeElement;

  const beginRename = (target: string) => {
    renameAttempt.current += 1;
    setRenamePending(null);
    setRenameError(null);
    setRenaming(target);
  };

  const cancelRename = (target: string, restoreFocus = true) => {
    renameAttempt.current += 1;
    setRenamePending(null);
    setRenameError(null);
    setRenaming(null);
    if (restoreFocus) focusRenameAction(target);
  };

  const commitRename = async (held: CodeSessionView, title: string) => {
    const trimmed = title.trim();
    if (trimmed === "" || trimmed === (held.title ?? "").trim()) {
      cancelRename(held.session_id);
      return;
    }

    const attempt = renameAttempt.current + 1;
    renameAttempt.current = attempt;
    setRenamePending(held.session_id);
    setRenameError(null);
    try {
      await onRename(held.session_id, trimmed);
    } catch (cause: unknown) {
      if (renameAttempt.current !== attempt) return;
      const keepFocus = renameFieldHasFocus();
      setRenamePending(null);
      setRenameError({
        sessionId: held.session_id,
        message: cause instanceof Error ? cause.message : String(cause),
      });
      if (keepFocus) {
        window.requestAnimationFrame(() => {
          renameInputRef.current?.focus();
        });
      }
      return;
    }

    if (renameAttempt.current !== attempt) return;
    const keepFocus = renameFieldHasFocus();
    setRenamePending(null);
    setRenaming(null);
    if (keepFocus) focusRenameAction(held.session_id);
  };

  return (
    <nav
      aria-label="最近的编码会话"
      className={`aw-code-sessions ${mobileOpen ? "is-mobile-open" : ""}`}
    >
      <SidebarSection
        actions={
          <>
            {/* Goes to the start page rather than POSTing an empty session:
                the first sentence is what names a session (ADR-047), and one
                created by a bare click sits unnamed in this list forever. */}
            <NewSessionAction label="新建会话" onClick={onNew} />
            <IconButton
              className="aw-code-sessions-close"
              label="关闭会话列表"
              onClick={onCloseMobile}
            >
              <X aria-hidden size={17} />
            </IconButton>
          </>
        }
        storageKey="aw.side.code.v1"
        title="最近编码"
      >
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
                    <>
                      <form
                        aria-busy={renamePending === held.session_id}
                        className="aw-session-inline-rename"
                        onSubmit={(event) => {
                          event.preventDefault();
                          if (renamePending === held.session_id) return;
                          const field = new FormData(event.currentTarget).get(
                            "title",
                          );
                          // A FormData entry is a string *or a File*, and
                          // `String(File)` is "[object File]" -- a name nobody typed.
                          void commitRename(
                            held,
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
                          aria-describedby={
                            renameError?.sessionId === held.session_id
                              ? `aw-code-rename-error-${held.session_id}`
                              : undefined
                          }
                          aria-invalid={
                            renameError?.sessionId === held.session_id ||
                            undefined
                          }
                          autoFocus
                          defaultValue={held.title ?? ""}
                          id={`aw-code-rename-${held.session_id}`}
                          name="title"
                          onBlur={() => {
                            if (renamePending !== held.session_id) {
                              cancelRename(held.session_id, false);
                            }
                          }}
                          onChange={() => {
                            if (renameError?.sessionId === held.session_id) {
                              setRenameError(null);
                            }
                          }}
                          onFocus={(event) => event.currentTarget.select()}
                          onKeyDown={(event) => {
                            if (event.key !== "Escape") return;
                            event.preventDefault();
                            event.stopPropagation();
                            cancelRename(held.session_id);
                          }}
                          readOnly={renamePending === held.session_id}
                          ref={renameInputRef}
                        />
                      </form>
                      {renameError?.sessionId === held.session_id ? (
                        <div
                          className="aw-session-rename-error"
                          id={`aw-code-rename-error-${held.session_id}`}
                        >
                          <ErrorNotice message={renameError.message} />
                        </div>
                      ) : null}
                    </>
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
                        // Named after the first instruction, so most rows have
                        // one. The id is the fallback for a session opened and
                        // never used. Rename is a separate action so opening a
                        // different session cannot be the first half of editing it.
                        title={held.title ?? held.session_id}
                        type="button"
                      >
                        <span className="aw-code-recent-title">
                          {held.title ?? shortId(held.session_id)}
                        </span>
                        {/* 副行只说时间。
                          稿子上这里是「3 轮 · 9 个文件 · 14:02」，前两段这个
                          接口给不出来——`CodeSessionView` 只有 session_id、
                          title、last_activity_at 三个字段。编一个轮数比空着
                          更糟：它会被当成真的读。
                          没有 last_activity_at 的会话（开了没用过）不留占位，
                          一行空的副行只是让每张卡都变高。 */}
                        {held.last_activity_at === null ? null : (
                          <time
                            className="aw-code-recent-when"
                            dateTime={held.last_activity_at}
                          >
                            {formatDateTime(held.last_activity_at)}
                          </time>
                        )}
                      </button>
                      {/* Always rendered, not revealed on hover: a control that
                        only exists under a pointer is one a keyboard cannot
                        reach and a touch screen never shows. CSS dims it until
                        the row is hovered or the button focused. */}
                      <span className="aw-session-row-actions aw-code-recent-actions">
                        <button
                          aria-label={`重命名会话 ${held.title ?? held.session_id}`}
                          className="aw-code-recent-rename"
                          onClick={() => {
                            beginRename(held.session_id);
                          }}
                          ref={(node) => {
                            if (node === null) {
                              renameActionRefs.current.delete(held.session_id);
                            } else {
                              renameActionRefs.current.set(
                                held.session_id,
                                node,
                              );
                            }
                          }}
                          title="重命名"
                          type="button"
                        >
                          <Pencil aria-hidden size={12} />
                        </button>
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
                      </span>
                    </div>
                  )}
                </li>
              ))}
            </ul>
          )}
        </div>
      </SidebarSection>
    </nav>
  );
}
