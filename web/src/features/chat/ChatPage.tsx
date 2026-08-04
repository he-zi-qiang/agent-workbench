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
import { useNavigate, useParams, useSearchParams } from "react-router-dom";
import { createChatSession } from "../../api/client";
import type { Citation, LocalChatSession } from "../../api/types";
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
import { StepDisclosure } from "../../components/StepDisclosure";
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
          answerMode={answerMode}
          session={selected}
          {...(selectedKnowledgeBase === undefined
            ? {}
            : { sourceLabel: selectedKnowledgeBase.name })}
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
          <AttachmentTray
            items={attachments.items}
            onRemove={attachments.remove}
            onRetry={attachments.retry}
          />
          <div className="aw-chat-composer-row">
            <AttachmentButton
              disabled={composerDisabled}
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
              {attachments.items.some(
                (item) => item.state === "waiting_for_source",
              )
                ? "附件需要先选择知识库"
                : attachments.hasBlockingItems
                  ? "附件可检索后才能发送"
                  : answerMode === "direct"
                    ? "自由回答不会检索项目资料"
                    : "回答会检索资料并标注引用"}
            </span>
          </div>
          <p>
            {selected === undefined
              ? "发送第一条消息时创建会话。附件会先加入所选知识库并等待索引。"
              : "同一会话可在自由回答与知识库回答之间逐条切换。"}
          </p>
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
}: {
  answerMode: "direct" | "rag";
  session: ChatSessionState | undefined;
  sourceLabel?: string;
  onReconnect?: () => void;
}) {
  return (
    <header className="aw-chat-header">
      <div>
        <p className="aw-eyebrow">Chat</p>
        <h1>{session?.title ?? "新会话"}</h1>
        <p>
          {session === undefined
            ? "自由对话与知识库问答共用一个输入框。"
            : answerMode === "direct"
              ? "当前：自由回答 · 可随时切换知识库"
              : `当前资料：${sourceLabel ?? "知识库"}`}
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
          {turn.historical ? (
            <small>历史记录</small>
          ) : (
            <small>{turn.answerMode === "direct" ? "自由回答" : "知识库回答"}</small>
          )}
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
          <Citations
            answerMode={turn.answerMode}
            citations={turn.citations}
            withheld={turn.phase === "withheld"}
            grounded={turn.grounded !== false}
          />
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
          {/* Openable for the same reason Work's timeline is: the label says a
              model call finished, and the thing the reader wants is what it
              was given and what it wrote. The one-line summary comes from the
              event itself, so `activity.detail` is not repeated here. */}
          <StepDisclosure event={activity.envelope} title={activity.label} />
        </li>
      ))}
    </ol>
  );
}

function Citations({
  answerMode,
  citations,
  withheld,
  grounded,
}: {
  answerMode: "direct" | "rag";
  citations: Citation[];
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
    answerMode: session.answerMode,
    knowledgeBaseId: session.knowledgeBaseId,
    createdAt: session.createdAt,
    updatedAt: session.updatedAt,
  };
}
