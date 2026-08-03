import {
  AlertTriangle,
  BookOpen,
  ChevronRight,
  CircleDot,
  MessageSquare,
  Plus,
  RefreshCw,
  RotateCcw,
  Send,
  ShieldAlert,
  Wifi,
  WifiOff,
} from "lucide-react";
import {
  type FormEvent,
  type KeyboardEvent,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
} from "react";
import { useNavigate, useParams } from "react-router-dom";
import { createChatSession } from "../../api/client";
import type { Citation, LocalChatSession } from "../../api/types";
import { useIdentity } from "../../app/IdentityContext";
import { MarkdownContent } from "../../components/MarkdownContent";
import {
  EmptyState,
  ErrorNotice,
  IconButton,
  LoadingLine,
  StepState,
  formatTime,
  shortId,
} from "../../components/ui";
import {
  hasUnfinishedTurn,
  type ChatActivity,
  type ChatConnectionState,
  type ChatSessionState,
  type ChatTurnState,
} from "./model";
import { useChatRuntime } from "./useChatRuntime";

const DEFAULT_KNOWLEDGE_BASE = "kb_local";

export function ChatPage() {
  const { identity } = useIdentity();
  const { runtime, state } = useChatRuntime(identity);
  const { sessionId } = useParams<{ sessionId?: string }>();
  const navigate = useNavigate();
  const selected = sessionId === undefined ? undefined : state.sessions[sessionId];
  const [question, setQuestion] = useState("");
  const [knowledgeBaseDrafts, setKnowledgeBaseDrafts] = useState<Record<string, string>>({});
  const [creatingSession, setCreatingSession] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const mounted = useRef(true);
  const latestSubmissionContext = useRef({ runtime, sessionId });

  useLayoutEffect(() => {
    mounted.current = true;
    latestSubmissionContext.current = { runtime, sessionId };
    return () => {
      mounted.current = false;
    };
  }, [runtime, sessionId]);

  const selectedSessionId = selected?.sessionId;
  useEffect(() => {
    if (selectedSessionId === undefined) return;
    const release = runtime.retainSessionStream(selectedSessionId);
    void runtime.ensureHistory(selectedSessionId);
    return release;
  }, [runtime, selectedSessionId]);

  const knowledgeBaseDraftKey = selectedSessionId ?? "new";
  const knowledgeBaseId =
    knowledgeBaseDrafts[knowledgeBaseDraftKey] ??
    selected?.knowledgeBaseId ??
    DEFAULT_KNOWLEDGE_BASE;
  const turns =
    selected === undefined
      ? []
      : (state.turnOrderBySession[selected.sessionId] ?? []).flatMap((turnId) => {
          const turn = state.turns[turnId];
          return turn === undefined ? [] : [turn];
        });
  const unfinished = selected === undefined ? false : hasUnfinishedTurn(state, selected.sessionId);
  const composerDisabled =
    creatingSession || unfinished || selected?.history === "loading";

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const trimmedQuestion = question.trim();
    const trimmedKnowledgeBase = knowledgeBaseId.trim();
    if (!trimmedQuestion || !trimmedKnowledgeBase || composerDisabled) return;
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
          knowledgeBaseId: trimmedKnowledgeBase,
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
        knowledgeBaseId: trimmedKnowledgeBase,
      });
      setQuestion("");
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
      <aside className="aw-chat-sessions" aria-label="本地 Chat 会话">
        <header className="aw-chat-sessions-header">
          <div>
            <strong>会话</strong>
            <span className="aw-local-badge">本地列表</span>
          </div>
          <IconButton label="新建本地会话" onClick={() => navigate("/chat")}>
            <Plus aria-hidden="true" size={17} />
          </IconButton>
        </header>
        <div className="aw-chat-session-list">
          {state.sessionOrder.length === 0 ? (
            <p className="aw-chat-local-note">尚无本地会话。发送第一条问题后才会创建。</p>
          ) : (
            state.sessionOrder.map((id) => {
              const session = state.sessions[id];
              if (session === undefined) return null;
              return (
                <button
                  aria-current={session.sessionId === sessionId ? "page" : undefined}
                  className={`aw-chat-session ${session.sessionId === sessionId ? "is-active" : ""}`}
                  key={session.sessionId}
                  onClick={() => navigate(`/chat/${encodeURIComponent(session.sessionId)}`)}
                  type="button"
                >
                  <span className="aw-chat-session-copy">
                    <strong>{session.title}</strong>
                    <small>
                      本地 · {formatTime(session.updatedAt)} · {shortId(session.sessionId)}
                    </small>
                  </span>
                  <ChevronRight aria-hidden="true" size={15} />
                </button>
              );
            })
          )}
        </div>
        <p className="aw-chat-local-note">
          这里只保存当前浏览器、当前本地身份见过的入口；服务端没有会话列举接口。
        </p>
      </aside>

      <main className="aw-chat-main">
        <ChatHeader
          session={selected}
          {...(selected === undefined
            ? {}
            : { onReconnect: () => runtime.reconnectSessionStream(selected.sessionId) })}
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
              title="用可核验的资料开始对话"
              description="答案只会在最终证据与权限复核通过后发布；被阻止的候选文本不会进入页面。"
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
              title="这个本地会话还是空的"
              description="输入知识库 ID 和问题；Enter 发送，Shift + Enter 换行。"
            />
          ) : (
            <div className="aw-chat-turn-list">
              {turns.map((turn) => (
                <ChatTurn
                  key={turn.localId}
                  turn={turn}
                  {...(turn.historical ? {} : { onRetry: () => runtime.retryAsk(turn.localId) })}
                />
              ))}
            </div>
          )}
        </section>

        <form className="aw-chat-composer" onSubmit={submit}>
          {submitError === null ? null : <ErrorNotice message={submitError} />}
          <div className="aw-chat-composer-row">
            <label className="aw-chat-kb-field">
              <span>知识库</span>
              <input
                aria-label="知识库 ID"
                disabled={composerDisabled}
                onChange={(event) =>
                  setKnowledgeBaseDrafts((current) => ({
                    ...current,
                    [knowledgeBaseDraftKey]: event.target.value,
                  }))
                }
                required
                value={knowledgeBaseId}
              />
            </label>
            <textarea
              aria-label="问题"
              disabled={composerDisabled}
              maxLength={4096}
              onChange={(event) => setQuestion(event.target.value)}
              onKeyDown={submitOnEnter}
              placeholder={unfinished ? "请等待当前问题安全终结…" : "询问已索引的知识…"}
              rows={1}
              value={question}
            />
            <button
              aria-label="发送问题"
              className="aw-button is-primary aw-chat-send"
              disabled={composerDisabled || !question.trim() || !knowledgeBaseId.trim()}
              type="submit"
            >
              {creatingSession ? <RefreshCw aria-hidden="true" className="aw-spin" size={17} /> : <Send aria-hidden="true" size={17} />}
            </button>
          </div>
          <p>
            {selected === undefined
              ? "发送时才创建服务端 Session；Session 创建本身没有幂等键。"
              : "同一会话一次只运行一个 Turn；失败重试会复用原 Idempotency-Key。"}
          </p>
        </form>
      </main>
    </div>
  );
}

function ChatHeader({
  session,
  onReconnect,
}: {
  session: ChatSessionState | undefined;
  onReconnect?: () => void;
}) {
  return (
    <header className="aw-chat-header">
      <div>
        <p className="aw-eyebrow">Chat + RAG</p>
        <h1>{session?.title ?? "新会话"}</h1>
        <p>
          {session === undefined
            ? "先选择知识库，再发送一个有证据边界的问题。"
            : `本地会话 · ${shortId(session.sessionId)} · ${session.knowledgeBaseId}`}
        </p>
      </div>
      {session === undefined ? null : (
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
    </button>
  );
}

function ChatTurn({ turn, onRetry }: { turn: ChatTurnState; onRetry?: () => void }) {
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
          {turn.historical ? <small>安全历史投影</small> : <small>{turn.phase}</small>}
        </header>
        {turn.activities.length === 0 ? null : <ActivityList activities={turn.activities} />}
        {turn.phase === "withheld" ? (
          <div className="aw-notice is-warning">
            <AlertTriangle aria-hidden="true" size={16} />
            <span>
              最终来源复核未通过；下面仅显示服务端发布的安全替代文本，不含候选答案。
            </span>
          </div>
        ) : null}
        {turn.answer === undefined ? null : <MarkdownContent text={turn.answer} />}
        {!turn.historical && (turn.phase === "submitting" || turn.phase === "running") ? (
          <LoadingLine label={turn.phase === "submitting" ? "正在提交 Turn" : "等待安全发布边界"} />
        ) : null}
        {turn.historical && turn.answer === undefined ? (
          <p className="aw-chat-no-citations">历史仅包含用户消息；服务端没有发布 assistant 消息。</p>
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
        {turn.phase === "committed" || turn.phase === "withheld" ? (
          <Citations citations={turn.citations} withheld={turn.phase === "withheld"} />
        ) : null}
      </div>
    </article>
  );
}

function ActivityList({ activities }: { activities: ChatActivity[] }) {
  return (
    <ol className="aw-chat-activity" aria-label="Turn durable events">
      {activities.map((activity) => (
        <li key={activity.key}>
          <span className={`aw-chat-activity-state is-${activity.state}`}>
            <StepState state={stepState(activity.state)} />
          </span>
          <span>
            <strong>{activity.label}</strong>
            {activity.detail === undefined ? null : <small>{activity.detail}</small>}
          </span>
          <time dateTime={activity.timestamp}>{formatTime(activity.timestamp)}</time>
        </li>
      ))}
    </ol>
  );
}

function Citations({ citations, withheld }: { citations: Citation[]; withheld: boolean }) {
  if (withheld) {
    return (
      <p className="aw-chat-no-citations">
        <ShieldAlert aria-hidden="true" size={14} />
        被阻止的答案不发布引用
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
  return (
    <div className="aw-chat-citations" aria-label="引用">
      {citations.map((citation) => (
        <span
          className="aw-chat-citation"
          key={`${citation.chunk_id}:${citation.document_version}`}
          title={`${citation.document_id} · ${citation.document_version}${citation.quote ? `\n\n${citation.quote}` : ""}`}
        >
          [{shortId(citation.chunk_id, 16)}]
        </span>
      ))}
    </div>
  );
}

function stepState(state: ChatActivity["state"]): "complete" | "active" | "waiting" | "failed" {
  if (state === "complete") return "complete";
  if (state === "failed") return "failed";
  if (state === "running") return "active";
  return "waiting";
}

function localSession(session: ChatSessionState): LocalChatSession {
  return {
    sessionId: session.sessionId,
    title: session.title,
    knowledgeBaseId: session.knowledgeBaseId,
    createdAt: session.createdAt,
    updatedAt: session.updatedAt,
  };
}
