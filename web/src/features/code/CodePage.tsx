/**
 * A coding session, in the browser.
 *
 * The turn is synchronous: the request stays open while the agent works, and
 * the report arrives in the response. That is why this page has no event
 * stream yet -- the steps a Code turn produces are on the session's SSE
 * endpoint and are worth showing, but rendering them is `StepStream`'s job
 * inside `ConversationShell`, which chat and work do not have either. When
 * that shell lands, this page joins it; until then a page that showed half a
 * step stream would be worse than one that shows none.
 *
 * What it does have is the two things a coding session cannot be used without:
 * the conversation, and the questions the agent stops on. Approvals are polled
 * rather than pushed for the same reason as above, and the poll only runs
 * while a turn is in flight -- there is nothing to answer when nothing is
 * asking.
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
  newIdempotencyKey,
} from "../../api/client";
import type {
  ApprovalDecision,
  MessageView,
  PendingApprovalView,
} from "../../api/types";
import { useIdentity } from "../../app/IdentityContext";
import { EmptyState, ErrorNotice, LoadingLine } from "../../components/ui";

/** How often to ask what the agent is stopped on, while it is working. */
const APPROVAL_POLL_MS = 1000;

/** The three answers, and the one that is not always offered. */
const DECISIONS: { decision: ApprovalDecision; label: string }[] = [
  { decision: "approve_once", label: "允许一次" },
  { decision: "approve_for_session", label: "本会话都允许" },
  { decision: "deny", label: "拒绝" },
];

export function CodePage() {
  const { identity } = useIdentity();
  const navigate = useNavigate();
  const { sessionId } = useParams<{ sessionId: string }>();

  const [messages, setMessages] = useState<MessageView[]>([]);
  const [instruction, setInstruction] = useState("");
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [approvals, setApprovals] = useState<PendingApprovalView[]>([]);
  const [opening, setOpening] = useState(false);

  const loadHistory = useCallback(
    async (id: string, signal?: AbortSignal) => {
      const history = await getCodeHistory(identity, id, signal);
      setMessages(history.messages);
    },
    [identity],
  );

  useEffect(() => {
    if (sessionId === undefined) {
      setMessages([]);
      return;
    }
    const controller = new AbortController();
    loadHistory(sessionId, controller.signal).catch((cause: unknown) => {
      if (controller.signal.aborted) return;
      setError(describe(cause));
    });
    return () => {
      controller.abort();
    };
  }, [loadHistory, sessionId]);

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
      await loadHistory(sessionId);
    } catch (cause: unknown) {
      setError(describe(cause));
      // Re-read rather than trust the optimistic append: a refused turn may
      // have recorded the instruction anyway, and guessing which is how a
      // transcript starts disagreeing with the server's.
      if (sessionId !== undefined) {
        await loadHistory(sessionId).catch(() => undefined);
      }
    } finally {
      setRunning(false);
    }
  }, [identity, instruction, loadHistory, running, sessionId]);

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
      <div className="aw-code-page">
        <main className="aw-code-main">
          <EmptyState
            icon={<Code2 aria-hidden />}
            title="还没有编码会话"
            description="打开一个会话，然后用一句话描述你要做的事。"
          />
          <button
            className="aw-button is-primary"
            disabled={opening}
            onClick={() => void openSession()}
            type="button"
          >
            {opening ? "正在打开" : "新建编码会话"}
          </button>
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
            messages.map((message, index) => (
              <article
                className={
                  message.role === "user" ? "aw-code-said" : "aw-code-report"
                }
                key={`${message.role}-${String(index)}`}
              >
                <h3>{message.role === "user" ? "你" : "报告"}</h3>
                <p>{message.text}</p>
              </article>
            ))
          )}
          {running ? <LoadingLine label="正在处理" /> : null}
        </section>

        {approvals.length === 0 ? null : (
          <section aria-label="待批准的调用" className="aw-code-approvals">
            {approvals.map((held) => (
              <article className="aw-code-approval" key={held.approval_id}>
                <h3>{held.tool_name} 需要你批准</h3>
                <p className="aw-code-approval-digest">{held.argument_digest}</p>
                <div className="aw-code-approval-actions">
                  {DECISIONS.filter(
                    // A standing yes to an irreversible effect is the one that
                    // must be asked every time, and the server refuses it --
                    // so it is not offered either.
                    ({ decision }) =>
                      decision !== "approve_for_session" ||
                      (held.risk !== "external" && held.risk !== "destructive"),
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
          <label className="aw-visually-hidden" htmlFor="aw-code-instruction">
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
    </div>
  );
}

function describe(cause: unknown): string {
  return cause instanceof Error ? cause.message : String(cause);
}
