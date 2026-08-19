import {
  AlertTriangle,
  BookOpen,
  ChevronRight,
  CircleDot,
  Copy,
  MessageSquare,
  PanelLeft,
  Plus,
  RefreshCw,
  RotateCcw,
  Send,
  ShieldAlert,
  ShieldCheck,
  Trash2,
  Wifi,
  WifiOff,
  Wrench,
  X,
} from "lucide-react";
import { useQuery } from "@tanstack/react-query";
import {
  type FormEvent,
  type KeyboardEvent,
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
} from "react";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";
import { createChatSession, getCitedPassage } from "../../api/client";
import type {
  Citation,
  LocalChatSession,
  PrincipalIdentity,
  SourceLocator,
} from "../../api/types";
import { useIdentity } from "../../app/IdentityContext";
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
import { StepStream } from "../../components/StepStream";
import {
  EmptyState,
  ErrorNotice,
  IconButton,
  LoadingLine,
  formatTime,
  shortId,
} from "../../components/ui";
import {
  calledToolNames,
  hasUnfinishedTurn,
  turnToolNames,
  type ChatConnectionState,
  type ChatSessionState,
  type ChatTurnState,
  type LiveTextState,
} from "./model";
import { deriveTurnStages, isTurnMetaActivity } from "./turnStages";
import type { StoredChatCursor } from "./storage";
import { useChatRuntime } from "./useChatRuntime";

export function ChatPage() {
  const { identity } = useIdentity();
  const { runtime, state } = useChatRuntime(identity);
  const { sessionId } = useParams<{ sessionId?: string }>();
  const [searchParams] = useSearchParams();
  const navigate = useNavigate();
  const selected = sessionId === undefined ? undefined : state.sessions[sessionId];
  const [question, setQuestion] = useState("");
  const [sourceDrafts, setSourceDrafts] = useState<Record<string, string | null>>({});
  const [creatingSession, setCreatingSession] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [mobileSessionsOpen, setMobileSessionsOpen] = useState(false);
  const mounted = useRef(true);
  const latestSubmissionContext = useRef({ runtime, sessionId });

  useLayoutEffect(() => {
    mounted.current = true;
    latestSubmissionContext.current = { runtime, sessionId };
    return () => {
      mounted.current = false;
    };
  }, [runtime, sessionId]);

  const removeSession = useCallback(
    async (target: string) => {
      if (!window.confirm("删除这个会话？它的问答记录会一起消失。")) return;
      try {
        await runtime.removeSession(target);
        setMobileSessionsOpen(false);
        if (target === sessionId) await navigate("/chat");
      } catch (cause: unknown) {
        // Reported where the composer's own failures are reported. A delete
        // that the server refused has to say so: the row is still there, and
        // silence would read as a click that did nothing.
        setSubmitError(cause instanceof Error ? cause.message : String(cause));
      }
    },
    [navigate, runtime, sessionId],
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
  const requestedKnowledgeBaseId =
    Object.prototype.hasOwnProperty.call(sourceDrafts, knowledgeBaseDraftKey)
      ? sourceDrafts[knowledgeBaseDraftKey] ?? null
      : selected?.knowledgeBaseId ?? requestedKnowledgeBase;
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
      : (state.turnOrderBySession[selected.sessionId] ?? []).flatMap((turnId) => {
          const turn = state.turns[turnId];
          return turn === undefined ? [] : [turn];
        });
  const quarantined =
    selected === undefined ? [] : state.quarantinedSequences[selected.sessionId] ?? [];
  const unfinished = selected === undefined ? false : hasUnfinishedTurn(state, selected.sessionId);
  const composerDisabled =
    creatingSession ||
    sourceResolving ||
    unfinished ||
    selected?.history === "loading";

  const changeSource = (nextId: string | null) => {
    if (nextId === knowledgeBaseId) return;
    if (
      attachments.items.length > 0 &&
      !window.confirm("切换资料会清空当前待发送的附件，是否继续？")
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
    if (!trimmedQuestion || composerDisabled || attachments.hasBlockingItems) return;
    setSubmitError(null);

    try {
      let target = selected;
      if (target === undefined) {
        setCreatingSession(true);
        const submittedRuntime = runtime;
        const submittedSessionId = sessionId;
        const opened = await createChatSession(identity, trimmedQuestion.slice(0, 200));
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
        const latest = latestSubmissionContext.current;
        if (
          !mounted.current ||
          latest.runtime !== submittedRuntime ||
          latest.sessionId !== submittedSessionId
        ) {
          // The Session already exists, so retain its local handle under the
          // identity that created it. Starting an Ask after the user switched
          // identity or route would execute a new side effect in stale context.
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
      if (selected === undefined && mounted.current) {
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

  const submitOnEnter = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key !== "Enter" || event.shiftKey || event.nativeEvent.isComposing) return;
    event.preventDefault();
    event.currentTarget.form?.requestSubmit();
  };

  return (
    <div className="aw-chat-page">
      {mobileSessionsOpen ? (
        <button
          aria-label="关闭会话列表"
          className="aw-chat-sessions-backdrop"
          onClick={() => setMobileSessionsOpen(false)}
          type="button"
        />
      ) : null}
      <aside
        className={`aw-chat-sessions ${mobileSessionsOpen ? "is-mobile-open" : ""}`}
        aria-label="本地 Chat 会话"
      >
        <header className="aw-chat-sessions-header">
          <div>
            <strong>会话</strong>
            <span className="aw-local-badge">本地</span>
            {/* The badge says "本地"; this says what that costs. Chat has no
                server-side session list, so this column is whatever *this
                browser* wrote down -- a session opened elsewhere is still
                there and still owned, it simply has no entry here. Without
                the sentence, an empty column reads as "you have no
                sessions". */}
            <small>存在这台浏览器里 · 服务端暂无列表</small>
          </div>
          <div className="aw-chat-session-actions">
            <IconButton
              label="新建本地会话"
              onClick={() => {
                setMobileSessionsOpen(false);
                void navigate("/chat");
              }}
            >
              <Plus aria-hidden="true" size={17} />
            </IconButton>
            <IconButton
              className="aw-chat-sessions-close"
              label="关闭会话列表"
              onClick={() => setMobileSessionsOpen(false)}
            >
              <X aria-hidden="true" size={17} />
            </IconButton>
          </div>
        </header>
        <div className="aw-chat-session-list">
          {state.sessionOrder.length === 0 ? (
            <p className="aw-chat-local-note">发送消息后，会话会保存在当前浏览器。</p>
          ) : (
            state.sessionOrder.map((id) => {
              const session = state.sessions[id];
              if (session === undefined) return null;
              return (
                <div className="aw-chat-session-row" key={session.sessionId}>
                  <button
                    aria-current={session.sessionId === sessionId ? "page" : undefined}
                    className={`aw-chat-session ${session.sessionId === sessionId ? "is-active" : ""}`}
                    onClick={() => {
                      setMobileSessionsOpen(false);
                      void navigate(`/chat/${encodeURIComponent(session.sessionId)}`);
                    }}
                    type="button"
                  >
                    <span className="aw-chat-session-copy">
                      <span className="aw-chat-session-head">
                        <strong>{session.title}</strong>
                        {/* The session's answer mode, which is stored per
                            session and therefore true for every row without
                            reading a single history. The sketch tags rows
                            带引用 / 未接地 instead -- both are facts about how
                            an answer turned out, and this list holds no turns
                            for any session but the open one. Tagging by a
                            fact the column cannot check would put "未接地" on
                            rows nothing had looked at. */}
                        <span
                          className={`aw-chat-session-tag is-${
                            session.answerMode === "rag" ? "evidence" : "plain"
                          }`}
                        >
                          {session.answerMode === "rag" ? "知识库" : "自由回答"}
                        </span>
                      </span>
                      <small>
                        本地 · {formatTime(session.updatedAt)} ·{" "}
                        {shortId(session.sessionId)}
                      </small>
                    </span>
                    <ChevronRight aria-hidden="true" size={15} />
                  </button>
                  <button
                    aria-label={`删除会话 ${session.title}`}
                    className="aw-chat-session-delete"
                    onClick={() => void removeSession(session.sessionId)}
                    title="删除"
                    type="button"
                  >
                    <Trash2 aria-hidden size={13} />
                  </button>
                </div>
              );
            })
          )}
        </div>
      </aside>

      <main className="aw-chat-main">
        <ChatHeader
          answerMode={answerMode}
          session={selected}
          {...(selectedKnowledgeBase === undefined
            ? {}
            : { sourceLabel: selectedKnowledgeBase.name })}
          {...(selected === undefined
            ? {}
            : { onReconnect: () => runtime.reconnectSessionStream(selected.sessionId) })}
          onOpenSessions={() => setMobileSessionsOpen(true)}
        />

        <section className="aw-chat-transcript" aria-live="polite">
          {sessionId !== undefined && selected === undefined ? (
            <EmptyState
              icon={<ShieldAlert aria-hidden="true" size={24} />}
              title="这不是当前身份的本地会话"
              description="会话 ID 不是凭证。本页面只打开当前本地身份记录过的入口，服务端仍会独立鉴权。"
              action={
                <button className="aw-button is-primary" onClick={() => navigate("/chat")} type="button">
                  开始新会话
                </button>
              }
            />
          ) : selected === undefined ? (
            <EmptyState
              icon={<MessageSquare aria-hidden="true" size={26} />}
              title="今天想聊什么？"
              description="默认直接对话；需要依据项目资料时，再从输入框下方选择一个知识库。"
            />
          ) : selected.history === "loading" && turns.length === 0 ? (
            <LoadingLine label="正在读取安全会话历史" />
          ) : selected.history === "failed" && turns.length === 0 ? (
            <div className="aw-chat-centered-notice">
              <ErrorNotice message={selected.historyError ?? "无法读取会话历史"} />
              <button className="aw-button is-ghost" onClick={() => void runtime.ensureHistory(selected.sessionId)} type="button">
                重试读取
              </button>
            </div>
          ) : turns.length === 0 ? (
            <EmptyState
              icon={<BookOpen aria-hidden="true" size={25} />}
              title="这个会话还是空的"
              description="可以直接提问，也可以选择知识库后获得带引用的回答。Enter 发送，Shift + Enter 换行。"
            />
          ) : (
            <div className="aw-chat-turn-list">
              {turns.map((turn) => (
                <ChatTurn
                  identity={identity}
                  key={turn.localId}
                  turn={turn}
                  {...(turn.historical ? {} : { onRetry: () => runtime.retryAsk(turn.localId) })}
                />
              ))}
            </div>
          )}
          {/* Under the transcript, not over it: it is a statement about what a
              reader has just scrolled through. */}
          <SessionGapNotice sequences={quarantined} />
        </section>

        <form className="aw-chat-composer" onSubmit={submit}>
          {submitError === null ? null : <ErrorNotice message={submitError} />}
          <AttachmentTray
            items={attachments.items}
            onRemove={attachments.remove}
            onRetry={attachments.retry}
          />
          <div className="aw-chat-composer-row">
            <AttachmentButton
              disabled={composerDisabled || attachments.readOnlyReason !== null}
              {...(attachments.readOnlyReason === null
                ? {}
                : { disabledReason: attachments.readOnlyReason })}
              onFiles={attachments.addFiles}
            />
            <textarea
              aria-label="问题"
              disabled={composerDisabled}
              maxLength={4096}
              onChange={(event) => setQuestion(event.target.value)}
              onKeyDown={submitOnEnter}
              placeholder={
                unfinished
                  ? "请等待当前回答完成…"
                  : answerMode === "direct"
                    ? "输入消息…"
                    : `询问 ${selectedKnowledgeBase?.name ?? "所选知识库"}…`
              }
              rows={1}
              value={question}
            />

            <button
              aria-label="发送问题"
              className="aw-button is-primary aw-chat-send"
              disabled={composerDisabled || !question.trim() || attachments.hasBlockingItems}
              type="submit"
            >
              {creatingSession ? <RefreshCw aria-hidden="true" className="aw-spin" size={17} /> : <Send aria-hidden="true" size={17} />}
            </button>
          </div>
          <div className="aw-chat-composer-tools">
            <KnowledgeSourcePicker
              compact
              disabled={composerDisabled}
              identity={identity}
              onChange={(knowledgeBase) =>
                changeSource(knowledgeBase?.knowledge_base_id ?? null)
              }
              value={knowledgeBaseId}
            />
            <span>
              {/* Said in the line, not only in the button's tooltip: a
                  disabled paperclip explains itself to a mouse and to nobody
                  on a touch screen or a screen reader that never hovers. */}
              {attachments.readOnlyReason !== null
                ? attachments.readOnlyReason
                : attachments.items.some(
                      (item) => item.state === "waiting_for_source",
                    )
                  ? "附件需要先选择知识库"
                  : attachments.hasBlockingItems
                    ? "附件可检索后才能发送"
                    : answerMode === "direct"
                      ? "自由回答不会检索项目资料"
                      : "回答会检索资料并标注引用"}
            </span>
            {/* 4096 这个上限一直在（textarea 上的 maxLength），只是看不见：打到
                头的人得到的是一个不再接受输入的输入框，没有任何东西说明为什么。
                放在这一行而不是压在输入框上：它是关于这次输入的说明，和左边那句
                「回答会检索资料并标注引用」是同一类东西。 */}
            <span
              className={`aw-chat-counter ${question.length >= 4096 ? "is-full" : ""}`}
            >
              {question.length} / 4096
            </span>
          </div>
        </form>
      </main>
    </div>
  );
}

function ChatHeader({
  answerMode,
  session,
  sourceLabel,
  onReconnect,
  onOpenSessions,
}: {
  answerMode: "direct" | "rag";
  session: ChatSessionState | undefined;
  sourceLabel?: string;
  onReconnect?: () => void;
  onOpenSessions: () => void;
}) {
  return (
    <header className="aw-chat-header">
      <IconButton
        className="aw-chat-mobile-sessions"
        label="打开会话列表"
        onClick={onOpenSessions}
      >
        <PanelLeft aria-hidden="true" size={18} />
      </IconButton>
      <div>
        <p className="aw-eyebrow">Chat</p>
        <h1>{session?.title ?? "新会话"}</h1>
        <p>
          {session === undefined
            ? "直接对话，或选择知识库。"
            : answerMode === "direct"
              ? "当前：自由回答 · 可随时切换知识库"
              : `当前资料：${sourceLabel ?? "知识库"}`}
        </p>
      </div>
      {session === undefined ? null : (
        <ConnectionBadge
          connection={session.connection}
          {...(session.cursor === undefined ? {} : { cursor: session.cursor })}
          {...(onReconnect === undefined ? {} : { onReconnect })}
        />
      )}
    </header>
  );
}

function ConnectionBadge({
  connection,
  cursor,
  onReconnect,
}: {
  connection: ChatConnectionState;
  /** Absent until the stream has delivered a frame. */
  cursor?: StoredChatCursor;
  onReconnect?: () => void;
}) {
  const connected = connection === "connected";
  const label =
    {
      idle: "未订阅",
      connecting: "连接事件流",
      connected: "事件流已连接",
      retrying: "正在断点重连",
      unavailable: "事件流不可用",
    }[connection] ?? connection;
  return (
    <button
      className={`aw-chat-connection ${connected ? "is-connected" : ""}`}
      disabled={onReconnect === undefined || connected || connection === "connecting"}
      onClick={onReconnect}
      title={connected ? "SSE 使用当前本地身份 Header" : "点击重新连接"}
      type="button"
    >
      {connected ? <Wifi aria-hidden="true" size={15} /> : <WifiOff aria-hidden="true" size={15} />}
      {label}
      {/* 流走到哪了。这条流是可断点续传的，「已连接」只说明这一刻通着，说不出
          重连之后会从哪一条接上——游标说得出。没有游标时整段不画，而不是画一个
          占位符：一条还没送来任何帧的流没有位置，写 #0 是在报一个不存在的位置。 */}
      {cursor === undefined ? null : (
        <span className="aw-chat-cursor">游标 #{cursor.id}</span>
      )}
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
        <header>
          <span className="aw-chat-assistant-mark" aria-hidden="true">A</span>
          <strong>Agent Workbench</strong>
          {turn.historical ? (
            <small>历史记录</small>
          ) : (
            <small>{turn.answerMode === "direct" ? "自由回答" : "知识库回答"}</small>
          )}
        </header>
        {turn.activities.length === 0 ? null : <TurnStepStream turn={turn} />}
        {turn.phase === "withheld" ? (
          <div className="aw-notice is-warning">
            <AlertTriangle aria-hidden="true" size={16} />
            <span>
              最终来源复核未通过；下面仅显示服务端发布的安全替代文本，不含候选答案。
            </span>
          </div>
        ) : null}
        {turn.answer === undefined ? null : <MarkdownContent text={turn.answer} />}
        {turn.stream === undefined ? null : <LiveText stream={turn.stream} />}
        {!turn.historical && (turn.phase === "submitting" || turn.phase === "running") ? (
          <LoadingLine
            label={
              turn.phase === "submitting"
                ? "正在提交 Turn"
                : turn.stream !== undefined && !turn.stream.redacted
                  ? "正在生成，尚未发布"
                  : "等待安全发布边界"
            }
          />
        ) : null}
        {turn.historical && turn.answer === undefined ? (
          <p className="aw-chat-no-citations">历史仅包含用户消息；服务端没有发布 assistant 消息。</p>
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
            <ErrorNotice message={turn.error ?? "这个 Turn 未能完成"} />
            {onRetry === undefined ? null : (
              <button className="aw-button is-ghost" onClick={onRetry} type="button">
                <RotateCcw aria-hidden="true" size={15} />
                使用原幂等键重试
              </button>
            )}
          </div>
        ) : null}
        {!turn.historical && (turn.phase === "committed" || turn.phase === "withheld") ? (
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
    <div aria-live="polite" className="aw-chat-live-text">
      <p>{stream.text}</p>
      <small>
        <CircleDot aria-hidden="true" size={12} />
        正在生成的过程文本，尚未通过发布边界
        {stream.dropped > 0 ? `（有 ${String(stream.dropped)} 段未能送达）` : ""}
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
      <TurnTools turn={turn} />
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
          <ChevronRight aria-hidden="true" className="aw-step-caret" size={13} />
          <span className="aw-turn-process-label">过程</span>
          {stages.map((stage) => (
            <span className={`aw-turn-phase is-${stage.state}`} key={stage.id}>
              {stage.title}
            </span>
          ))}
          <span className="aw-turn-process-count">
            {stages.length} 个阶段 · {turn.activities.length} 条事件
          </span>
        </summary>
      <StepStream
        ariaLabel="回答过程"
        // The label the turn already computed. It carries what the event meant
        // in Chat's own vocabulary -- "答案已发布（未经证据核实）" is a distinction
        // Work's generic titles do not draw.
        eventTitle={(event) =>
          turn.activities.find((activity) => activity.envelope === event)?.label ??
          event.event_type
        }
        meta={{ title: "运行记录", events: meta.map((activity) => activity.envelope) }}
        running={running}
        stages={stages}
      />
      </details>
    </>
  );
}

/**
 * What this turn could reach, and what it actually reached.
 *
 * The names were always on the wire -- `RunStarted.tool_names` carries them,
 * and the step detail has rendered them since it was written. What was missing
 * is that `RunStarted` is run bookkeeping, so it lives in the collapsed
 * "运行记录" group at the bottom: the answer to "什么工具可用" sat three
 * disclosures deep, under a heading that promises the opposite of a capability
 * list. Reading it off the same event and putting it at the top costs nothing
 * and is the first thing a reader asks of an agent.
 *
 * A turn with no tools renders nothing rather than "可用工具：无". The direct
 * and fixed shapes are toolless by construction, and a row that says so on
 * every message is noise that teaches the reader to stop looking.
 */
function TurnTools({ turn }: { turn: ChatTurnState }) {
  const available = turnToolNames(turn.activities);
  if (available.length === 0) return null;
  const called = new Set(calledToolNames(turn.activities));

  return (
    <div className="aw-turn-tools" aria-label="本轮可用工具">
      <Wrench aria-hidden="true" size={13} />
      {available.map((name) => (
        <span
          className={`aw-turn-tool ${called.has(name) ? "is-called" : ""}`}
          key={name}
          title={called.has(name) ? `${name}（本轮已调用）` : `${name}（本轮未调用）`}
        >
          {name}
        </span>
      ))}
    </div>
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
      <p className="aw-chat-no-citations aw-chat-ungrounded">
        <ShieldAlert aria-hidden="true" size={14} />
        {answerMode === "rag"
          ? "已检索所选知识库，但没有找到足够相关的内容；这条回答由模型直接作答，没有引用"
          : "未经证据核实：本条回答由模型直接作答，未检索知识库，因此没有引用"}
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
        已接地 · {citations.length} 条引用
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
      {/* The design sketch shows a document *title* on each row. There is no
          route that turns a document_id into one, and inventing a readable
          name from the id would be this page asserting something no endpoint
          established -- so the row shows the id, and this line says why that
          is what it shows. */}
      <p className="aw-chat-citation-gap">
        <span>需新接口</span>
        文档标题需要一个按 document_id 取名的读接口；当前只有 id 与 version 可信。
      </p>
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
    queryKey: ["chat", "citation", sessionId, turnId ?? "", citation.chunk_id],
    enabled: open && turnId !== undefined,
    staleTime: Number.POSITIVE_INFINITY,
    retry: false,
    queryFn: () => {
      if (turnId === undefined) throw new Error("这一轮没有可用的 turn id");
      return getCitedPassage(identity, sessionId, turnId, citation.chunk_id);
    },
  });

  return (
    <div className="aw-chat-citation-row">
      <span aria-hidden="true" className="aw-chat-citation-ordinal">
        {ordinal}
      </span>
      <div className="aw-chat-citation-body">
        <button
          aria-expanded={open}
          className="aw-chat-citation"
          // A turn this page never bound an id to cannot address the route, so
          // the row stays inert rather than offering a click that 404s for a
          // reason that has nothing to do with the reader's permissions.
          disabled={turnId === undefined}
          onClick={() => {
            setOpen((was) => !was);
          }}
          title={`${citation.chunk_id}\n${citation.document_id} · ${citation.document_version}`}
          type="button"
        >
          <strong>{shortId(citation.document_id, 22)}</strong>
          <span className="aw-chat-citation-version">
            {citation.document_version}
          </span>
          {locator === null ? null : (
            <small className="aw-chat-citation-locator">{locator}</small>
          )}
        </button>
        {/* The sketch prints the quote on the row. `Citation.quote` is never
            assigned anywhere in this repository, so the only text that exists
            is behind the passage route -- and that route is a fresh read that
            may correctly refuse (ADR-067). Fetching all of them on render
            would spend one read per citation to sometimes produce a column of
            "读不到", so the row carries what is already true and the text
            stays one click away. */}
        {!open ? null : (
          <div className="aw-chat-citation-passage">
            {passage.isPending ? (
              <LoadingLine label="正在读取被引用的原文" />
            ) : passage.isError ? (
              // Never "引用坏了". The commonest cause is that this reader may no
              // longer read the document, which is a decision somebody made
              // rather than a fault in the transcript.
              <p className="aw-page-note">
                读不到这段原文了：可能是这份文档的权限变了，或者它已经被重新导入过。
              </p>
            ) : (
              <p>{passage.data.text}</p>
            )}
          </div>
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
          <strong>这次连接里有 {sequences.length} 个位置没能交给这个页面。</strong>
          <small>
            这些事件仍在日志里，只是这次没能解码、没有交给这个页面。通知本身不带 run，
            所以这里只标出位置，说不出它落在哪一轮的哪两步之间。
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
