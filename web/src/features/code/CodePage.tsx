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

import { Code2 } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import {
  askCode,
  createCodeSession,
  decideCodeApproval,
  getCodeApprovals,
  getCodeHistory,
  getCodeWorkspace,
  newIdempotencyKey,
} from "../../api/client";
import type {
  ApprovalDecision,
  MessageView,
  PendingApprovalView,
  WorkspaceEntryView,
} from "../../api/types";
import { useIdentity } from "../../app/IdentityContext";
import { EmptyState, ErrorNotice, LoadingLine } from "../../components/ui";
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

  const [messages, setMessages] = useState<MessageView[]>([]);
  const [files, setFiles] = useState<WorkspaceEntryView[]>([]);
  const [instruction, setInstruction] = useState("");
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [approvals, setApprovals] = useState<PendingApprovalView[]>([]);
  const [opening, setOpening] = useState(false);

  const steps = useCodeStream(identity, sessionId, running);

  const loadHistory = useCallback(
    async (id: string, signal?: AbortSignal) => {
      const history = await getCodeHistory(identity, id, signal);
      setMessages(history.messages);
    },
    [identity],
  );

  const loadWorkspace = useCallback(
    async (id: string, signal?: AbortSignal) => {
      const workspace = await getCodeWorkspace(identity, id, signal);
      setFiles(workspace.files);
    },
    [identity],
  );

  useEffect(() => {
    if (sessionId === undefined) {
      setMessages([]);
      setFiles([]);
      return;
    }
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
    if (pollingSession === undefined) {
      setApprovals([]);
      return;
    }
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
      navigate(`/code/${created.session_id}`);
    } catch (cause: unknown) {
      setError(describe(cause));
    } finally {
      setOpening(false);
    }
  }, [identity, navigate]);

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
        ]);
      }
    }
  }, [identity, instruction, loadHistory, loadWorkspace, running, sessionId]);

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
              <ol>
                {steps.map((event) => (
                  <li key={event.event_id}>{eventTitle(event)}</li>
                ))}
              </ol>
            </section>
          ) : null}
        </section>

        {approvals.length === 0 ? null : (
          <section aria-label="待批准的调用" className="aw-code-approvals">
            {approvals.map((held) => (
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

      <aside aria-label="工作区文件" className="aw-code-workspace">
        <header>
          <h2>工作区</h2>
        </header>
        {files.length === 0 ? (
          <p className="aw-code-workspace-empty">还没有文件。</p>
        ) : (
          <ul>
            {files.map((file) => (
              <li key={file.name}>
                <span className="aw-code-file-name">{file.name}</span>
                <span className="aw-code-value">{formatSize(file.size_bytes)}</span>
              </li>
            ))}
          </ul>
        )}
      </aside>
    </div>
  );
}

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${String(bytes)} B`;
  return `${(bytes / 1024).toFixed(1)} KB`;
}

function describe(cause: unknown): string {
  return cause instanceof Error ? cause.message : String(cause);
}
