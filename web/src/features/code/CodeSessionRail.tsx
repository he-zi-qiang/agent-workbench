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

import { Pencil, Search, Trash2, X } from "lucide-react";
import { useCallback, useMemo, useRef, useState } from "react";
import { WorkspaceSidebarActions } from "../../app/WorkspaceSidebar";
import type { CodeSessionView } from "../../api/types";
import {
  ErrorNotice,
  formatDateTime,
  IconButton,
  NewSessionAction,
  shortId,
  SidebarAction,
} from "../../components/ui";
import { groupSessions, VISIBLE_SESSIONS } from "./sessionGroups";

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
  //: 搜索框开着没有，以及框里是什么。
  //:
  //: 藏在一颗图标后面而不是常驻：这一栏一共 260px 宽，一个永远占着一行的搜索框，
  //: 在只有四段会话的时候是纯粹的家具。它长在工作区那一行上（和「新建」并排），
  //: 因为那一行**就是**这一组的标题——过滤的是它底下这份列表。
  const [searching, setSearching] = useState(false);
  const [query, setQuery] = useState("");
  //: 读者按过「全部显示」。按过就不再折。
  const [expanded, setExpanded] = useState(false);
  const searchInputRef = useRef<HTMLInputElement | null>(null);
  const searchActionRef = useRef<HTMLButtonElement | null>(null);

  const view = useMemo(
    () =>
      groupSessions(known, {
        expanded,
        // 收窄到一个文件夹时不分组：那时候只有一组，而它的名字已经写在上面
        // 那行「这个文件夹里的会话」里了。
        grouped: !scoped,
        projectNames,
        query,
        sessionId,
      }),
    [expanded, known, projectNames, query, scoped, sessionId],
  );

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

  const row = (held: CodeSessionView) =>
    renaming === held.session_id ? (
      <>
        <form
          aria-busy={renamePending === held.session_id}
          className="aw-session-inline-rename"
          onSubmit={(event) => {
            event.preventDefault();
            if (renamePending === held.session_id) return;
            const field = new FormData(event.currentTarget).get("title");
            // A FormData entry is a string *or a File*, and
            // `String(File)` is "[object File]" -- a name nobody typed.
            void commitRename(held, typeof field === "string" ? field : "");
          }}
        >
          <label className="aw-sr-only" htmlFor={`aw-code-rename-${held.session_id}`}>
            会话名字
          </label>
          <input
            aria-describedby={
              renameError?.sessionId === held.session_id
                ? `aw-code-rename-error-${held.session_id}`
                : undefined
            }
            aria-invalid={renameError?.sessionId === held.session_id || undefined}
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
              if (renameError?.sessionId === held.session_id) setRenameError(null);
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
          aria-current={held.session_id === sessionId ? "page" : undefined}
          className="aw-code-recent-link"
          onClick={() => {
            onOpen(held.session_id);
          }}
          // Named after the first instruction, so most rows have one. The id is
          // the fallback for a session opened and never used. Rename is a
          // separate action so opening a different session cannot be the first
          // half of editing it.
          title={held.title ?? held.session_id}
          type="button"
        >
          <span className="aw-code-recent-title">
            {/* 这个位子**永远**留着，点只在真的跑着的时候画。
                此前它是条件渲染的，于是一段会话开跑的瞬间，它那一行的标题会
                往右挪 12px——一列本来对齐的名字里有一行不对齐，而那一下抖动
                恰好发生在读者正盯着它看的时候。
                空着的时候不画一个空心圈：那会是一句「它没在跑」，而这一栏
                只知道**这个标签页**发出去的请求（见 `runningIds`），另一个
                标签页里跑着的回合在这里是看不见的。留位子不撒谎，画空心圈会。 */}
            <span
              aria-hidden="true"
              className={`aw-code-recent-running ${
                runningIds.has(held.session_id) ? "is-running" : ""
              }`}
            />
            {held.title ?? shortId(held.session_id)}
            {runningIds.has(held.session_id) ? (
              <span className="aw-sr-only">（正在运行）</span>
            ) : null}
          </span>
          {/* 副行只剩时间。
              文件夹名搬去了组的抬头（`sessionGroups.ts`）：一份 40 条的列表里，
              同一个名字此前会在每一行上各印一遍，而它要区分的东西只有 3 个。
              轮数和文件数这个接口仍然给不出来，而且**是被明确拒绝的**：
              `SessionView` 的 docstring 与 ADR-047 §4 说，任何一个都会把列表行
              从一次查询变成每行一次查询。编一个数比空着更糟——它会被当成真的读。
              没有 last_activity_at 的会话（开了没用过）不留占位，一行空的副行
              只是让每张卡都变高。 */}
          {held.last_activity_at === null ? null : (
            <time className="aw-code-recent-when" dateTime={held.last_activity_at}>
              {formatDateTime(held.last_activity_at)}
            </time>
          )}
        </button>
        {/* Always rendered, not revealed on hover: a control that only exists
            under a pointer is one a keyboard cannot reach and a touch screen
            never shows. CSS dims it until the row is hovered or the button
            focused. */}
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
    );

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
        {/* 两颗，和截图里那一行一样：一颗开新的，一颗找旧的。它们长在**导航项
            自己那一行**上，因为那一行就是这一组的标题——过滤的是它底下这份列表。 */}
        <SidebarAction
          active={searching}
          buttonRef={searchActionRef}
          label={searching ? "关掉搜索" : "在会话里搜索"}
          onClick={() => {
            const next = !searching;
            setSearching(next);
            // 关掉时把搜索词一起清掉。留着的话，下次打开这一栏看到的是一份被
            // 上一次的搜索词过滤过的列表，而那个词已经不在屏幕上了。
            if (!next) setQuery("");
          }}
        >
          <Search aria-hidden="true" size={15} />
        </SidebarAction>
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
      {searching ? (
        <div className="aw-code-sessions-search">
          <Search aria-hidden="true" size={13} />
          <label className="aw-sr-only" htmlFor="aw-code-session-search">
            在会话里搜索
          </label>
          <input
            // `autoFocus` 而不是在按钮的 onClick 里 rAF 一次再 `focus()`。
            // 后者写过，实测焦点留在了那颗按钮上：这个输入框是 portal 进侧栏的，
            // 而按钮在另一棵 portal 里，两次提交之间焦点回到了刚被点的那颗按钮。
            // `autoFocus` 由 React 在挂载时做，不用去猜哪一帧。
            autoFocus
            id="aw-code-session-search"
            onChange={(event) => setQuery(event.target.value)}
            onKeyDown={(event) => {
              if (event.key !== "Escape") return;
              // 就地拦下，不往上冒：这个抽屉在窄屏上是模态的，而 `AppShell` 挂在
              // window 上的那个 Escape 会把整栏一起关掉——读者只是想清掉搜索词。
              event.preventDefault();
              event.stopPropagation();
              if (query !== "") {
                setQuery("");
                return;
              }
              setSearching(false);
              searchActionRef.current?.focus();
            }}
            placeholder="名字、文件夹"
            ref={searchInputRef}
            type="search"
            value={query}
          />
        </div>
      ) : null}
      <div className="aw-code-session-list">
        {view.groups.length === 0 ? (
          // 三种空，三句话。搜不到东西时说「一段都没有」，读者会以为它们被删了。
          <p className="aw-code-sessions-empty">
            {query.trim() !== ""
              ? `没有匹配「${query.trim()}」的会话。`
              : scoped
                ? "这个文件夹里还没有会话。说一句要做的事就开一个。"
                : "还没有会话。说一句要做的事就开一个。"}
          </p>
        ) : (
          view.groups.map((group) => (
            <div className="aw-code-session-group" key={group.key}>
              {group.label === null ? null : (
                <h3 className="aw-code-session-group-name">{group.label}</h3>
              )}
              <ul>
                {group.sessions.map((held) => (
                  <li key={held.session_id}>{row(held)}</li>
                ))}
              </ul>
            </div>
          ))
        )}
        {/* 折起来的那些，数出来。
            长在滚动区**里面**、列表末尾，和下面那颗「只看这个文件夹」不同：
            它展开的东西就接在它下面，所以它该在那个位置上；而那一颗切换的是
            整份列表的范围，一按下去列表会从 2 条变成 50 条，跟着沉到底下就
            够不着了——所以那一颗钉在栏底。 */}
        {view.hidden === 0 ? null : (
          <button
            className="aw-code-sessions-more"
            onClick={() => setExpanded(true)}
            type="button"
          >
            还有 {view.hidden} 段 · 全部显示
          </button>
        )}
        {expanded && view.matched > VISIBLE_SESSIONS ? (
          <button
            className="aw-code-sessions-more"
            onClick={() => setExpanded(false)}
            type="button"
          >
            收起，只看最近 {VISIBLE_SESSIONS} 段
          </button>
        ) : null}
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
