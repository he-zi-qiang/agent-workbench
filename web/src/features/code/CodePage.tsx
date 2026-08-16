/**
 * A coding session, in the browser.
 *
 * Three panes because a coding session answers three different questions at
 * three different rates. The transcript grows once per turn. The working set
 * changes with every write *inside* a turn, and it is the actual product --
 * before it had a pane, the only way to see a file was to spend a turn asking
 * the agent to read it back. The step list changes several times a second
 * while a turn runs, and exists because a turn holds its request open for
 * minutes: a spinner for all of that cannot be told apart from a hang.
 *
 * What this page still does not have is `ConversationShell` and `StepStream`,
 * which is where the other two flows will put their equivalents. Those arrive
 * with A6; this page joins them then. Until it does, the steps are rendered as
 * a plain list of what happened rather than as a half-built stage view.
 */

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Code2 } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import {
  askCode,
  createCodeSession,
  decideCodeApproval,
  downloadCodeWorkspaceFile,
  getCodeApprovals,
  getCodeHistory,
  getCodeWorkspace,
  getCodeWorkspaceFileText,
  listCodeSessions,
  newIdempotencyKey,
  renameCodeSession,
} from "../../api/client";
import type {
  ApprovalDecision,
  MessageView,
  PendingApprovalView,
  WorkspaceEntryView,
} from "../../api/types";
import { useIdentity } from "../../app/IdentityContext";
import { isReadableMedia } from "../../components/media";
import {
  EmptyState,
  ErrorNotice,
  LoadingLine,
  shortId,
} from "../../components/ui";
import { groupSteps } from "../../components/stepGroups";
import { eventTitle } from "../work/workTimeline";
import { useCodeStream } from "./useCodeStream";

/** How often to ask what the agent is stopped on, while it is working. */
const APPROVAL_POLL_MS = 1000;

/** The three answers, and the one that is not always offered. */
const DECISIONS: { decision: ApprovalDecision; label: string }[] = [
  { decision: "approve_once", label: "允许一次" },
  { decision: "approve_for_session", label: "本会话都允许" },
  { decision: "deny", label: "拒绝" },
];

/** Risks whose second occurrence deserves the same question as their first. */
const UNREPEATABLE = new Set(["external", "destructive"]);

export function CodePage() {
  const { identity } = useIdentity();
  const navigate = useNavigate();
  const { sessionId } = useParams<{ sessionId: string }>();

  const [loadedMessages, setMessages] = useState<MessageView[]>([]);
  const [loadedFiles, setFiles] = useState<WorkspaceEntryView[]>([]);
  //: Which session the two above were loaded for. Without it the page had to
  //: empty them from an effect when the session changed, which is a render too
  //: late -- the previous session's transcript was on screen for a frame, and
  //: for the whole of the next session's fetch.
  const [loadedFor, setLoadedFor] = useState<string | null>(null);
  const [instruction, setInstruction] = useState("");
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [approvals, setApprovals] = useState<PendingApprovalView[]>([]);
  const [opening, setOpening] = useState(false);
  const [opened, setOpened] = useState<OpenedFile | null>(null);
  const [renaming, setRenaming] = useState<string | null>(null);
  const queries = useQueryClient();

  // A query rather than an effect, because two things invalidate it -- opening
  // a session and renaming one -- and both happen somewhere other than where
  // the list is rendered.
  const sessions = useQuery({
    queryKey: ["code-sessions", identity],
    queryFn: () => listCodeSessions(identity),
  });
  const known = sessions.data?.sessions ?? [];

  const steps = useCodeStream(identity, sessionId, running);

  // Derived, not reset. Both of these used to be cleared from an effect when
  // their subject changed, which is a render behind: the old session's file and
  // the finished turn's approvals were on screen for a frame first. The file
  // already carries the session it belongs to, and the approvals are only
  // meaningful while a turn runs, so both questions are answerable here.
  const viewing = opened?.sessionId === sessionId ? opened : null;
  const pending = running ? approvals : [];
  const messages = loadedFor === sessionId ? loadedMessages : [];
  const files = loadedFor === sessionId ? loadedFiles : [];

  const loadHistory = useCallback(
    async (id: string, signal?: AbortSignal) => {
      const history = await getCodeHistory(identity, id, signal);
      setMessages(history.messages);
      setLoadedFor(id);
    },
    [identity],
  );

  const loadWorkspace = useCallback(
    async (id: string, signal?: AbortSignal) => {
      const workspace = await getCodeWorkspace(identity, id, signal);
      setFiles(workspace.files);
      setLoadedFor(id);
    },
    [identity],
  );

  useEffect(() => {
    if (sessionId === undefined) return;
    const controller = new AbortController();
    Promise.all([
      loadHistory(sessionId, controller.signal),
      loadWorkspace(sessionId, controller.signal),
    ]).catch((cause: unknown) => {
      if (controller.signal.aborted) return;
      setError(describe(cause));
    });
    return () => {
      controller.abort();
    };
  }, [loadHistory, loadWorkspace, sessionId]);

  // Only while a turn is running. A poll that kept going would ask a question
  // nobody is waiting on the answer to, once a second, forever.
  const pollingSession = running ? sessionId : undefined;
  useEffect(() => {
    if (pollingSession === undefined) return;
    const controller = new AbortController();
    const tick = () => {
      getCodeApprovals(identity, pollingSession, controller.signal)
        .then((pending) => {
          setApprovals(pending.approvals);
        })
        .catch(() => {
          // A failed poll is not a failed turn. The turn's own request is what
          // reports an error; this one just tries again.
        });
    };
    tick();
    const timer = window.setInterval(tick, APPROVAL_POLL_MS);
    return () => {
      controller.abort();
      window.clearInterval(timer);
    };
  }, [identity, pollingSession]);

  const openSession = useCallback(async () => {
    setOpening(true);
    setError(null);
    try {
      const created = await createCodeSession(identity);
      await queries.invalidateQueries({ queryKey: ["code-sessions", identity] });
      await navigate(`/code/${created.session_id}`);
    } catch (cause: unknown) {
      setError(describe(cause));
    } finally {
      setOpening(false);
    }
  }, [identity, navigate, queries]);

  const send = useCallback(async () => {
    const text = instruction.trim();
    if (text === "" || sessionId === undefined || running) return;
    setRunning(true);
    setError(null);
    // Shown immediately, because the turn holds the connection for as long as
    // the agent works and an input that emptied into nothing reads as a
    // dropped message.
    setMessages((current) => [...current, { role: "user", text }]);
    setInstruction("");
    try {
      await askCode(identity, sessionId, text, newIdempotencyKey("code"));
    } catch (cause: unknown) {
      setError(describe(cause));
    } finally {
      setRunning(false);
      // Both, and on every path. Re-reading rather than trusting the
      // optimistic append is what keeps this transcript from disagreeing with
      // the server's; and a refused turn may still have written files, because
      // the workspace pointer moves per write rather than at the end.
      if (sessionId !== undefined) {
        await Promise.all([
          loadHistory(sessionId).catch(() => undefined),
          loadWorkspace(sessionId).catch(() => undefined),
          // The first instruction is what names the session, and every
          // instruction moves it to the top of the list.
          queries.invalidateQueries({ queryKey: ["code-sessions", identity] }),
        ]);
      }
    }
  }, [
    identity,
    instruction,
    loadHistory,
    loadWorkspace,
    queries,
    running,
    sessionId,
  ]);

  const open = useCallback(
    async (file: WorkspaceEntryView) => {
      if (sessionId === undefined) return;
      const readable = isReadableMedia(file.media_type);
      // Shown before the fetch resolves, so a large file does not look like a
      // click that did nothing. A type that cannot be shown skips the fetch
      // entirely rather than downloading bytes to decide not to render them.
      setOpened({
        sessionId,
        name: file.name,
        loading: readable,
        text: null,
        truncated: false,
      });
      if (!readable) return;
      try {
        const body = await getCodeWorkspaceFileText(identity, sessionId, file.name);
        setOpened((current) =>
          // A second click while the first read was in flight wins. Without
          // this the slower fetch would land last and show the wrong file.
          current?.name === file.name && current.sessionId === sessionId
            ? { ...current, loading: false, ...body }
            : current,
        );
      } catch (cause: unknown) {
        setOpened(null);
        setError(describe(cause));
      }
    },
    [identity, sessionId],
  );

  const decide = useCallback(
    async (approvalId: string, decision: ApprovalDecision) => {
      if (sessionId === undefined) return;
      try {
        await decideCodeApproval(identity, sessionId, approvalId, decision);
        setApprovals((current) =>
          current.filter((held) => held.approval_id !== approvalId),
        );
      } catch (cause: unknown) {
        setError(describe(cause));
      }
    },
    [identity, sessionId],
  );

  const rename = useCallback(
    async (target: string, title: string) => {
      const trimmed = title.trim();
      setRenaming(null);
      if (trimmed === "") return;
      try {
        await renameCodeSession(identity, target, trimmed);
        await queries.invalidateQueries({ queryKey: ["code-sessions", identity] });
      } catch (cause: unknown) {
        setError(describe(cause));
      }
    },
    [identity, queries],
  );

  const sessionList =
    known.length === 0 ? null : (
      <nav aria-label="最近的编码会话" className="aw-code-recent">
        <h2>最近</h2>
        <ul>
          {known.map((held) => (
            <li key={held.session_id}>
              {renaming === held.session_id ? (
                <form
                  onSubmit={(event) => {
                    event.preventDefault();
                    const field = new FormData(event.currentTarget).get("title");
                    // A FormData entry is a string *or a File*, and `String(File)`
                    // is "[object File]" -- a name nobody typed.
                    void rename(
                      held.session_id,
                      typeof field === "string" ? field : "",
                    );
                  }}
                >
                  <label className="aw-sr-only" htmlFor="aw-code-rename">
                    会话名字
                  </label>
                  <input
                    autoFocus
                    defaultValue={held.title ?? ""}
                    id="aw-code-rename"
                    name="title"
                    onBlur={() => {
                      setRenaming(null);
                    }}
                  />
                </form>
              ) : (
                <button
                  aria-current={held.session_id === sessionId ? "page" : undefined}
                  className="aw-code-recent-link"
                  onClick={() => {
                    void navigate(`/code/${held.session_id}`);
                  }}
                  onDoubleClick={() => {
                    setRenaming(held.session_id);
                  }}
                  // Named after the first instruction, so most rows have one.
                  // The id is the fallback for a session opened and never used.
                  title={held.title ?? held.session_id}
                  type="button"
                >
                  {held.title ?? shortId(held.session_id)}
                </button>
              )}
            </li>
          ))}
        </ul>
      </nav>
    );

  if (sessionId === undefined) {
    return (
      <div className="aw-code-page is-empty">
        <main className="aw-code-main">
          <section className="aw-code-transcript">
            <EmptyState
              icon={<Code2 aria-hidden />}
              title="还没有编码会话"
              description="打开一个会话，然后用一句话描述你要做的事。"
              action={
                <button
                  className="aw-button is-primary"
                  disabled={opening}
                  onClick={() => void openSession()}
                  type="button"
                >
                  {opening ? "正在打开" : "新建编码会话"}
                </button>
              }
            />
            {sessionList}
          </section>
          {error === null ? null : <ErrorNotice message={error} />}
        </main>
      </div>
    );
  }

  return (
    <div className="aw-code-page">
      <main className="aw-code-main">
        <section aria-label="编码会话" aria-live="polite" className="aw-code-transcript">
          {messages.length === 0 ? (
            <EmptyState
              icon={<Code2 aria-hidden />}
              title="这个会话还是空的"
              description="描述你要做的事，比如「把 notes.md 里的待办整理成清单」。"
            />
          ) : (
            <ol className="aw-code-turns">
              {messages.map((message, index) => (
                <li
                  className={
                    message.role === "user" ? "aw-code-said" : "aw-code-report"
                  }
                  key={`${message.role}-${String(index)}`}
                >
                  <h3>{message.role === "user" ? "你" : "报告"}</h3>
                  <p>{message.text}</p>
                </li>
              ))}
            </ol>
          )}

          {running ? (
            <section aria-label="正在进行的步骤" className="aw-code-steps">
              <LoadingLine label="正在处理" />
              {/* One line per action, not per event.

                  A turn with three tool calls emits about twenty durable events
                  -- model started, model completed, proposed, permission
                  resolved, started, completed, over and over -- and rendering
                  them one to a row makes a reader reconstruct "it wrote two
                  files" out of the log's own vocabulary. `groupSteps` already
                  folds that for Work and Chat and argues for itself at length;
                  this page was the one that had not adopted it. */}
              <ol>
                {groupSteps(steps, eventTitle).map((step) => (
                  <li className={`is-${step.outcome}`} key={step.key}>
                    <span className="aw-code-step-title">{step.title}</span>
                    {step.subject === null ? null : (
                      <span className="aw-code-value" title={step.subject}>
                        {step.subject}
                      </span>
                    )}
                  </li>
                ))}
              </ol>
            </section>
          ) : null}
        </section>

        {pending.length === 0 ? null : (
          <section aria-label="待批准的调用" className="aw-code-approvals">
            {pending.map((held) => (
              <article className="aw-code-approval" key={held.approval_id}>
                <h3>{held.tool_name} 需要你批准</h3>
                <p className="aw-code-value">{held.argument_digest}</p>
                <div className="aw-code-approval-actions">
                  {DECISIONS.filter(
                    // A standing yes to an irreversible effect is the one that
                    // must be asked every time, and the server refuses it --
                    // so it is not offered either. A button whose only outcome
                    // is a 422 teaches the reader the wrong rule.
                    ({ decision }) =>
                      decision !== "approve_for_session" ||
                      held.risk === null ||
                      !UNREPEATABLE.has(held.risk),
                  ).map(({ decision, label }) => (
                    <button
                      className="aw-button"
                      key={decision}
                      onClick={() => void decide(held.approval_id, decision)}
                      type="button"
                    >
                      {label}
                    </button>
                  ))}
                </div>
              </article>
            ))}
          </section>
        )}

        {error === null ? null : <ErrorNotice message={error} />}

        <form
          className="aw-code-composer"
          onSubmit={(event) => {
            event.preventDefault();
            void send();
          }}
        >
          <label className="aw-sr-only" htmlFor="aw-code-instruction">
            要做的事
          </label>
          <textarea
            disabled={running}
            id="aw-code-instruction"
            onChange={(event) => {
              setInstruction(event.target.value);
            }}
            placeholder="描述你要做的事"
            rows={3}
            value={instruction}
          />
          <button
            className="aw-button is-primary"
            disabled={running || instruction.trim() === ""}
            type="submit"
          >
            {running ? "正在处理" : "发送"}
          </button>
        </form>
      </main>

      {/* A plain wrapper so the two panes share one grid column while staying
          separate landmarks. The nav used to live inside the aside, where its
          rows counted as rows of the region labelled "工作区文件" -- anything
          reading that region, a screen reader first among them, announced
          session ids as though they were files the turn had produced. */}
      <div className="aw-code-side">
      <aside aria-label="工作区文件" className="aw-code-workspace">
        <header>
          <h2>工作区</h2>
          <button
            className="aw-button"
            disabled={opening}
            onClick={() => void openSession()}
            type="button"
          >
            {opening ? "正在打开" : "新建"}
          </button>
        </header>
        {files.length === 0 ? (
          <p className="aw-code-workspace-empty">还没有文件。</p>
        ) : (
          <ul>
            {files.map((file) => (
              <li key={file.name}>
                <button
                  aria-current={file.name === viewing?.name ? "true" : undefined}
                  className="aw-code-file-open"
                  onClick={() => void open(file)}
                  type="button"
                >
                  <span className="aw-code-file-name">{file.name}</span>
                  <span className="aw-code-value">{formatSize(file.size_bytes)}</span>
                </button>
              </li>
            ))}
          </ul>
        )}
        {viewing === null ? null : (
          <section aria-label={`文件 ${viewing.name}`} className="aw-code-file-view">
            <header>
              <h3>{viewing.name}</h3>
              <button
                className="aw-button"
                onClick={() => {
                  void downloadCodeWorkspaceFile(identity, viewing.sessionId, viewing.name)
                    .catch((cause: unknown) => {
                      setError(describe(cause));
                    });
                }}
                type="button"
              >
                下载
              </button>
            </header>
            {viewing.text === null ? (
              <p className="aw-code-value">
                {viewing.loading ? "正在读取" : "这个类型只能下载。"}
              </p>
            ) : (
              <>
                <pre>{viewing.text}</pre>
                {viewing.truncated ? (
                  <p className="aw-code-value">只显示了开头一部分，完整内容请下载。</p>
                ) : null}
              </>
            )}
          </section>
        )}
      </aside>
        {sessionList}
      </div>
    </div>
  );
}

/**
 * The file the reader has open, and what could be shown of it.
 *
 * `sessionId` is carried rather than read from the URL at download time: the
 * two are the same until the reader switches sessions with the viewer open, and
 * then they are not -- which would download one session's name against
 * another's working set and answer 404 for a file the reader can see.
 */
interface OpenedFile {
  sessionId: string;
  name: string;
  loading: boolean;
  text: string | null;
  truncated: boolean;
}

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${String(bytes)} B`;
  return `${(bytes / 1024).toFixed(1)} KB`;
}

function describe(cause: unknown): string {
  return cause instanceof Error ? cause.message : String(cause);
}
