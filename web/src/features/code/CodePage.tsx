/**
 * A coding session, in the browser.
 *
 * Two shapes, decided by whether there is a session. Without one the page is a
 * centered start: a composer and the list of recent sessions -- the two things
 * a person arriving here can act on -- and nothing else, because every other
 * pane describes a session that does not exist yet. The first sentence opens
 * the session (ADR-047) and the page becomes the working shape.
 *
 * The working shape is two columns with one question each. The left column is
 * the conversation: what was asked, the steps each turn took (`StepStream`,
 * one stage per turn), the report it came back with, and the composer to say
 * the next thing. The right column is the product: the working set's files,
 * with the opened one previewed *in that column* -- text, images and PDFs
 * in-page, the rest honestly download-only. It renders only when there is
 * something to show (a file in the workspace, or one already open); an empty
 * session keeps the whole width for the conversation.
 *
 * Upload lives beside the composer, because attaching a file is part of asking
 * -- it sat in the far pane's header before, visually unrelated to the act it
 * serves. The recent-sessions list keeps its markup (rename by double-click,
 * delete behind confirm) and folds behind a disclosure at the top of the
 * conversation column while a session is open; the start page shows it
 * unfolded, where "where was I" is the likeliest question.
 */

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Code2, Paperclip, Trash2 } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import {
  askCode,
  createCodeSession,
  decideCodeApproval,
  deleteCodeSession,
  downloadCodeWorkspaceFile,
  getCodeApprovals,
  getCodeHistory,
  getCodeWorkspace,
  getCodeWorkspaceFileBlob,
  getCodeWorkspaceFileText,
  listCodeSessions,
  newIdempotencyKey,
  putCodeWorkspaceFile,
  renameCodeSession,
} from "../../api/client";
import type {
  ApprovalDecision,
  MessageView,
  PendingApprovalView,
  PrincipalIdentity,
  WorkspaceEntryView,
} from "../../api/types";
import { useIdentity } from "../../app/IdentityContext";
import { BlobPreview } from "../../components/BlobPreview";
import { previewKind } from "../../components/media";
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
  //: Scoped to the session it happened in, and derived rather than cleared:
  //: an error from one session lingering over the next -- "artifact not
  //: found" hanging above a healthy workspace -- is the same one-render-late
  //: bug the `loadedFor` trick below exists for, solved the same way.
  const [fault, setFault] = useState<{ scope: string | null; text: string } | null>(
    null,
  );
  const error = fault !== null && fault.scope === (sessionId ?? null) ? fault.text : null;
  const [approvals, setApprovals] = useState<PendingApprovalView[]>([]);
  const [opened, setOpened] = useState<OpenedFile | null>(null);
  const [renaming, setRenaming] = useState<string | null>(null);
  const [uploading, setUploading] = useState(false);
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
      setFault({ scope: sessionId, text: describe(cause) });
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

  const send = useCallback(async () => {
    const text = instruction.trim();
    if (text === "" || running) return;

    // The session is opened here when there is not one yet: nobody arrives at
    // a coding tool wanting "a session", they arrive wanting a thing done, so
    // the first instruction both names the session (ADR-047) and starts the
    // work. The route's optional param is what lets `running` survive the
    // navigation below -- see App.tsx.
    let target = sessionId;
    setRunning(true);
    setFault(null);
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
      // `target`: the turn that opened the session reports where it now shows.
      setFault({ scope: target ?? null, text: describe(cause) });
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
      const kind = previewKind(file.media_type);
      // Shown before any fetch resolves, so a large file does not look like a
      // click that did nothing. Only text is fetched here: images and PDFs
      // fetch on render (`BlobPreview` caches by session and name), and a type
      // with no viewer skips the transfer entirely rather than downloading
      // bytes to decide not to render them.
      setOpened({
        sessionId,
        name: file.name,
        mediaType: file.media_type,
        sizeBytes: file.size_bytes,
        loading: kind === "text",
        text: null,
        truncated: false,
      });
      if (kind !== "text") return;
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
        setFault({ scope: sessionId, text: describe(cause) });
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
        setFault({ scope: sessionId, text: describe(cause) });
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
        setFault({ scope: sessionId ?? null, text: describe(cause) });
      }
    },
    [identity, queries, sessionId],
  );

  const attach = useCallback(
    async (chosen: FileList | null) => {
      if (chosen === null || chosen.length === 0) return;
      // The session has to exist before a file can go in it, and the composer
      // is reachable before one does. Rather than open a session here -- which
      // would create one whose first act was not an instruction, leaving it
      // unnamed in the list -- the start page says a sentence has to come
      // first, and the input stays disabled until one has.
      if (sessionId === undefined) {
        setFault({
          scope: null,
          text: "先说一句要做的事，会话开起来之后就能上传文件了。",
        });
        return;
      }
      setUploading(true);
      setFault(null);
      try {
        // Sequential, not `Promise.all`: each write advances the workspace
        // version with a compare-and-set against the one it read, so two
        // uploads in flight would race and the loser would be refused.
        for (const file of Array.from(chosen)) {
          const listing = await putCodeWorkspaceFile(identity, sessionId, file);
          setFiles(listing.files);
          setLoadedFor(sessionId);
        }
      } catch (cause: unknown) {
        setFault({ scope: sessionId, text: describe(cause) });
      } finally {
        setUploading(false);
      }
    },
    [identity, sessionId],
  );

  const remove = useCallback(
    async (target: string) => {
      // Confirmed because it cannot be undone and the row it removes is a
      // whole conversation. `window.confirm` rather than a dialog component:
      // this console has no modal of its own, and inventing one here would be
      // a second thing to review.
      if (!window.confirm("删除这个编码会话？它的对话和步骤都会消失。")) return;
      try {
        await deleteCodeSession(identity, target);
        await queries.invalidateQueries({ queryKey: ["code-sessions", identity] });
        // Only when the reader was looking at it. Navigating away from a
        // session they had not opened would move them for somebody else's sake.
        if (target === sessionId) await navigate("/code");
      } catch (cause: unknown) {
        setFault({ scope: sessionId ?? null, text: describe(cause) });
      }
    },
    [identity, navigate, queries, sessionId],
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
                <div className="aw-code-recent-row">
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
                  {/* Always rendered, not revealed on hover: a control that
                      only exists under a pointer is one a keyboard cannot
                      reach and a touch screen never shows. CSS dims it until
                      the row is hovered or the button focused. */}
                  <button
                    aria-label={`删除会话 ${held.title ?? held.session_id}`}
                    className="aw-code-recent-delete"
                    onClick={() => void remove(held.session_id)}
                    title="删除"
                    type="button"
                  >
                    <Trash2 aria-hidden size={13} />
                  </button>
                </div>
              )}
            </li>
          ))}
        </ul>
      </nav>
    );

  // One composer for both shapes of the page: attaching a file is part of
  // asking, so the control sits where the asking happens. The label wraps a
  // hidden input rather than a button clicking one through a ref -- the one
  // control shape a keyboard and a screen reader both already understand.
  const composer = (
    <form
      className="aw-code-composer"
      onSubmit={(event) => {
        event.preventDefault();
        void send();
      }}
    >
      <div className="aw-code-composer-row">
        <label
          className={`aw-code-attach ${uploading ? "is-busy" : ""}`}
          title={
            sessionId === undefined
              ? "发出第一句话后，就可以上传文件"
              : "上传文件到工作区"
          }
        >
          <Paperclip aria-hidden size={16} />
          <span className="aw-sr-only">
            {uploading ? "正在上传" : "上传文件"}
          </span>
          <input
            disabled={uploading || sessionId === undefined}
            multiple
            onChange={(event) => {
              void attach(event.target.files);
              // Cleared so choosing the same file twice fires `change`
              // again -- a re-upload after an edit is an ordinary thing to
              // want, and without this the second attempt does nothing.
              event.target.value = "";
            }}
            type="file"
          />
        </label>
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
      </div>
    </form>
  );

  // The start shape: no session means no transcript, no workspace and no
  // approvals, so none of those panes is mounted. What remains is what a
  // person can act on -- say the first sentence, or go back to a session.
  if (sessionId === undefined) {
    return (
      <div className="aw-code-start">
        <div className="aw-code-start-inner">
          <header className="aw-code-start-head">
            <Code2 aria-hidden />
            <h1>开始一段编码</h1>
            <p>
              描述你要做的事，比如「把 notes.md 里的待办整理成清单」。
              第一句话会开出一个会话，并成为它的名字；之后就可以在输入框旁上传文件。
            </p>
          </header>
          {error === null ? null : <ErrorNotice message={error} />}
          {composer}
          {sessionList}
        </div>
      </div>
    );
  }

  // The working shape. The right column exists only while it has something to
  // say -- a workspace with files, or a file already open. An empty session
  // showing an empty "工作区" pane beside an empty transcript was two panes of
  // nothing; the conversation keeps the width until there is a product.
  const showSide = files.length > 0 || viewing !== null;

  return (
    <div className={`aw-code-page${showSide ? "" : " is-solo"}`}>
      <main className="aw-code-main">
        <details className="aw-code-sessions-fold">
          <summary>会话</summary>
          <div className="aw-code-sessions-fold-body">
            {/* "新建" goes to the start page rather than POSTing an empty
                session: the first sentence is what names a session (ADR-047),
                and a session created by a bare click sits unnamed in the list
                forever. */}
            <button
              className="aw-button"
              onClick={() => void navigate("/code")}
              type="button"
            >
              新建会话
            </button>
            {sessionList}
          </div>
        </details>

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
              // A step that names a produced file selects it in the artifact
              // column, where the preview is -- the same click Work routes to
              // its reading column.
              onOpenArtifact={(artifact) => {
                const produced = files.find(
                  (held) => held.name === artifact.filename,
                );
                if (produced !== undefined) void open(produced);
              }}
              running={running}
              stages={stages}
            />
          )}
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

        {composer}
      </main>

      {showSide ? (
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
                <div className="aw-code-file-actions">
                  <button
                    className="aw-button"
                    onClick={() => {
                      void downloadCodeWorkspaceFile(
                        identity,
                        viewing.sessionId,
                        viewing.name,
                      ).catch((cause: unknown) => {
                        setFault({
                          scope: viewing.sessionId,
                          text: describe(cause),
                        });
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
              <FilePreview identity={identity} viewing={viewing} />
            </section>
          )}
        </aside>
      ) : null}
    </div>
  );
}

/**
 * The opened file's body, by what its type allows.
 *
 * Text was fetched when the file was opened and arrives through `viewing`;
 * images and PDFs fetch on render through `BlobPreview`, which owns the size
 * cap and the object-URL lifetime. A .docx lands on the download-only sentence
 * on purpose: the conversion endpoints are artifact-addressed and a workspace
 * file has no artifact id a client may hold (known-gaps F-11).
 */
function FilePreview({
  identity,
  viewing,
}: {
  identity: PrincipalIdentity;
  viewing: OpenedFile;
}) {
  const kind = previewKind(viewing.mediaType);
  if (kind === "image" || kind === "pdf") {
    return (
      <BlobPreview
        kind={kind}
        load={() =>
          getCodeWorkspaceFileBlob(identity, viewing.sessionId, viewing.name)
        }
        name={viewing.name}
        queryKey={["code-file-blob", viewing.sessionId, viewing.name]}
        sizeBytes={viewing.sizeBytes}
      />
    );
  }
  if (viewing.text === null) {
    return (
      <p className="aw-code-value">
        {viewing.loading ? "正在读取" : "这个类型只能下载。"}
      </p>
    );
  }
  return (
    <>
      <pre className="aw-code-file-body">{viewing.text}</pre>
      {viewing.truncated ? (
        <p className="aw-code-value">只显示了开头一部分，完整内容请下载。</p>
      ) : null}
    </>
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
  mediaType: string;
  sizeBytes: number;
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
