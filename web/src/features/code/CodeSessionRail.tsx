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

import { Trash2, X } from "lucide-react";
import type { CodeSessionView } from "../../api/types";
import {
  formatDateTime,
  IconButton,
  NewSessionButton,
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
        {/* 三件事，每件都在别处问过一次。
            「服务端列表」是与 Chat 那一列的对照：那边是这台浏览器记下来的，
            这边不是，换台机器还在。
            「名字来自第一句指令」解释了为什么这里没有"重命名"按钮也没有输入框
            ——名字是 ADR-047 从第一句话取的，不是谁填的。
            「双击改名」是这一行真正的理由：改名功能一直在（onDoubleClick 就在
            下面），但界面上没有任何东西说过它存在。一个没人找得到的功能等于
            没有。 */}
        <small>服务端列表 · 名字来自第一句指令 · 双击改名</small>
        <div className="aw-code-sessions-actions">
          <IconButton
            className="aw-code-sessions-close"
            label="关闭会话列表"
            onClick={onCloseMobile}
          >
            <X aria-hidden size={17} />
          </IconButton>
        </div>
      </header>
      {/* Goes to the start page rather than POSTing an empty session: the
          first sentence is what names a session (ADR-047), and one created
          by a bare click sits unnamed in this list forever. */}
      <NewSessionButton label="新建会话" onClick={onNew} />

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
                      // Esc gets out without saving. Clicking away already
                      // did (onBlur below), but that is the mouse's exit and
                      // this field is opened by a gesture -- a double-click --
                      // that is easy to trigger by accident and impossible to
                      // trigger from a keyboard. Leaving the only way out on
                      // the pointer strands whoever arrived here without one.
                      onKeyDown={(event) => {
                        if (event.key !== "Escape") return;
                        event.preventDefault();
                        setRenaming(null);
                      }}
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
                      // never used. The rename hint rides along: the header
                      // says it once for the column, this says it on the row
                      // the pointer is actually over.
                      title={`${held.title ?? held.session_id}\n双击改名`}
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
