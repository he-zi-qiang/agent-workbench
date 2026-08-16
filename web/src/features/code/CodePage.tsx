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
 * This page now uses `StepStream` like the other two flows. The note that used
 * to stand here said it would join them "with A6"; A6 did not come, and in the
 * meantime this was the only surface whose steps could not be opened. Each turn
 * is a stage (`turnStages`), and every step inside it discloses the same facts,
 * arguments and tool output that Work and Chat have shown all along.
 *
 * The layout follows Work's, for a reason found by using this page: a wide
 * reading column with a narrow file rail beside it. Code used to put the file
 * *list* and the file *contents* both inside a 268px column, so opening
 * anything squeezed the list it was opened from and rendered the contents at
 * 11px in a quarter of the window.
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
import { EmptyState, ErrorNotice, shortId } from "../../components/ui";
import { MarkdownContent } from "../../components/MarkdownContent";
import { StepStream } from "../../components/StepStream";
import { eventTitle } from "../work/workTimeline";
import { codeTurnStages } from "./turnStages";
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

  const steps = useCodeStream(identity, sessionId);
  const stages = codeTurnStages(steps, running);

  // Derived, not reset. Both of these used to be cleared from an effect when
  // their subject changed, which is a render behind: the old session's file and
  // the finished turn's approvals were on screen for a frame first. The file
  // already carries the session it belongs to, and the approvals are only
  // meaningful while a turn runs, so both questions are answerable here.
  const viewing = opened?.sessionId === sessionId ? opened : null;
  const pending = running ? approvals : [];
  const messages = loadedFor === sessionId ? loadedMessages : [];
  const files = loadedFor === sessionId ? loadedFiles : [];

  //: Every setState here sits inside a `.then`, and that placement is load
  //: bearing. This used to be two `async` callbacks that awaited a fetch and
  //: then set state, which the effect below called; React's lint rule rejects
  //: that, because it judges the *call* -- a function that sets state, invoked
  //: from an effect body, is the cascading render it is looking for, and an
  //: `await` inside the callee does not change what the call site looks like.
  //: In a promise callback it is the same work with the same timing and the
  //: shape the rule asks for.
  //:
  //: The two fetches keep their own `.then` rather than sharing one after
  //: `Promise.all`, so that a workspace that fails to load still leaves the
  //: transcript on screen, and the other way round. The combined promise is
  //: only how the caller learns that something went wrong.
  const reload = useCallback(
    (id: string, signal?: AbortSignal) =>
      Promise.all([
        getCodeHistory(identity, id, signal).then((history) => {
          setMessages(history.messages);
          setLoadedFor(id);
        }),
        getCodeWorkspace(identity, id, signal).then((workspace) => {
          setFiles(workspace.files);
          setLoadedFor(id);
        }),
      ]),
    [identity],
  );

  useEffect(() => {
    if (sessionId === undefined) return;
    const controller = new AbortController();
    reload(sessionId, controller.signal).catch((cause: unknown) => {
      if (controller.signal.aborted) return;
      setError(describe(cause));
    });
    return () => {
      controller.abort();
    };
  }, [reload, sessionId]);

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
    if (text === "" || running) return;

    // The session is opened here when there is not one yet, which is what
    // removed the splash screen this page used to open with. That screen asked
    // for a click whose only effect was a POST the first instruction can carry
    // on its own: nobody arrives at a coding tool wanting "a session", they
    // arrive wanting a thing done. Opening lazily means the composer is the
    // first thing on screen and the first sentence both names the session
    // (ADR-047) and starts the work.
    let target = sessionId;
    setRunning(true);
    setError(null);
    setMessages((current) => [...current, { role: "user", text }]);
    setInstruction("");
    try {
      if (target === undefined) {
        const created = await createCodeSession(identity);
        target = created.session_id;
        // Navigate before the turn, not after: the turn holds its request open
        // for minutes, and a URL that only becomes shareable once the work
        // finishes is a URL nobody can send while the work is worth watching.
        await navigate(`/code/${target}`);
      }
      await askCode(identity, target, text, newIdempotencyKey("code"));
    } catch (cause: unknown) {
      setError(describe(cause));
    } finally {
      setRunning(false);
      // Both, and on every path. Re-reading rather than trusting the
      // optimistic append is what keeps this transcript from disagreeing with
      // the server's; and a refused turn may still have written files, because
      // the workspace pointer moves per write rather than at the end.
      //
      // `target`, not `sessionId`: on the turn that opened the session the
      // prop is still undefined here -- the navigation above does not write it
      // back into this closure -- so reading it would skip the reload on
      // exactly the turn that created everything there is to load.
      if (target !== undefined) {
        const settled = target;
        await Promise.all([
          reload(settled).catch(() => undefined),
          // The first instruction is what names the session, and every
          // instruction moves it to the top of the list.
          queries.invalidateQueries({ queryKey: ["code-sessions", identity] }),
        ]);
      }
    }
  }, [identity, instruction, navigate, queries, reload, running, sessionId]);

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

  // No early return for "no session yet" any more. That branch rendered a
  // full-screen door whose handle posted an empty session, and until somebody
  // turned it there was no composer on the page at all -- so the first thing a
  // coding tool asked of a reader was a click that did no work. The composer is
  // now always mounted, and `send` opens the session when there is not one.

  return (
    <div className="aw-code-page">
      <main className="aw-code-main">
        <section aria-label="编码会话" aria-live="polite" className="aw-code-transcript">
          {messages.length === 0 ? (
            <EmptyState
              icon={<Code2 aria-hidden />}
              title={sessionId === undefined ? "开始一段编码" : "这个会话还是空的"}
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
                  {/* The report is the agent's own prose and arrives as
                      Markdown -- lists, file names in backticks, occasionally a
                      fenced diff. Rendered as a paragraph it was one run-on
                      block with the syntax still in it. */}
                  {message.role === "user" ? (
                    <p>{message.text}</p>
                  ) : (
                    <MarkdownContent text={message.text} />
                  )}
                </li>
              ))}
            </ol>
          )}

          {/* Not gated on `running`. The steps of a finished turn are the ones
              a reader most often wants -- they are reading the report and
              asking how it got there -- and this pane used to be mounted only
              while the turn was in flight, over a list the hook emptied on the
              way out. Both halves of that are gone: `useCodeStream` keeps the
              session's events, and each turn is a stage that opens. */}
          {stages.length === 0 ? null : (
            <StepStream
              ariaLabel="执行过程"
              eventTitle={eventTitle}
              running={running}
              stages={stages}
            />
          )}
        </section>

        {/* The file, in the wide column. It was in the 268px rail beside the
            list it is opened from, where it squeezed that list and rendered a
            source file at 11px in a quarter of the window -- Work has answered
            this exact question since the day it got a preview, and answers it
            with the reading column. */}
        {viewing === null ? null : (
          <section aria-label={`文件 ${viewing.name}`} className="aw-code-file-view">
            <header>
              <h3>{viewing.name}</h3>
              <div className="aw-code-file-actions">
                <button
                  className="aw-button"
                  onClick={() => {
                    void downloadCodeWorkspaceFile(
                      identity,
                      viewing.sessionId,
                      viewing.name,
                    ).catch((cause: unknown) => {
                      setError(describe(cause));
                    });
                  }}
                  type="button"
                >
                  下载
                </button>
                <button
                  className="aw-button"
                  onClick={() => {
                    setOpened(null);
                  }}
                  type="button"
                >
                  关闭
                </button>
              </div>
            </header>
            {viewing.text === null ? (
              <p className="aw-code-value">
                {viewing.loading ? "正在读取" : "这个类型只能下载。"}
              </p>
            ) : (
              <>
                <pre className="aw-code-file-body">{viewing.text}</pre>
                {viewing.truncated ? (
                  <p className="aw-code-value">只显示了开头一部分，完整内容请下载。</p>
                ) : null}
              </>
            )}
          </section>
        )}

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
