import {
  AlertTriangle,
  ArrowUp,
  BookOpen,
  ChevronRight,
  CircleDot,
  Copy,
  PanelLeft,
  Pencil,
  RefreshCw,
  RotateCcw,
  Search,
  ShieldAlert,
  ShieldCheck,
  Trash2,
  Wifi,
  WifiOff,
  X,
} from "lucide-react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  type FormEvent,
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
} from "react";
import {
  Link,
  useNavigate,
  useParams,
  useSearchParams,
} from "react-router-dom";
import {
  ApiError,
  createChatSession,
  getCitedPassage,
  getChatSession,
  listChatSessions,
  renameChatSession,
} from "../../api/client";
import type {
  Citation,
  LocalChatSession,
  PrincipalIdentity,
  SourceLocator,
} from "../../api/types";
import { useIdentity } from "../../app/IdentityContext";
import {
  useWorkspaceSidebar,
  WorkspaceSidebarActions,
  WorkspaceSidebarPortal,
} from "../../app/WorkspaceSidebar";
import {
  AttachmentButton,
  AttachmentTray,
  useKnowledgeAttachments,
} from "../../components/AttachmentTray";
import {
  KnowledgeSourcePicker,
  useKnowledgeBases,
} from "../../components/KnowledgeSourcePicker";
import { MarkdownContent } from "../../components/MarkdownContent";
import { TurnUsage, sumTurnUsage } from "../../components/TurnUsage";
import {
  ModeStarterPrompts,
  ModeStartHeader,
  submitTextareaOnEnter,
} from "../../components/ModeStart";
import { StepStream } from "../../components/StepStream";
import {
  EmptyState,
  ErrorNotice,
  IconButton,
  NewSessionAction,
  SidebarAction,
  LoadingLine,
  formatTime,
  shortId,
} from "../../components/ui";
import {
  hasUnfinishedTurn,
  type ChatConnectionState,
  type ChatSessionState,
  type ChatTurnState,
  type LiveTextState,
} from "./model";
import { deriveTurnStages, isTurnMetaActivity } from "./turnStages";
import type { ChatRuntime } from "./runtime";
import { useChatRuntime } from "./useChatRuntime";

const STARTER_PROMPTS = [
  {
    title: "梳理项目资料",
    prompt: "请梳理所选项目资料，提炼关键结论、主要分歧和下一步建议。",
    outcome: "输入：所选知识库 → 产出：带引用的结论摘要，引用可点开核对",
  },
  {
    title: "拆解复杂问题",
    prompt: "请先把这个问题拆成几个关键部分，再说明每一部分应该如何分析。",
    outcome: "产出：问题拆解与分析路径，不依赖资料",
  },
  {
    title: "准备一份方案",
    prompt: "请帮我比较几个可行方案，列出各自的收益、风险与推荐选择。",
    outcome: "产出：方案对比与推荐；要成文件请改用任务",
  },
] as const;

export function ChatPage() {
  const { identity } = useIdentity();
  const { runtime, state } = useChatRuntime(identity);
  const queries = useQueryClient();
  const serverSessions = useQuery({
    queryKey: ["chat-sessions", identity],
    queryFn: ({ signal }) => listChatSessions(identity, signal),
  });
  const { sessionId } = useParams<{ sessionId?: string }>();
  const [searchParams] = useSearchParams();
  const navigate = useNavigate();
  const selected =
    sessionId === undefined ? undefined : state.sessions[sessionId];
  const selectedServerSession = useQuery({
    queryKey: ["chat-session", identity, sessionId],
    enabled: sessionId !== undefined && selected === undefined,
    queryFn: ({ signal }) => {
      if (sessionId === undefined)
        throw new Error("Chat session id is required");
      return getChatSession(identity, sessionId, signal);
    },
  });
  const [question, setQuestion] = useState("");
  const [sessionQuery, setSessionQuery] = useState("");
  // 搜索折叠在标题右边那颗图标后面。展开时才占一行，收起时把查询也清掉——
  // 一个看不见却仍在过滤的搜索框，会让人以为会话丢了。
  const [searchOpen, setSearchOpen] = useState(false);
  const [sourceDrafts, setSourceDrafts] = useState<
    Record<string, string | null>
  >({});
  const [creatingSession, setCreatingSession] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);
  // Carries the runtime that opened it, and is read back through
  // `editingSessionId` below rather than cleared when the principal changes.
  // Derived, not reset, for the reason the rest of this codebase gives: an
  // effect that clears state is a render late, and `react-hooks/set-state-in-
  // effect` refuses it outright. A rename belongs to whoever started it.
  const [renamingSession, setRenamingSession] = useState<{
    owner: ChatRuntime;
    sessionId: string;
  } | null>(null);
  const [renamePending, setRenamePending] = useState<string | null>(null);
  const [renameError, setRenameError] = useState<{
    sessionId: string;
    message: string;
  } | null>(null);
  const workspaceSidebar = useWorkspaceSidebar();
  const mounted = useRef(true);
  const composerRef = useRef<HTMLTextAreaElement>(null);
  const latestSubmissionContext = useRef({ runtime, sessionId });
  const renameInputRef = useRef<HTMLInputElement | null>(null);
  const renameActionRefs = useRef(new Map<string, HTMLButtonElement>());
  const renameAttempt = useRef(0);

  const focusRenameAction = useCallback((target: string) => {
    window.requestAnimationFrame(() => {
      renameActionRefs.current.get(target)?.focus();
    });
  }, []);

  // Whether the reader is still sitting in the field they submitted. Every
  // focus move below is conditioned on it, because the two halves of a rename
  // are separated by a round trip: on the way out the caret is in the field
  // and the field is about to unmount, so focus has to be handed somewhere
  // deliberate or it falls to <body>; on the way back the reader may have
  // clicked another session, the composer, or a different page entirely --
  // and a PATCH that resolves a second later must not drag them back to a row
  // they left. `onBlur` cannot decide this for us: it declines to cancel while
  // a rename is pending, precisely so the request keeps its row.
  const renameFieldHasFocus = useCallback(
    () =>
      renameInputRef.current !== null &&
      renameInputRef.current === document.activeElement,
    [],
  );

  const beginRename = useCallback(
    (target: string) => {
      renameAttempt.current += 1;
      setRenamePending(null);
      setRenameError(null);
      setRenamingSession({ owner: runtime, sessionId: target });
    },
    [runtime],
  );

  // Only the principal on screen is editing anything. Nothing below has to
  // ask again: the pending flag and the inline error are both rendered inside
  // this branch, so scoping the row scopes them with it.
  const editingSessionId =
    renamingSession?.owner === runtime ? renamingSession.sessionId : null;

  const cancelRename = useCallback(
    (target: string, restoreFocus = true) => {
      renameAttempt.current += 1;
      setRenamePending(null);
      setRenameError(null);
      setRenamingSession(null);
      if (restoreFocus) focusRenameAction(target);
    },
    [focusRenameAction],
  );
  const selectedSessionNotFound =
    selectedServerSession.error instanceof ApiError &&
    selectedServerSession.error.status === 404;

  useLayoutEffect(() => {
    mounted.current = true;
    latestSubmissionContext.current = { runtime, sessionId };
    return () => {
      mounted.current = false;
    };
  }, [runtime, sessionId]);

  useEffect(() => {
    if (serverSessions.data === undefined) return;
    runtime.reconcileServerSessions(serverSessions.data.sessions);
  }, [runtime, serverSessions.data]);

  useEffect(() => {
    if (selectedServerSession.data === undefined) return;
    runtime.reconcileServerSessions([selectedServerSession.data]);
  }, [runtime, selectedServerSession.data]);

  const removeSession = useCallback(
    async (target: string) => {
      if (!window.confirm("删除这个对话？它的问答记录会一起消失。")) return;
      const submittedRuntime = runtime;
      try {
        await runtime.removeSession(target);
        await queries.invalidateQueries({
          queryKey: ["chat-sessions", identity],
        });
        const latest = latestSubmissionContext.current;
        if (!mounted.current || latest.runtime !== submittedRuntime) return;
        workspaceSidebar.close();
        // Asked of the route as it stands now, not of the one the click was
        // made under. Both directions were wrong when this compared against
        // the submitted route: a reader who opened another session mid-delete
        // was sent to /chat for nothing, and a reader who navigated *into* the
        // row being deleted was left sitting on a session the server no longer
        // has. What decides it is only ever whether the page is showing the
        // thing that just stopped existing.
        if (latest.sessionId === target) await navigate("/chat");
      } catch (cause: unknown) {
        if (
          mounted.current &&
          latestSubmissionContext.current.runtime === submittedRuntime
        ) {
          // Reported where the composer's own failures are reported. A delete
          // that the server refused has to say so: the row is still there, and
          // silence would read as a click that did nothing.
          setSubmitError(
            cause instanceof Error ? cause.message : String(cause),
          );
        }
      }
    },
    [identity, navigate, queries, runtime, workspaceSidebar],
  );


  const renameSession = useCallback(
    async (target: string, title: string) => {
      const trimmed = title.trim();
      if (trimmed === "" || state.sessions[target]?.title === trimmed) {
        cancelRename(target);
        return;
      }
      const attempt = renameAttempt.current + 1;
      renameAttempt.current = attempt;
      const submittedRuntime = runtime;
      setRenamePending(target);
      setRenameError(null);
      // Two questions are asked of every continuation below, and a third
      // deliberately is not. `attempt` catches a newer rename, an Escape or a
      // cancel having taken the row over, and `mounted` catches the page being
      // gone; the identity is asked separately because it decides *where* an
      // outcome may be written, not whether it is still wanted.
      //
      // What used to be asked as well was the *detail route* the rename was
      // submitted under -- and that was the bug. Renaming is an act on the
      // sidebar, which /chat and every /chat/:id show alike. Opening another
      // session while the PATCH was in flight made this return early with
      // `renamePending` still set to the row, so the field stayed `readOnly`
      // for the rest of the page's life and the only way out was a reload. A
      // route moving is not news to a rename.
      const release = () => {
        setRenamePending(null);
        setRenameError(null);
        setRenamingSession(null);
      };
      try {
        const accepted = await renameChatSession(identity, target, trimmed);
        if (accepted.title === null) {
          throw new Error("服务端没有返回这个对话的名字");
        }
        // The server is authoritative over normalization (including trimming),
        // so the local projection must use the accepted value rather than the
        // request string. Addressed to the runtime the rename was submitted
        // under, not to whichever one is current -- that is the one holding
        // the session this title belongs to.
        submittedRuntime.renameSession(target, accepted.title);
        await queries.invalidateQueries({
          queryKey: ["chat-sessions", identity],
        });
      } catch (cause: unknown) {
        if (renameAttempt.current !== attempt || !mounted.current) return;
        if (latestSubmissionContext.current.runtime !== submittedRuntime) {
          // Somebody else's page now. There is nowhere to report this, but the
          // edit still has to be let go of: left pending, switching back would
          // find the row locked mid-request with no request left to finish it.
          release();
          return;
        }
        const keepFocus = renameFieldHasFocus();
        setRenamePending(null);
        setRenameError({
          sessionId: target,
          message: cause instanceof Error ? cause.message : String(cause),
        });
        if (keepFocus) {
          window.requestAnimationFrame(() => {
            renameInputRef.current?.focus();
          });
        }
        return;
      }

      if (renameAttempt.current !== attempt || !mounted.current) return;
      if (latestSubmissionContext.current.runtime !== submittedRuntime) {
        release();
        return;
      }
      const keepFocus = renameFieldHasFocus();
      setRenamePending(null);
      setRenamingSession(null);
      if (keepFocus) focusRenameAction(target);
    },
    [
      cancelRename,
      focusRenameAction,
      identity,
      queries,
      renameFieldHasFocus,
      runtime,
      state.sessions,
    ],
  );

  const selectedSessionId = selected?.sessionId;
  useEffect(() => {
    if (selectedSessionId === undefined) return;
    const release = runtime.retainSessionStream(selectedSessionId);
    void runtime.ensureHistory(selectedSessionId);
    return release;
  }, [runtime, selectedSessionId]);

  const knowledgeBaseDraftKey = selectedSessionId ?? "new";
  const requestedKnowledgeBase = searchParams.get("kb");
  const requestedKnowledgeBaseId = Object.prototype.hasOwnProperty.call(
    sourceDrafts,
    knowledgeBaseDraftKey,
  )
    ? (sourceDrafts[knowledgeBaseDraftKey] ?? null)
    : (selected?.knowledgeBaseId ?? requestedKnowledgeBase);
  const knowledgeBases = useKnowledgeBases(identity);
  const knowledgeBaseId =
    requestedKnowledgeBaseId !== null &&
    knowledgeBases.data?.knowledge_bases.some(
      (item) => item.knowledge_base_id === requestedKnowledgeBaseId,
    )
      ? requestedKnowledgeBaseId
      : null;
  const sourceResolving =
    requestedKnowledgeBaseId !== null && knowledgeBases.isPending;
  const answerMode = knowledgeBaseId === null ? "direct" : "rag";
  const selectedKnowledgeBase = knowledgeBases.data?.knowledge_bases.find(
    (item) => item.knowledge_base_id === knowledgeBaseId,
  );
  const attachments = useKnowledgeAttachments(identity, knowledgeBaseId);
  const turns =
    selected === undefined
      ? []
      : (state.turnOrderBySession[selected.sessionId] ?? []).flatMap(
          (turnId) => {
            const turn = state.turns[turnId];
            return turn === undefined ? [] : [turn];
          },
        );
  const quarantined =
    selected === undefined
      ? []
      : (state.quarantinedSequences[selected.sessionId] ?? []);
  const unfinished =
    selected === undefined
      ? false
      : hasUnfinishedTurn(state, selected.sessionId);
  const composerDisabled =
    creatingSession ||
    sourceResolving ||
    unfinished ||
    selected?.history === "loading";
  const visibleSessionIds = state.sessionOrder.filter((id) => {
    const session = state.sessions[id];
    if (session === undefined) return false;
    const query = sessionQuery.trim().toLocaleLowerCase();
    if (query === "") return true;
    return `${session.title} ${session.sessionId}`
      .toLocaleLowerCase()
      .includes(query);
  });
  const composerNotice =
    attachments.readOnlyReason !== null
      ? attachments.readOnlyReason
      : attachments.items.some((item) => item.state === "waiting_for_source")
        ? "附件需要先选择知识库"
        : attachments.hasBlockingItems
          ? "附件可检索后才能发送"
          : null;

  const changeSource = (nextId: string | null) => {
    if (nextId === knowledgeBaseId) return;
    if (
      attachments.items.length > 0 &&
      !window.confirm("切换知识库会清空当前待发送的附件，是否继续？")
    ) {
      return;
    }
    attachments.clear();
    setSourceDrafts((current) => ({
      ...current,
      [knowledgeBaseDraftKey]: nextId,
    }));
  };

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const trimmedQuestion = question.trim();
    if (!trimmedQuestion || composerDisabled || attachments.hasBlockingItems)
      return;
    setSubmitError(null);

    // Captured before anything is awaited: both continuations below ask what
    // has changed since the reader pressed send, and neither can ask a closure
    // that was created after the change.
    const submittedRuntime = runtime;
    const submittedSessionId = sessionId;

    try {
      let target = selected;
      if (target === undefined) {
        setCreatingSession(true);
        const opened = await createChatSession(
          identity,
          trimmedQuestion.slice(0, 200),
        );
        const now = new Date().toISOString();
        target = {
          sessionId: opened.session_id,
          title: trimmedQuestion,
          answerMode,
          knowledgeBaseId,
          createdAt: now,
          updatedAt: now,
          connection: "idle",
          history: "loaded",
        };
        runtime.addLocalSession(localSession(target));
        void queries.invalidateQueries({
          queryKey: ["chat-sessions", identity],
        });
        const latest = latestSubmissionContext.current;
        if (!mounted.current || latest.runtime !== submittedRuntime) {
          // The Session already exists, so retain its local handle under the
          // identity that created it. Starting an Ask after the user switched
          // identity would execute a new side effect in stale context.
          //
          // The *route* is deliberately not asked about, and this is the third
          // place that mattered. An Ask is addressed to `target.sessionId` --
          // the session this POST just created -- not to whatever the address
          // bar says. Comparing against the submitted route meant that opening
          // an existing session while the create was in flight dropped the
          // question entirely: the reader was left with a new empty session in
          // their list, their sentence still in the composer, and nothing to
          // say the send had done nothing. Pressing send again then asked it
          // in a different session from the one the first press had made.
          return;
        }
      }

      runtime.startAsk({
        sessionId: target.sessionId,
        question: trimmedQuestion,
        answerMode,
        knowledgeBaseId,
      });
      setQuestion("");
      attachments.clear();
      // The navigation, unlike the Ask, *is* the route's business: it may only
      // move a reader who has not already moved themselves.
      if (
        selected === undefined &&
        mounted.current &&
        latestSubmissionContext.current.sessionId === submittedSessionId
      ) {
        void navigate(`/chat/${encodeURIComponent(target.sessionId)}`);
      }
    } catch (error) {
      if (mounted.current) {
        setSubmitError(error instanceof Error ? error.message : "无法发送问题");
      }
    } finally {
      if (mounted.current) setCreatingSession(false);
    }
  };

  const chooseStarter = (prompt: string) => {
    setQuestion(prompt);
    window.requestAnimationFrame(() => composerRef.current?.focus());
  };

  return (
    <div className={`aw-chat-page ${sessionId === undefined ? "is-new" : ""}`}>
      <WorkspaceSidebarActions>
        {state.sessionOrder.length >= 5 || sessionQuery !== "" ? (
          <SidebarAction
            active={searchOpen}
            label="搜索对话"
            onClick={() => {
              setSearchOpen((open) => {
                if (open) setSessionQuery("");
                return !open;
              });
            }}
          >
            <Search aria-hidden="true" size={15} />
          </SidebarAction>
        ) : null}
        <NewSessionAction
          label="新对话"
          onClick={() => {
            workspaceSidebar.close();
            void navigate("/chat");
          }}
        />
      </WorkspaceSidebarActions>
      <WorkspaceSidebarPortal>
        <aside
          className={`aw-chat-sessions ${workspaceSidebar.drawerOpen ? "is-mobile-open" : ""}`}
          // 这个区域可见的名字是「最近对话」，读屏软件此前听到的却是
          // 「Chat 会话」——同一块地方两个名字，还夹着一个英文产品名。
          aria-label="最近对话"
        >
          <IconButton
            className="aw-chat-sessions-close"
            label="关闭对话列表"
            onClick={workspaceSidebar.close}
          >
            <X aria-hidden="true" size={17} />
          </IconButton>

          {searchOpen ? (
            <div className="aw-chat-session-search">
              <Search aria-hidden="true" size={14} />
              <input
                aria-label="搜索对话"
                autoFocus
                onChange={(event) => setSessionQuery(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key !== "Escape") return;
                  setSessionQuery("");
                  setSearchOpen(false);
                }}
                placeholder="搜索对话"
                type="search"
                value={sessionQuery}
              />
            </div>
          ) : null}
          <div className="aw-chat-session-list">
            {state.sessionOrder.length === 0 ? (
              <p className="aw-chat-local-note">
                发送第一条消息后，对话会出现在这里。
              </p>
            ) : visibleSessionIds.length === 0 ? (
              <p className="aw-chat-local-note">没有匹配的对话。</p>
            ) : (
              visibleSessionIds.map((id) => {
                const session = state.sessions[id];
                if (session === undefined) return null;
                return (
                  <div className="aw-chat-session-row" key={session.sessionId}>
                    {editingSessionId === session.sessionId ? (
                      <>
                        <form
                          aria-busy={renamePending === session.sessionId}
                          className="aw-session-inline-rename"
                          onSubmit={(event) => {
                            event.preventDefault();
                            if (renamePending === session.sessionId) return;
                            const field = new FormData(event.currentTarget).get(
                              "title",
                            );
                            void renameSession(
                              session.sessionId,
                              typeof field === "string" ? field : "",
                            );
                          }}
                        >
                          <label
                            className="aw-sr-only"
                            htmlFor={`aw-chat-rename-${session.sessionId}`}
                          >
                            对话名字
                          </label>
                          <input
                            aria-describedby={
                              renameError?.sessionId === session.sessionId
                                ? `aw-chat-rename-error-${session.sessionId}`
                                : undefined
                            }
                            aria-invalid={
                              renameError?.sessionId === session.sessionId ||
                              undefined
                            }
                            autoFocus
                            defaultValue={session.title}
                            id={`aw-chat-rename-${session.sessionId}`}
                            name="title"
                            onBlur={() => {
                              if (renamePending !== session.sessionId) {
                                cancelRename(session.sessionId, false);
                              }
                            }}
                            onChange={() => {
                              if (
                                renameError?.sessionId === session.sessionId
                              ) {
                                setRenameError(null);
                              }
                            }}
                            onFocus={(event) => event.currentTarget.select()}
                            onKeyDown={(event) => {
                              if (event.key !== "Escape") return;
                              event.preventDefault();
                              event.stopPropagation();
                              cancelRename(session.sessionId);
                            }}
                            readOnly={renamePending === session.sessionId}
                            ref={renameInputRef}
                          />
                        </form>
                        {renameError?.sessionId === session.sessionId ? (
                          <div
                            className="aw-session-rename-error"
                            id={`aw-chat-rename-error-${session.sessionId}`}
                          >
                            <ErrorNotice message={renameError.message} />
                          </div>
                        ) : null}
                      </>
                    ) : (
                      <>
                        <Link
                          aria-current={
                            session.sessionId === sessionId ? "page" : undefined
                          }
                          className={`aw-chat-session ${session.sessionId === sessionId ? "is-active" : ""}`}
                          onClick={workspaceSidebar.close}
                          onKeyDown={(event) => {
                            if (event.key !== "F2") return;
                            event.preventDefault();
                            beginRename(session.sessionId);
                          }}
                          to={`/chat/${encodeURIComponent(session.sessionId)}`}
                        >
                          <span className="aw-chat-session-copy">
                            <strong>{session.title}</strong>
                            <small>{formatTime(session.updatedAt)}</small>
                          </span>
                        </Link>
                        <span className="aw-session-row-actions">
                          <button
                            aria-label={`重命名对话 ${session.title}`}
                            className="aw-chat-session-rename"
                            onClick={() => beginRename(session.sessionId)}
                            ref={(node) => {
                              if (node === null) {
                                renameActionRefs.current.delete(
                                  session.sessionId,
                                );
                              } else {
                                renameActionRefs.current.set(
                                  session.sessionId,
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
                            aria-label={`删除对话 ${session.title}`}
                            className="aw-chat-session-delete"
                            onClick={() =>
                              void removeSession(session.sessionId)
                            }
                            title="删除"
                            type="button"
                          >
                            <Trash2 aria-hidden size={13} />
                          </button>
                        </span>
                      </>
                    )}
                  </div>
                );
              })
            )}
          </div>
        </aside>
      </WorkspaceSidebarPortal>

      <main
        className={`aw-chat-main ${sessionId === undefined ? "aw-mode-start" : ""}`}
      >
        {sessionId === undefined ? null : (
          <ChatHeader
            answerMode={answerMode}
            session={selected}
            sidebarOpen={workspaceSidebar.drawerOpen}
            {...(selectedKnowledgeBase === undefined
              ? {}
              : { sourceLabel: selectedKnowledgeBase.name })}
            {...(selected === undefined
              ? {}
              : {
                  onReconnect: () =>
                    runtime.reconnectSessionStream(selected.sessionId),
                })}
            onOpenSessions={workspaceSidebar.open}
          />
        )}

        {/* Not a live region, deliberately. This used to carry
            `aria-live="polite"` over the whole subtree: every turn, every
            step line, every citation chip and every disclosure toggle.
            A region that announces all of it announces nothing usable --
            the reader cannot tell which sentence is the new one, and the
            token stream inside re-fires it several times a second.
            The transcript is readable content; what deserves announcing
            is the phase, and each turn says that for itself below. */}
        <section className="aw-chat-transcript">
          {sessionId !== undefined &&
          selected === undefined &&
          (selectedServerSession.isPending ||
            selectedServerSession.data !== undefined) ? (
            <LoadingLine label="正在确认这个对话" />
          ) : null}
          {sessionId !== undefined &&
          selected === undefined &&
          selectedSessionNotFound ? (
            <EmptyState
              icon={<ShieldAlert aria-hidden="true" size={24} />}
              title="这个对话不属于当前身份"
              description="可能是换过身份，也可能这个链接本来就不是给你的——用当前身份查不到它。"
              action={
                <button
                  className="aw-button is-primary"
                  onClick={() => navigate("/chat")}
                  type="button"
                >
                  开始新对话
                </button>
              }
            />
          ) : sessionId !== undefined &&
            selected === undefined &&
            selectedServerSession.isError ? (
            <EmptyState
              icon={<AlertTriangle aria-hidden="true" size={24} />}
              title="暂时无法确认这个对话"
              description={
                selectedServerSession.error instanceof Error
                  ? selectedServerSession.error.message
                  : "请检查连接后重试。"
              }
              action={
                <button
                  className="aw-button is-primary"
                  onClick={() => void selectedServerSession.refetch()}
                  type="button"
                >
                  重新确认
                </button>
              }
            />
          ) : selected === undefined ? (
            <ChatWelcome
              onOpenSessions={workspaceSidebar.open}
              sidebarOpen={workspaceSidebar.drawerOpen}
            />
          ) : selected.history === "loading" && turns.length === 0 ? (
            <LoadingLine label="正在读取历史消息" />
          ) : selected.history === "failed" && turns.length === 0 ? (
            <div className="aw-chat-centered-notice">
              <ErrorNotice
                message={selected.historyError ?? "读不到这个对话的历史消息"}
              />
              <button
                className="aw-button is-ghost"
                onClick={() => void runtime.ensureHistory(selected.sessionId)}
                type="button"
              >
                重试读取
              </button>
            </div>
          ) : turns.length === 0 ? (
            <EmptyState
              icon={<BookOpen aria-hidden="true" size={25} />}
              title="这个对话还是空的"
              description="可以直接提问，也可以选择知识库后获得带引用的回答。Enter 发送，Shift + Enter 换行。"
            />
          ) : (
            <div className="aw-chat-turn-list">
              {turns.map((turn) => (
                <ChatTurn
                  identity={identity}
                  key={turn.localId}
                  turn={turn}
                  {...(turn.historical
                    ? {}
                    : { onRetry: () => runtime.retryAsk(turn.localId) })}
                />
              ))}
            </div>
          )}
          {/* Under the transcript, not over it: it is a statement about what a
              reader has just scrolled through. */}
          <SessionGapNotice sequences={quarantined} />
        </section>

        <form
          aria-busy={creatingSession || unfinished}
          className="aw-chat-composer"
          onSubmit={submit}
        >
          {submitError === null ? null : <ErrorNotice message={submitError} />}
          {/* 整段会话的合计，坐在输入框上方。每一轮下面那行答的是「这一轮」，
              而人问「这段聊下来花了多少」的时候不会去把十行加起来。 */}
          <TurnUsage
            label="这段会话"
            usage={sumTurnUsage(turns.map((turn) => turn.usage))}
          />
          {/* 一张卡，不是两截。此前工具行（知识库选择、字数）在圆角卡片**外面**
              另起一行，于是「输入这件事」在版面上被切成两个互不相干的块，读起来
              像一个搜索框底下挂了一条说明。现在卡片自己包住三段：附件、正文、
              底部的工具行，发送键在卡内右下角——和这个界面里其他几处输入
              （编码页的会话输入框）是同一个形状。 */}
          <div className="aw-chat-composer-card aw-mode-composer-card">
            <AttachmentTray
              items={attachments.items}
              onRemove={attachments.remove}
              onRetry={attachments.retry}
              targetName={attachments.targetName}
            />
            <textarea
              aria-label="问题"
              aria-keyshortcuts="Enter"
              disabled={composerDisabled}
              enterKeyHint="send"
              maxLength={4096}
              onChange={(event) => setQuestion(event.target.value)}
              onKeyDown={submitTextareaOnEnter}
              placeholder={
                unfinished
                  ? "请等待当前回答完成…"
                  : answerMode === "direct"
                    ? "输入消息…"
                    : `询问 ${selectedKnowledgeBase?.name ?? "所选知识库"}…`
              }
              rows={1}
              ref={composerRef}
              value={question}
            />

            <div className="aw-chat-composer-tools">
              <AttachmentButton
                disabled={
                  composerDisabled || attachments.readOnlyReason !== null
                }
                {...(attachments.readOnlyReason === null
                  ? {}
                  : { disabledReason: attachments.readOnlyReason })}
                onFiles={attachments.addFiles}
              />
              <KnowledgeSourcePicker
                compact
                disabled={composerDisabled}
                identity={identity}
                onChange={(knowledgeBase) =>
                  changeSource(knowledgeBase?.knowledge_base_id ?? null)
                }
                value={knowledgeBaseId}
              />
              {composerNotice === null ? null : <span>{composerNotice}</span>}
              <span className="aw-chat-composer-spacer" />
              {/* 4096 这个上限一直在（textarea 上的 maxLength），只是看不见：打到
                头的人得到的是一个不再接受输入的输入框，没有任何东西说明为什么。
                放在这一行而不是压在输入框上：它是关于这次输入的说明，和左边那句
                「回答会检索资料并标注引用」是同一类东西。 */}
              {question.length < 3500 ? null : (
                <span
                  className={`aw-chat-counter ${question.length >= 4096 ? "is-full" : ""}`}
                >
                  {question.length} / 4096
                </span>
              )}
              <button
                aria-label="发送问题"
                className="aw-button is-primary aw-chat-send aw-mode-send"
                disabled={
                  composerDisabled ||
                  !question.trim() ||
                  attachments.hasBlockingItems
                }
                type="submit"
              >
                {creatingSession ? (
                  <RefreshCw aria-hidden="true" className="aw-spin" size={17} />
                ) : (
                  <ArrowUp aria-hidden="true" size={17} />
                )}
              </button>
            </div>
          </div>
          {sessionId === undefined ? (
            <ModeStarterPrompts
              disabled={composerDisabled}
              items={STARTER_PROMPTS}
              label="对话起点"
              onChoose={chooseStarter}
            />
          ) : null}
        </form>
      </main>
    </div>
  );
}

function ChatWelcome({
  onOpenSessions,
  sidebarOpen,
}: {
  onOpenSessions: () => void;
  sidebarOpen: boolean;
}) {
  return (
    <div className="aw-chat-welcome">
      <ModeStartHeader
        action={
          <IconButton
            className="aw-chat-mobile-sessions"
            controls="workspace-sidebar-context"
            expanded={sidebarOpen}
            label="打开对话列表"
            onClick={onOpenSessions}
          >
            <PanelLeft aria-hidden="true" size={18} />
          </IconButton>
        }
        description="直接提问，或选一个知识库：回答会带上来源，点开引用能核对原文。"
        title="有什么可以帮你？"
      />
    </div>
  );
}

function ChatHeader({
  answerMode,
  session,
  sourceLabel,
  onReconnect,
  onOpenSessions,
  sidebarOpen,
}: {
  answerMode: "direct" | "rag";
  session: ChatSessionState | undefined;
  sourceLabel?: string;
  onReconnect?: () => void;
  onOpenSessions: () => void;
  sidebarOpen: boolean;
}) {
  return (
    <header className="aw-chat-header">
      <IconButton
        className="aw-chat-mobile-sessions"
        controls="workspace-sidebar-context"
        expanded={sidebarOpen}
        label="打开对话列表"
        onClick={onOpenSessions}
      >
        <PanelLeft aria-hidden="true" size={18} />
      </IconButton>
      <div>
        <h1>{session?.title ?? "新对话"}</h1>
        {session !== undefined && answerMode === "rag" ? (
          <p>{sourceLabel ?? "知识库"}</p>
        ) : null}
        {/* 这里曾经有一个归属选择器。ADR-074 把 Project 收进了 Code——它现在是
            编码工作区，一个目录，而一段对话住不进目录里。
            服务端那一列和那个端点都还在（ADR-074 §6：下线一个端点和删一列数据是
            同一种破坏的两种形式），只是界面不再写它。 */}
      </div>
      {session === undefined ||
      ["idle", "connected"].includes(session.connection) ? null : (
        <ConnectionBadge
          connection={session.connection}
          {...(onReconnect === undefined ? {} : { onReconnect })}
        />
      )}
    </header>
  );
}

function ConnectionBadge({
  connection,
  onReconnect,
}: {
  connection: ChatConnectionState;
  onReconnect?: () => void;
}) {
  const connected = connection === "connected";
  const label =
    {
      idle: "",
      connecting: "正在连接",
      connected: "",
      retrying: "正在重连",
      unavailable: "连接中断",
    }[connection] ?? connection;
  return (
    // `role="status"` because this badge sits in the header, *outside*
    // the transcript, and losing the connection mid-answer was announced
    // to nobody at all. The polite live region carries the label, which
    // is why the label is also what changes -- 正在重连 / 连接中断 are
    // the two sentences worth interrupting a reader for.
    //
    // The `title` says what to do about it; it cannot say what happened,
    // because a title is not read until it is hovered.
    <button
      aria-live="polite"
      className={`aw-chat-connection ${connected ? "is-connected" : ""}`}
      disabled={
        onReconnect === undefined || connected || connection === "connecting"
      }
      onClick={onReconnect}
      role="status"
      title={connected ? undefined : "点击重新连接"}
      type="button"
    >
      {connected ? (
        <Wifi aria-hidden="true" size={15} />
      ) : (
        <WifiOff aria-hidden="true" size={15} />
      )}
      {label}
    </button>
  );
}

function ChatTurn({
  identity,
  onRetry,
  turn,
}: {
  /** Carried only so a citation can be opened; nothing else here fetches. */
  identity: PrincipalIdentity;
  onRetry?: () => void;
  turn: ChatTurnState;
}) {
  return (
    <article className="aw-chat-turn">
      <div className="aw-chat-user-message">
        <span>你</span>
        <p>{turn.question}</p>
      </div>
      <div className="aw-chat-assistant-message">
        {/* 此前每一条回答上面都顶着一行「(A) Agent [历史]」。三个元素里两个
            是常量——一个圆头像和一个永远写着 Agent 的名字，在只有两个说话人
            且用户消息靠右成气泡的版面里，它们不携带任何信息，只是把回答从
            一份文档降级成一条聊天记录。
            「历史」不是常量，所以它留着：现在它没有陪衬，出现即有意义。 */}
        {turn.historical ? <p className="aw-chat-turn-tag">历史消息</p> : null}
        {turn.activities.length === 0 ? null : <TurnStepStream turn={turn} />}
        {turn.phase === "withheld" ? (
          // 「不是什么」这三句是这一块最值钱的部分，理由和多 Agent 面板里那三句
          // 一样：这个失败长得像另外三个问题，而那三个的修法完全不同。少了它们，
          // 读者最可能做的事是把同一句话再问一遍——那不会有任何变化，因为变的
          // 不是问题，是那几份文档的权限。
          //
          // 没有按钮。「换掉这几段重问」需要知道是哪几段被撤了权，而一条被扣下
          // 的回答连引用都不发出来——那正是它被扣下的原因。画一颗按钮，等于承诺
          // 一个这一层做不到的动作。
          <div className="aw-notice is-warning aw-chat-withheld">
            <AlertTriangle aria-hidden="true" size={16} />
            <div>
              <strong>
                这条回答写完了，但没有发出来：发出去之前又查了一遍来源，这一次没通过。
              </strong>
              <p>
                整条作废而不是删掉那几段再发——删几段之后剩下的句子仍然是<b>用它们推出来的</b>。这一笔 token 已经花掉了，所以它照常记在下面那行用量里。
              </p>
              <ul>
                <li>不是模型出错，它已经答完了</li>
                <li>不是检索没找到，找到了才写得出来</li>
                <li>不是你一直没有权限——是那几份资料在这几秒里被改了权限</li>
              </ul>
            </div>
          </div>
        ) : null}
        {turn.answer === undefined ? null : (
          <MarkdownContent text={turn.answer} />
        )}
        {/* 贴在答案正下方，在引用之前：读者问「这一轮怎么这么慢/这么贵」的那一
            刻，眼睛正停在答案末尾。收进任何需要展开的地方，这个问题就问不出来
            了，而那是唯一会当场问它的时刻。 */}
        <TurnUsage usage={turn.usage} />
        {turn.stream === undefined ? null : <LiveText stream={turn.stream} />}
        {!turn.historical &&
        (turn.phase === "submitting" || turn.phase === "running") ? (
          <LoadingLine
            label={
              turn.phase === "submitting"
                ? "正在发送"
                : turn.stream !== undefined && !turn.stream.redacted
                  ? "正在生成，尚未发布"
                  : "正在整理回答"
            }
          />
        ) : null}
        {/* The settled outcome, once, in one sentence. `LoadingLine`
            above is itself a `role="status"`, so the running phases are
            announced; without this the *end* -- the answer arriving --
            was the one thing that stayed silent. */}
        {turn.historical ||
        turn.phase === "submitting" ||
        turn.phase === "running" ? null : (
          <p aria-atomic="true" className="aw-sr-only" role="status">
            {turn.phase === "withheld"
              ? "回答没有发出来"
              : turn.answer === undefined
                ? "这一轮没有回答"
                : `回答已生成，${String(turn.citations.length)} 条引用`}
          </p>
        )}
        {turn.historical && turn.answer === undefined ? (
          <p className="aw-chat-no-citations">
            这一轮只留下了你的问题，没有留下回答。
          </p>
        ) : null}
        {turn.historical && turn.answer !== undefined ? (
          // The history endpoint returns role and text and nothing else, so a
          // reloaded answer carries no citations and no grounded flag. Running
          // the live verdict here would tell the reader "服务端没有为这段答案发布引用"
          // about answers that did publish citations, and would quietly drop the
          // ungrounded warning off answers that earned one.
          <p className="aw-chat-no-citations">
            <CircleDot aria-hidden="true" size={13} />
            历史记录只保存对话文本，不含引用与证据标记
          </p>
        ) : null}
        {turn.phase === "failed" ? (
          <div className="aw-chat-turn-error">
            <ErrorNotice message={turn.error ?? "这次回答未能完成"} />
            {onRetry === undefined ? null : (
              <button
                className="aw-button is-ghost"
                onClick={onRetry}
                type="button"
              >
                <RotateCcw aria-hidden="true" size={15} />
                重试
              </button>
            )}
          </div>
        ) : null}
        {!turn.historical &&
        (turn.phase === "committed" || turn.phase === "withheld") ? (
          <>
            <Citations
              answerMode={turn.answerMode}
              citations={turn.citations}
              identity={identity}
              sessionId={turn.sessionId}
              {...(turn.turnId === undefined ? {} : { turnId: turn.turnId })}
              withheld={turn.phase === "withheld"}
              grounded={turn.grounded !== false}
            />
            {/* Only for an answer that was actually published with evidence. A
                withheld turn has nothing to hand on, and an ungrounded one has
                no citations to carry -- offering the control there would let a
                reader paste "答案 + 引用" that was only ever an answer. */}
            {turn.phase === "committed" &&
            turn.answer !== undefined &&
            turn.citations.length > 0 ? (
              <CopyAnswer answer={turn.answer} citations={turn.citations} />
            ) : null}
          </>
        ) : null}
      </div>
    </article>
  );
}

/**
 * The turn's thinking, in the same shape Work shows a Task's.
 *
 * This replaces a flat list of every durable event. That list was already
 * openable, but it read as a log rather than as work: eight lines that did not
 * say which of them were the same phase, and no way to collapse the ones the
 * reader was done with. Grouping into the three things a turn does gives the
 * same detail behind three lines, and the running one opens itself.
 */

/**
 * What the model is writing, before anything has been published.
 *
 * Marked as provisional in the markup and in words, because it is the one
 * thing on this page that is not a published answer: it has not crossed the
 * release boundary, it may still be withheld, and it disappears when the turn
 * settles rather than turning into the answer above it.
 *
 * `redacted` renders nothing at all. Retrieval-backed shapes stream deltas the
 * server has deliberately emptied (ADR-052), so there is genuinely no text --
 * and an empty bubble claiming to be live output would be a worse lie than the
 * waiting line beneath it.
 */
function LiveText({ stream }: { stream: LiveTextState }) {
  if (stream.redacted || stream.text === "") return null;
  return (
    // No `aria-live`: this <p> is rewritten on every delta, so announcing
    // it means re-reading the whole partial answer several times a
    // second. The turn's own status line says 正在生成 once.
    <div className="aw-chat-live-text">
      <p>{stream.text}</p>
      <small>
        <CircleDot aria-hidden="true" size={12} />
        正在生成，内容可能继续调整
        {stream.dropped > 0
          ? `（有 ${String(stream.dropped)} 段未能送达）`
          : ""}
      </small>
    </div>
  );
}

function TurnStepStream({ turn }: { turn: ChatTurnState }) {
  const stages = deriveTurnStages(turn.activities, turn.phase);
  const meta = turn.activities.filter(isTurnMetaActivity);
  const running = turn.phase === "submitting" || turn.phase === "running";

  return (
    <>
      {/* 三个阶段折成一行「过程」。
       *
       * 已经答完的一轮，读者要的是答案；过程是他起疑时才展开的东西——摊开三行
       * 阶段会把答案推下去。折叠行上并排列出三个阶段名，所以"它做了哪三件事"
       * 不用展开就看得到，哪一件出了问题也带着自己的颜色。
       *
       * 跑着的时候强制展开：StepStream 会把正在跑的那一段自己打开，外面再套一
       * 层收起来的壳，等于把实时进度藏了。 */}
      <details className="aw-turn-process" open={running}>
        <summary>
          <ChevronRight
            aria-hidden="true"
            className="aw-step-caret"
            size={13}
          />
          <span className="aw-turn-process-label">过程</span>
          {stages.map((stage) => (
            <span className={`aw-turn-phase is-${stage.state}`} key={stage.id}>
              {stage.title}
            </span>
          ))}
        </summary>
        <StepStream
          ariaLabel="回答过程"
          // The label the turn already computed. It carries what the event meant
          // in Chat's own vocabulary -- "答案已发布（未经证据核实）" is a distinction
          // Work's generic titles do not draw.
          eventTitle={(event) =>
            turn.activities.find((activity) => activity.envelope === event)
              ?.label ?? event.event_type
          }
          meta={{
            title: "运行记录",
            events: meta.map((activity) => activity.envelope),
          }}
          running={running}
          stages={stages}
        />
      </details>
    </>
  );
}

/**
 * The answer and its evidence, in one paste.
 *
 * The smallest honest thing this page could gain. Chat has no artifact
 * container and is deliberately not getting one -- an answer is not a produced
 * file, and giving Chat a third artifact mechanism (storage, addressing,
 * lifetime, collection) to solve "I want to send this to a colleague" would be
 * paying for a warehouse to move one box. Nothing here is stored, nothing is
 * addressed, and no download control appears (every surface in this console
 * carries exactly one labelled 下载, and the count is pinned by tests).
 *
 * What it fixes is small and real: the ids are shortened on screen
 * (`shortId` cuts the middle out), so a reader who selected the citation row
 * and pasted it got `[chunk_01…8f]` -- an identifier nobody can look anything
 * up with. Every id goes out in full here.
 *
 * The clipboard write can be refused (an insecure origin, a denied permission)
 * and the refusal is reported rather than swallowed: a copy button that does
 * nothing and says nothing is worse than no copy button, because the reader
 * walks away believing they have the text.
 */
function CopyAnswer({
  answer,
  citations,
}: {
  answer: string;
  citations: Citation[];
}) {
  const [state, setState] = useState<"idle" | "copied" | "failed">("idle");
  return (
    <div className="aw-chat-copy">
      <button
        className="aw-button is-ghost"
        onClick={() => {
          const lines = citations.map((citation) => {
            const locator = citationLocator(citation.locator);
            return `- ${citation.chunk_id} · ${citation.document_id} · ${citation.document_version}${locator === null ? "" : ` · ${locator}`}`;
          });
          void navigator.clipboard
            .writeText(`${answer}\n\n## 引用\n\n${lines.join("\n")}\n`)
            .then(() => {
              setState("copied");
            })
            .catch(() => {
              setState("failed");
            });
        }}
        type="button"
      >
        <Copy aria-hidden="true" size={14} />
        复制答案与引用
      </button>
      {state === "idle" ? null : (
        <small aria-live="polite">
          {state === "copied" ? "已复制" : "复制失败，浏览器拒绝了剪贴板写入"}
        </small>
      )}
    </div>
  );
}

function Citations({
  answerMode,
  citations,
  identity,
  sessionId,
  turnId,
  withheld,
  grounded,
}: {
  answerMode: "direct" | "rag";
  citations: Citation[];
  identity: PrincipalIdentity;
  sessionId: string;
  /**
   * Absent for a turn this page never bound one to -- a reload, or a claim
   * that failed before the response came back. The chips still render; they
   * simply do not open, because the passage route is addressed through the
   * turn and there is nothing honest to send.
   */
  turnId?: string;
  withheld: boolean;
  grounded: boolean;
}) {
  if (withheld) {
    return (
      <p className="aw-chat-no-citations">
        <ShieldAlert aria-hidden="true" size={14} />
        被阻止的答案不发布引用
      </p>
    );
  }
  // Checked before the empty-citation case, because the two look identical
  // from the citation list alone and mean opposite things. "No citations
  // published" invites the reader to wonder what went wrong; this one is the
  // correct and complete output of a path that never retrieved.
  if (!grounded) {
    // Two different events land here and only one of them skipped retrieval.
    // A turn the reader sent at a knowledge base *was* searched -- the server
    // looked, judged nothing relevant enough to answer from, and fell back.
    // Telling them it "did not search the knowledge base" would contradict the
    // knowledge base they picked and the label above this answer.
    return (
      <p className="aw-chat-no-citations">
        <ShieldAlert aria-hidden="true" size={14} />
        {answerMode === "rag"
          ? "已检索所选知识库，但没有找到足够相关的内容；这条回答由模型直接作答，没有引用"
          : "这条回答没有查资料，是模型直接作答的，所以没有引用"}
      </p>
    );
  }
  if (citations.length === 0) {
    return (
      <p className="aw-chat-no-citations">
        <CircleDot aria-hidden="true" size={13} />
        服务端没有为这段答案发布引用
      </p>
    );
  }
  // 到这里为止，withheld / 未接地 / 无引用 三种情况都已经各自返回了。剩下的这
  // 一种此前没有任何标记——三条否定各有说法，唯一的肯定反而是沉默的。
  return (
    <div className="aw-chat-citations" aria-label="引用">
      <p className="aw-chat-grounded">
        <ShieldCheck aria-hidden="true" size={13} />
        已核对来源 · {citations.length} 条引用
      </p>
      {citations.map((citation, index) => (
        <CitationRow
          citation={citation}
          identity={identity}
          key={`${citation.chunk_id}:${citation.document_version}`}
          ordinal={index + 1}
          sessionId={sessionId}
          {...(turnId === undefined ? {} : { turnId })}
        />
      ))}
      {/* One note, and it answers the question a reader actually arrives
          with once the rows start refusing: why is the text not simply
          *here*, next to the claim it supports. Saying it under the rows
          rather than in an ADR nobody on this page has open is what makes a
          later 读不到 read as the design working rather than the console
          breaking.

          There used to be a second paragraph here, and it was written to the
          maintainer rather than to the reader: 三段寻址, chunk, revision,
          document_id, turn id, 读接口 -- six internal words in two sentences,
          under every grounded answer, addressed to somebody who came to check
          whether an answer is true. Both facts it carried have better homes.
          That a row can be inert now says so on the row itself (the chip's
          own `title`); that a document has no readable name needs no sentence
          at all, because the row simply shows what it has. */}
      <div className="aw-chat-citation-gap">
        <p>
          原文不跟答案一起存下来：每次点开都重新去读一次，权限也重新核一遍——所以昨天能看的引用，今天可能正确地打不开。
        </p>
      </div>
    </div>
  );
}

/**
 * One citation, and the passage behind it when the reader asks for it.
 *
 * The chip used to be an inert `<span>`: the only thing a reader could do with
 * the evidence for a claim was read a 16-character id with its middle cut out.
 * Checking an answer meant opening the knowledge base and searching by hand,
 * which is exactly as much work as having no citations at all.
 *
 * **Opening one is a fresh read, and it can correctly fail.** The server
 * re-decides authorization from scratch (ADR-067), so a citation still on
 * screen may answer 404 because the grant was revoked or the document was
 * re-ingested since the answer was published. That is the right behaviour and
 * the wording has to carry it: 读不到 rather than 出错了. A stored citation is a
 * record of what was answered, never a standing permit to read it again.
 *
 * Fetched on demand and cached forever after. `staleTime: Infinity` because a
 * passage that came back is the passage as of the check that let it through;
 * re-polling it would spend reads to eventually contradict what the reader is
 * looking at, with no action available to them either way.
 */
function CitationRow({
  citation,
  identity,
  ordinal,
  sessionId,
  turnId,
}: {
  citation: Citation;
  identity: PrincipalIdentity;
  /** 1-based, and positional: it indexes this answer's list, not the corpus. */
  ordinal: number;
  sessionId: string;
  turnId?: string;
}) {
  const [open, setOpen] = useState(false);
  const locator = citationLocator(citation.locator);
  const passage = useQuery({
    // 身份进键，理由同 Work 的产物缓存：QueryClient 是应用级的，切身份不会
    // 重建它，而被引原文是按 staleTime 缓着的。
    queryKey: [
      "chat",
      "citation",
      identity.tenantId,
      identity.principalId,
      [...identity.scopes].sort(),
      sessionId,
      turnId ?? "",
      citation.chunk_id,
    ],
    enabled: open && turnId !== undefined,
    staleTime: Number.POSITIVE_INFINITY,
    retry: false,
    queryFn: () => {
      if (turnId === undefined) throw new Error("这一轮没有可用的 turn id");
      return getCitedPassage(identity, sessionId, turnId, citation.chunk_id);
    },
  });

  // Three states, and the row has to be able to draw all three: read, not yet
  // read, and could not be read. The middle one is the one that was missing --
  // an unopened row looked exactly like a row whose passage happened to be
  // empty, so the click that is the whole point of ADR-067 was undiscoverable.
  // A row that cannot address the route offers no hint at all: 点开取原文 on a
  // chip that will not open is worse than the silence it replaced.
  const failed = open && passage.isError;
  const hint =
    turnId === undefined
      ? null
      : !open
        ? "点开取原文"
        : passage.isSuccess
          ? "刚刚重新读了一次"
          : null;

  return (
    <div className="aw-chat-citation-row">
      {/* Muted once the read refused, so the failure is visible while scanning
          the column of ordinals rather than only in the sentence beside it. */}
      <span
        aria-hidden="true"
        className={
          failed
            ? "aw-chat-citation-ordinal is-unread"
            : "aw-chat-citation-ordinal"
        }
      >
        {ordinal}
      </span>
      <div className="aw-chat-citation-body">
        <button
          aria-expanded={open}
          className="aw-chat-citation"
          // A turn this page never bound an id to cannot address the route, so
          // the row stays inert rather than offering a click that 404s for a
          // reason that has nothing to do with the reader's permissions. The
          // `title` says which of the two it is: a disabled chip with no
          // explanation is indistinguishable from a broken one, and this used
          // to be explained in a paragraph under the whole list instead.
          disabled={turnId === undefined}
          onClick={() => {
            setOpen((was) => !was);
          }}
          title={
            turnId === undefined
              ? "刷新过页面之后，这一轮的引用打不开了"
              : `${citation.chunk_id}\n${citation.document_id} · ${citation.document_version}`
          }
          type="button"
        >
          <strong>{shortId(citation.document_id, 22)}</strong>
          <span className="aw-chat-citation-version">
            {citation.document_version}
          </span>
          {locator === null ? null : (
            <small className="aw-chat-citation-locator">{locator}</small>
          )}
          {hint === null ? null : (
            <span className="aw-chat-citation-hint">{hint}</span>
          )}
        </button>
        {/* The sketch prints the quote on the row. `Citation.quote` is never
            assigned anywhere in this repository, so the only text that exists
            is behind the passage route -- and that route is a fresh read that
            may correctly refuse (ADR-067). Fetching all of them on render
            would spend one read per citation to sometimes produce a column of
            "读不到", so the row carries what is already true and the text
            stays one click away. */}
        {!open ? null : passage.isPending ? (
          <LoadingLine label="正在读取被引用的原文" />
        ) : passage.isError ? (
          // Never "引用坏了", and never a cause. Three things end here -- the
          // grant was withdrawn, the document was re-ingested, the point is no
          // longer in the index -- and naming which one either leaks somebody
          // else's authorization state or sends this reader looking through
          // their own data for a mistake that is not there. So the sentence
          // says what happened and then says, out loud, that it is not
          // distinguishing; the alternative is a reader who assumes it did.
          <p className="aw-chat-citation-unread">
            读不到这段原文了。可能是权限被收回、这一版已经改过，或者这个点不在索引里——这一次没有区分，因为区分它们要么泄漏别人的东西，要么把你支去自己的数据里找一个不存在的错。
          </p>
        ) : (
          // A blockquote, not a paragraph: this is somebody else's words
          // carried into this answer, and the evidence rule is the one that
          // marks the boundary between what the model wrote and what it read.
          <blockquote className="aw-chat-citation-passage">
            {passage.data.text}
          </blockquote>
        )}
      </div>
    </div>
  );
}

/**
 * Where a cited chunk sits, in the only two terms that survive to the client.
 *
 * `paragraph` is the chunk's ordinal inside its document -- `retrieval.py`
 * fills it straight from `chunk.ordinal` -- and not a paragraph number Word or
 * a PDF would recognise. So it reads 片段 #12 and never 第 12 段: the second
 * would be this page inventing a unit the server never counted.
 *
 * `page` is absent for every format without pages, which is most of a Markdown
 * corpus. A citation from one says the fragment alone rather than defaulting to
 * 第 1 页, and a citation with neither position renders no marker at all --
 * an empty locator is not a location.
 */
function citationLocator(locator: SourceLocator): string | null {
  const parts: string[] = [];
  if (locator.page !== undefined && locator.page !== null) {
    parts.push(`第 ${locator.page} 页`);
  }
  if (locator.paragraph !== undefined && locator.paragraph !== null) {
    parts.push(`片段 #${locator.paragraph}`);
  }
  return parts.length === 0 ? null : parts.join(" · ");
}

/**
 * The positions this session's stream said it could not deliver.
 *
 * The same disclosure Work makes under a Task timeline, in the same words: the
 * rows are still in the log, and what failed is decoding them here. Telling a
 * reader their history was destroyed when it was not sends them looking for the
 * wrong thing.
 *
 * Chat can say less than Work, and says less rather than dressing it up. A
 * notice carries no run, so there is no pair of steps to hang it between the
 * way Work anchors each hole -- the position alone is what this page honestly
 * has.
 */
function SessionGapNotice({ sequences }: { sequences: readonly number[] }) {
  if (sequences.length === 0) return null;

  return (
    // The same 820px column the turns use, so the notice lines up with the
    // messages it is about instead of spanning the whole transcript. Reusing
    // that width rather than declaring a second one.
    <div className="aw-chat-turn-list">
      <div className="aw-notice is-warning aw-timeline-gaps">
        <AlertTriangle aria-hidden="true" size={16} />
        <div>
          {/* Scoped to this connection, not to the session. The cursor is
              persisted (`storage.ts`) but these sequences are not -- a reload
              resumes past the hole and this notice disappears while the hole
              stays. Saying "这个会话" would make a per-connection count read as
              a total, which is the shape of claim this notice exists to stop. */}
          <strong>
            这次连接里有 {sequences.length} 个位置没能交给这个页面。
          </strong>
          <small>
            这些事件仍在日志里，只是这次没能解码、没有交给这个页面。通知本身不带
            run， 所以这里只标出位置，说不出它落在哪一轮的哪两步之间。
          </small>
          <ul>
            {sequences.map((sequence) => (
              <li key={sequence}>#{sequence}</li>
            ))}
          </ul>
        </div>
      </div>
    </div>
  );
}

function localSession(session: ChatSessionState): LocalChatSession {
  return {
    sessionId: session.sessionId,
    title: session.title,
    answerMode: session.answerMode,
    knowledgeBaseId: session.knowledgeBaseId,
    createdAt: session.createdAt,
    updatedAt: session.updatedAt,
  };
}
