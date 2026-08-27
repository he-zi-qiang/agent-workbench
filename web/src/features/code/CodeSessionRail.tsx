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
import { WorkspaceSidebarActions } from "../../app/WorkspaceSidebar";
import type { CodeSessionView } from "../../api/types";
import {
  ErrorNotice,
  formatDateTime,
  IconButton,
  NewSessionAction,
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
  onToggleScope,
  outsideCount,
  projectNames,
  renaming,
  runningIds,
  scoped,
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
  /** 在「只看这个文件夹」和「全部」之间切换。 */
  onToggleScope: () => void;
  /** 有多少段会话不在当前这个文件夹里。0 表示没有可切换的东西。 */
  outsideCount: number;
  /**
   * 此刻有回合开着的会话。
   *
   * 这个标签页知道的那些——`CodePage` 的 `runningIn` 是它自己发出去的请求的
   * 集合，不是服务端的账。另一个标签页里跑着的回合在这里是看不见的，而这一点
   * 和副行不肯编一个轮数出来是同一条规矩：这一栏只说它真的知道的事。
   */
  runningIds: ReadonlySet<string | null>;
  /**
   * `project_id` → 文件夹名，给「全部会话」那一档用。
   *
   * 传一张表进来而不是在这里取：`["projects", identity]` 已经被
   * `ProjectChooser` 和 `ProjectPicker` 取着，第三个取用方只会让同一份答案在
   * 三处各有各的加载态。空表就是「还没取到」，那一档下不画标记——**少标一个
   * 是漏说，标错一个是撒谎**，和树上那个点是同一条规矩。
   */
  projectNames: ReadonlyMap<string, string>;
  /** Which row is being renamed, if any. */
  renaming: string | null;
  /** 这份列表此刻是不是只列了当前文件夹里的会话。 */
  scoped: boolean;
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
      <IconButton
        className="aw-code-sessions-close"
        label="关闭会话列表"
        onClick={onCloseMobile}
      >
        <X aria-hidden size={17} />
      </IconButton>

      <WorkspaceSidebarActions>
        {/* Goes to the start page rather than POSTing an empty session:
            the first sentence is what names a session (ADR-047), and one
            created by a bare click sits unnamed in this list forever. */}
        <NewSessionAction label="新建会话" onClick={onNew} />
      </WorkspaceSidebarActions>
      {/* 这一栏里有两块东西，此前只有一块有名字。
          上面是 `ProjectFileTree` 的「项目目录」加一条绝对路径，下面紧接着就
          是一列标题——读者看到的是「文件、文件、文件、然后几行不知道是什么的
          字」，而那几行字恰好也长得像文件名。给它一个抬头，两块东西才各自成
          立；`aw-eyebrow` 是上面那块用的同一个类，因为它们是并列的两节，不是
          一节和它的附注。 */}
      <span className="aw-eyebrow aw-code-sessions-eyebrow">
        {scoped ? "这个文件夹里的会话" : "全部会话"}
      </span>
      <div className="aw-code-session-list">
        {known.length === 0 ? (
          // 收窄之后的空列表不是「一段会话都没有」。说错了这句，读者会以为
          // 自己上周做的那些不见了——它们在别的文件夹里，而下面那行字正是
          // 去那里的路。
          <p className="aw-code-sessions-empty">
            {scoped
              ? "这个文件夹里还没有会话。说一句要做的事就开一个。"
              : "还没有会话。说一句要做的事就开一个。"}
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
                        {/* 有回合开着的那一行自己说出来。副行只有时间，而
                            「上次活动 14:02」和「此刻正在跑」在一列里长得一
                            模一样——读者要靠自己记得刚才在哪一行按了发送。
                            点在标题前面而不是行尾：行尾归重命名和删除，把一个
                            只读的状态放进一排可点的东西里，是在邀请人点它。 */}
                        {runningIds.has(held.session_id) ? (
                          <span
                            aria-hidden="true"
                            className="aw-code-recent-running"
                          />
                        ) : null}
                        {held.title ?? shortId(held.session_id)}
                        {runningIds.has(held.session_id) ? (
                          <span className="aw-sr-only">（正在运行）</span>
                        ) : null}
                      </span>
                      {/* 副行说时间，以及——只在「全部会话」那一档——它属于哪个
                          文件夹。
                          稿子上这里是「3 轮 · 9 个文件 · 14:02」，轮数和文件数
                          这个接口仍然给不出来，而且**是被明确拒绝的**：
                          `SessionView` 的 docstring 与 ADR-047 §4 说，任何一个
                          都会把列表行从一次查询变成每行一次查询，正确的未来机
                          制是投影表。编一个数比空着更糟：它会被当成真的读。
                          文件夹名不属于那一类。`project_id` 本来就在每一行上，
                          名字来自一份**已经被取着**的项目列表，所以它是一次
                          join，不是第 N 次查询。
                          收窄那一档不画它：整份列表都在同一个文件夹里的时候，
                          在每一行上重复那个名字，是把一个不区分任何东西的字段
                          印 N 遍。
                          没有 last_activity_at 的会话（开了没用过）不留占位，
                          一行空的副行只是让每张卡都变高。 */}
                      {scoped || held.project_id == null ? null : (
                        <span className="aw-code-recent-project">
                          {projectNames.get(held.project_id) ?? "别的文件夹"}
                        </span>
                      )}
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
                            renameActionRefs.current.set(held.session_id, node);
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

      {/* 被这次收窄挡在外面的会话，数出来。
          一个不说数量的「显示全部」是在让读者猜自己有没有漏东西；数量为 0 时
          整行不出现——那时候「全部」和「这个文件夹」是同一份列表，多一个切不出
          区别的开关只是噪音。

          长在滚动区**外面**，钉在这一栏的底。放在列表末尾试过，量出来的问题是
          它只在收窄状态下够得着：一按「全部显示」，列表从 2 条变成 50 条，而那
          行「只看这个文件夹」跟着沉到了 50 条底下——把人送进一个只能靠滚动才
          退得出来的状态，比不给这个开关更糟。 */}
      {outsideCount === 0 ? null : (
        <button
          aria-pressed={scoped}
          className="aw-code-sessions-scope"
          onClick={onToggleScope}
          type="button"
        >
          {scoped
            ? `另外 ${String(outsideCount)} 段在别的文件夹 · 全部显示`
            : "只看这个文件夹"}
        </button>
      )}
    </nav>
  );
}
