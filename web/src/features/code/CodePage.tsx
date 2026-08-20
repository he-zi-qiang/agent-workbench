/**
 * A coding session, in the browser.
 *
 * Three regions while a session is open. **Left** is the session list, always
 * there. **Middle** is the conversation, and a conversation here means one
 * block per instruction holding everything that instruction caused -- what it
 * did, the files it produced, the report, what it thought. **Right** is a
 * preview surface that mounts when you click something and unmounts when you
 * close it; it is not a file browser, and the full listing lives folded at its
 * foot.
 *
 * What this replaced, and why each piece went:
 *
 * * The session list was a `<details>` fold at the top of the transcript, and
 *   a second unfolded copy on the start page. One list, one place.
 * * The step stream was `StepStream` -- Work's component, over stages built by
 *   `codeTurnStages`. Work has a graph and its reader asks which node a run is
 *   on. A coding session has no graph. Borrowing the component brought a node
 *   rail, a `第 N 轮` pseudo-stage and three nested disclosures between the
 *   reader and a filename, and it brought `workTimeline`'s vocabulary with it
 *   -- `TaskDeadLettered` is not a phrase that belongs over a coding step.
 * * The reasoning excerpt rendered inside each of those steps *and* streamed
 *   live above them *and* appeared again inside each step's raw JSON dump.
 *   Now: live in the running block, excerpt in that block's 想过什么 fold, and
 *   `buildTurnBlocks` takes the excerpt only from `ModelCompleted` -- so the
 *   two sets are disjoint by construction, not by timing (ADR-061, narrowed by
 *   ADR-063).
 * * The right column mounted on `files.length > 0`, taking up to 560px from
 *   the first turn onward whether or not anyone wanted to look at anything.
 *
 * Kept deliberately: upload sits beside the composer, because attaching a file
 * is part of asking, and it spent a while in the far pane's header where it
 * was visually unrelated to the act it serves. And the derived-not-reset
 * discipline below (`loadedFor`, `fault.scope`, `viewing.sessionId`) -- every
 * one of those exists because clearing state from an effect is a render late,
 * and the previous session's transcript, error or open file was on screen for
 * that frame and for the whole of the next session's fetch.
 */

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowUpRight, Code2, PanelLeft, Paperclip } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
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
  listCodeSessions,
  newIdempotencyKey,
  putCodeWorkspaceFile,
  renameCodeSession,
} from "../../api/client";
import type {
  ApprovalDecision,
  CodeSessionListResponse,
  MessageView,
  PendingApprovalView,
  WorkspaceEntryView,
} from "../../api/types";
import { useIdentity } from "../../app/IdentityContext";
import { effectiveMediaType } from "../../components/media";
import { EmptyState, ErrorNotice, IconButton } from "../../components/ui";
import { CodeSessionRail } from "./CodeSessionRail";
import { CodeTurn } from "./CodeTurn";
import type { OpenedFile } from "./FilePreview";
import { PreviewPanel } from "./PreviewPanel";
import { buildTurnBlocks } from "./turnBlocks";
import { useCodeStream } from "./useCodeStream";

/** How often to ask what the agent is stopped on, while it is working. */
const APPROVAL_POLL_MS = 1000;

const CODE_STARTERS = [
  {
    title: "理解并整理代码",
    prompt: "阅读当前项目，先说明结构和关键模块，再整理一份可执行的改进清单。",
  },
  {
    title: "定位并修复问题",
    prompt: "帮我定位这个问题的根因，修复后运行相关检查，并说明改动影响。",
  },
  {
    title: "实现一个小功能",
    prompt: "在保持现有架构和风格的前提下，实现下面这个功能，并补齐必要验证：",
  },
] as const;

/**
 * How long a run with no instruction is given to explain itself.
 *
 * Long enough that the reload `askCode` already triggered wins the race in the
 * ordinary case -- that one is a loopback fetch, tens of milliseconds -- and
 * short enough that a reader watching a turn started somewhere else is not
 * staring at 这个会话还是空的 while steps stream past underneath.
 */
const ORPHAN_RELOAD_DELAY_MS = 600;

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
  const instructionRef = useRef<HTMLTextAreaElement>(null);
  const [running, setRunning] = useState(false);
  //: The instruction whose request is still open, held here rather than
  //: appended to `loadedMessages`. Appending optimistically and then re-reading
  //: the server's transcript is how the same sentence ends up on screen twice;
  //: worse, on the turn that *opens* a session the optimistic copy was dropped
  //: by the `loadedFor` guard the moment the route changed, so the reader's own
  //: instruction vanished and the pane said "这个会话还是空的" under it.
  //:
  //: It carries the session it was typed into, and that is not decoration. The
  //: turn that opens a session navigates while its request is still open, so a
  //: reader who switches to another session mid-turn used to find their
  //: sentence sitting at the foot of *that* transcript, under a spinner
  //: belonging to a run it has nothing to do with. `sessionId: null` is the one
  //: instant before the session exists, and the start page draws no blocks.
  const [pending, setPending] = useState<{
    sessionId: string | null;
    text: string;
  } | null>(null);
  //: Scoped to the session it happened in, and derived rather than cleared:
  //: an error from one session lingering over the next -- "artifact not
  //: found" hanging above a healthy workspace -- is the same one-render-late
  //: bug the `loadedFor` trick above exists for, solved the same way.
  const [fault, setFault] = useState<{ scope: string | null; text: string } | null>(
    null,
  );
  const error = fault !== null && fault.scope === (sessionId ?? null) ? fault.text : null;
  const [approvals, setApprovals] = useState<PendingApprovalView[]>([]);
  const [opened, setOpened] = useState<OpenedFile | null>(null);
  const [renaming, setRenaming] = useState<string | null>(null);
  const [uploading, setUploading] = useState(false);
  const [mobileSessionsOpen, setMobileSessionsOpen] = useState(false);
  const [railHidden, setRailHidden] = useState(false);
  const [panelOpen, setPanelOpen] = useState(false);
  const [directoryOpen, setDirectoryOpen] = useState(false);
  const queries = useQueryClient();

  // A query rather than an effect, because two things invalidate it -- opening
  // a session and renaming one -- and both happen somewhere other than where
  // the list is rendered.
  const sessions = useQuery({
    queryKey: ["code-sessions", identity],
    queryFn: () => listCodeSessions(identity),
  });
  const known = sessions.data?.sessions ?? [];

  const { steps, thinking, thinkingCallId, answer, progress } = useCodeStream(
    identity,
    sessionId,
  );

  // Derived, not reset. Both of these used to be cleared from an effect when
  // their subject changed, which is a render behind: the old session's file and
  // the finished turn's approvals were on screen for a frame first. The file
  // already carries the session it belongs to, and the approvals are only
  // meaningful while a turn runs, so both questions are answerable here.
  const viewing = opened?.sessionId === sessionId ? opened : null;
  const pendingApprovals = running ? approvals : [];
  const messages = loadedFor === sessionId ? loadedMessages : [];
  // The sentence to draw a block for, or null because the server's transcript
  // already carries it -- or because it was typed into a different session.
  const pendingInstruction =
    running && pending !== null && (pending.sessionId ?? sessionId) === sessionId
      ? pending.text
      : null;
  // Memoised, unlike the three above, only because `openByName` closes over it:
  // the `[]` branch is a fresh array every render, which would give that
  // callback a new identity on every frame the event stream delivers.
  const files = useMemo(
    () => (loadedFor === sessionId ? loadedFiles : []),
    [loadedFiles, loadedFor, sessionId],
  );

  // Which run is live is derived inside `buildTurnBlocks`, from the run
  // bookkeeping in the events themselves rather than from anything this
  // component remembers about the moment it pressed send.
  const { blocks, orphanRuns, orphanRunIds } = buildTurnBlocks({
    messages,
    events: steps,
    running,
    pendingInstruction,
    liveCallId: thinkingCallId,
  });

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
          // These land in one React batch, which is the point: the server's
          // copy of the instruction appears in the same commit that drops the
          // pending one, so the sentence never flickers as two.
          setMessages(history.messages);
          setLoadedFor(id);
          // Dropped only when the transcript being installed *has* the
          // sentence, and reading that off the data rather than off which call
          // site asked for the reload is the whole fix.
          //
          // The unconditional `setPending(null)` that used to be here was
          // right for the reload at the end of a turn and catastrophic for the
          // one the route change fires: opening a session navigates *before*
          // the turn is sent, so the effect below re-read a transcript the
          // server had not written the instruction into yet, cleared the
          // pending copy, and left `buildTurnBlocks` with no block at all.
          // Measured on a real 7-second turn: the pane said 这个会话还是空的
          // for all of it -- no instruction, no steps, no thinking, no report
          // -- and then the finished turn appeared whole. Every session's
          // first turn looked like the console had frozen and then pasted.
          setPending((held) =>
            held !== null &&
            history.messages.some(
              (message) => message.role === "user" && message.text === held.text,
            )
              ? null
              : held,
          );
        }),
        getCodeWorkspace(identity, id, signal).then((workspace) => {
          setFiles(displayable(workspace.files));
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

  //: A run the transcript cannot place is usually a transcript this page read
  //: a moment too early, not another tab.
  //:
  //: The transcript is fetched when the session changes and again when *this
  //: tab's* turn returns, and nothing else. So a run that started any other
  //: way -- the reader reloaded the page a second after sending, a second tab,
  //: anything posting to the same session -- arrives on the event stream with
  //: no instruction to hang off. `buildTurnBlocks` refuses to guess which turn
  //: it belongs to and drops it, which is right, and the pane then says
  //: 这个会话还是空的 while the steps stream past underneath. Measured that way:
  //: a turn posted outside this tab rendered nothing at all, start to finish.
  //:
  //: Re-reading the transcript is the whole fix -- the server appends the user
  //: message *before* the run starts, so the sentence is already there.
  //:
  //: Keyed on the run id and not on `orphanRuns`, because the count is not a
  //: fresh signal: a genuinely unpairable run holds it above zero forever, and
  //: a reload keyed on that would fetch on every render until the session
  //: closed. Each id is tried exactly once; if the reload does not produce an
  //: instruction for it, the page keeps the honest gap it already showed.
  const attempted = useRef<{ session: string; ids: Set<string> }>({
    session: "",
    ids: new Set(),
  });
  const orphanKey = orphanRunIds.join(",");
  useEffect(() => {
    if (sessionId === undefined || orphanKey === "") return;
    // Not before this session's transcript has landed once. Opening a session
    // with history replays every past run onto the stream while the first
    // fetch is still in flight, so for that window there are no instructions
    // and *every* run looks orphaned -- and re-reading then would add a second
    // fetch to every session open, to learn what the first one was already on
    // its way to say. After it has landed, an unpairable run is news.
    if (loadedFor !== sessionId) return;
    // Carried with its session, like everything else on this page: ids are
    // unique per run, but a set that outlived the session it was filled for
    // would grow for as long as the tab stayed open.
    if (attempted.current.session !== sessionId) {
      attempted.current = { session: sessionId, ids: new Set() };
    }
    const fresh = orphanKey
      .split(",")
      .filter((id) => !attempted.current.ids.has(id));
    if (fresh.length === 0) return;
    const controller = new AbortController();
    // Waited out rather than fired at once, because the ordinary path has this
    // same shape for a moment. When *this tab's* turn returns, `running` drops
    // and the run joins the settled list while the reload that `askCode`
    // triggered is still in flight -- so for a few hundred milliseconds the
    // page holds a run its transcript cannot place, and re-reading then would
    // add a second fetch after every turn to learn what the first one was
    // already about to say. If that reload lands, the orphan disappears, this
    // effect is torn down and the timer never fires. What survives the wait is
    // a run nothing else is going to explain.
    const timer = window.setTimeout(() => {
      for (const id of fresh) attempted.current.ids.add(id);
      reload(sessionId, controller.signal).catch(() => {
        // A failed re-read is not a failed turn, and the id is spent either
        // way: retrying on the next render is the loop this is shaped to
        // avoid.
      });
    }, ORPHAN_RELOAD_DELAY_MS);
    return () => {
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [loadedFor, orphanKey, reload, sessionId]);

  // Only while a turn is running. A poll that kept going would ask a question
  // nobody is waiting on the answer to, once a second, forever.
  const pollingSession = running ? sessionId : undefined;
  useEffect(() => {
    if (pollingSession === undefined) return;
    const controller = new AbortController();
    const tick = () => {
      getCodeApprovals(identity, pollingSession, controller.signal)
        .then((held) => {
          setApprovals(held.approvals);
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
    setPending({ sessionId: sessionId ?? null, text });
    setInstruction("");
    try {
      if (target === undefined) {
        const created = await createCodeSession(identity);
        // A `const` beside the `let`, because the callback below closes over it
        // and a reassignable binding is `string | undefined` in there however
        // obviously it was just assigned.
        const opened = created.session_id;
        target = opened;
        setPending({ sessionId: opened, text });
        // Into the list now, not when the turn comes back. The invalidation in
        // `finally` is the only thing that used to put a new session in the
        // rail, and a coding turn holds its request open for minutes -- so for
        // all of them the session the reader was watching was the one session
        // not in the list beside it, and leaving the page was the only way to
        // make it appear.
        //
        // The name is this client's own reading of the instruction, and it is
        // provisional in the honest sense: the server derives the same name
        // from the same sentence by the same rule (`session_titles.py`, ADR-047)
        // and its copy replaces this one at the invalidation below. Prepended
        // rather than inserted by date, because the list is ordered by last
        // spoken in and this session was just spoken in.
        queries.setQueryData<CodeSessionListResponse>(
          ["code-sessions", identity],
          (held) => ({
            sessions: [
              {
                session_id: opened,
                title: provisionalTitle(text),
                last_activity_at: null,
              },
              ...(held?.sessions ?? []),
            ],
          }),
        );
        // Navigate before the turn, not after: the turn holds its request open
        // for minutes, and a URL that only becomes shareable once the work
        // finishes is a URL nobody can send while the work is worth watching.
        await navigate(`/code/${target}`);
      }
      const answer = await askCode(identity, target, text, newIdempotencyKey("code"));
      // A turn that dies on its budget appends no assistant message at all
      // (the server declines to invent one), so without this the transcript
      // shows the instruction and then silence -- which reads as "it cannot
      // do anything any more", not as "that turn ran out".
      if (answer.status !== "completed") {
        setFault({ scope: target, text: stopNote(answer.stop_reason) });
      }
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
          // `pending` is deliberately *not* cleared on the failure path: the
          // server appends the user message before the run starts, so a failed
          // reload leaves this block as the only record on screen of the
          // sentence the reader typed. Losing it would be worse than showing
          // it twice, and the success path already prevents the twice.
          reload(settled).catch(() => undefined),
          // The first instruction is what names the session, and every
          // instruction moves it to the top of the list.
          queries.invalidateQueries({ queryKey: ["code-sessions", identity] }),
        ]);
      }
    }
  }, [identity, instruction, navigate, queries, reload, running, sessionId]);

  // What a run started from a preview changes out here. Only the listing: the
  // per-file bodies are react-query caches and `FilePreview` invalidates the
  // ones it knows went stale, but the working set lives in this component's
  // state -- read once per turn -- and a file a script wrote is a name that
  // was not in it. Without this, running a `.py` that produces `out.csv` leaves
  // the card for it unclickable and the 工作区 count one short until the next
  // instruction.
  const refreshWorkspace = useCallback(() => {
    if (sessionId === undefined) return;
    const target = sessionId;
    getCodeWorkspace(identity, target)
      .then((workspace) => {
        setFiles(displayable(workspace.files));
        setLoadedFor(target);
      })
      .catch((cause: unknown) => {
        setFault({ scope: target, text: describe(cause) });
      });
  }, [identity, sessionId]);

  // Naming what to show, and nothing else. Every kind fetches inside its own
  // preview component now, which is what deleted the rest of this callback:
  // it used to prefetch text here, hold `loading`/`text`/`truncated` on the
  // opened file, and merge a late response back in while guarding against a
  // second click landing first. All of that was one kind's special case, and
  // it is the reason a produced `.py` could not be previewed inside the
  // conversation -- there was nowhere in a card to run the prefetch.
  const open = useCallback(
    (file: WorkspaceEntryView) => {
      if (sessionId === undefined) return;
      setPanelOpen(true);
      setOpened({
        sessionId,
        name: file.name,
        mediaType: file.media_type,
        sizeBytes: file.size_bytes,
      });
    },
    [sessionId],
  );

  // What a card in the conversation clicks. A card knows the name a tool
  // wrote; the size and media type come from the current listing, which is why
  // a name no longer in the workspace has no entry to open -- the card renders
  // that case disabled rather than routing to a 404.
  const openByName = useCallback(
    (name: string) => {
      const held = files.find((file) => file.name === name);
      if (held !== undefined) open(held);
    },
    [files, open],
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
          setFiles(displayable(listing.files));
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

  const rail = (
    <CodeSessionRail
      known={known}
      mobileOpen={mobileSessionsOpen}
      onCloseMobile={() => {
        setMobileSessionsOpen(false);
      }}
      onDelete={(target) => void remove(target)}
      onNew={() => {
        setMobileSessionsOpen(false);
        void navigate("/code");
      }}
      onOpen={(target) => {
        setMobileSessionsOpen(false);
        void navigate(`/code/${target}`);
      }}
      onRename={(target, title) => void rename(target, title)}
      renaming={renaming}
      sessionId={sessionId}
      setRenaming={setRenaming}
    />
  );

  const backdrop = mobileSessionsOpen ? (
    <button
      aria-label="关闭会话列表"
      className="aw-code-sessions-backdrop"
      onClick={() => {
        setMobileSessionsOpen(false);
      }}
      type="button"
    />
  ) : null;

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
          ref={instructionRef}
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

  // The start shape. The rail is mounted here too -- it is the same list in
  // the same place, so arriving with no session and arriving with one are the
  // same page with different middles, rather than two layouts a reader has to
  // re-learn. What is gone from the middle is the second copy of the list.
  if (sessionId === undefined) {
    return (
      <div className={`aw-code-page${railHidden ? " is-rail-hidden" : ""}`}>
        {backdrop}
        {rail}
        <main className="aw-code-main">
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
              <div className="aw-code-starters" aria-label="编码任务起点">
                {CODE_STARTERS.map((starter) => (
                  <button
                    aria-label={starter.title}
                    key={starter.title}
                    onClick={() => {
                      setInstruction(starter.prompt);
                      window.requestAnimationFrame(() => instructionRef.current?.focus());
                    }}
                    type="button"
                  >
                    <span>{starter.title}</span>
                    <ArrowUpRight aria-hidden="true" size={15} />
                  </button>
                ))}
              </div>
              {error === null ? null : <ErrorNotice message={error} />}
              {composer}
            </div>
          </div>
        </main>
      </div>
    );
  }

  const title = known.find((held) => held.session_id === sessionId)?.title;

  return (
    <div
      className={`aw-code-page${railHidden ? " is-rail-hidden" : ""}${
        panelOpen ? " has-preview" : ""
      }`}
    >
      {backdrop}
      {rail}

      <main className="aw-code-main">
        <header className="aw-code-header">
          <IconButton
            className="aw-code-mobile-sessions"
            label="打开会话列表"
            onClick={() => {
              setMobileSessionsOpen(true);
            }}
          >
            <PanelLeft aria-hidden size={18} />
          </IconButton>
          <IconButton
            className="aw-code-rail-toggle"
            label={railHidden ? "显示会话栏" : "隐藏会话栏"}
            onClick={() => {
              setRailHidden((held) => !held);
            }}
          >
            <PanelLeft aria-hidden size={18} />
          </IconButton>
          <div className="aw-code-header-copy">
            <p className="aw-eyebrow">Code</p>
            <h1>{title ?? "新会话"}</h1>
          </div>
          {/* The way to the whole working set, including everything no card
              could account for. Absent entirely when there is nothing in it. */}
          {files.length === 0 ? null : (
            <button
              className="aw-button aw-code-workspace-entry"
              onClick={() => {
                setPanelOpen(true);
                setDirectoryOpen(true);
              }}
              type="button"
            >
              工作区 {files.length}
            </button>
          )}
        </header>

        <section aria-label="编码会话" aria-live="polite" className="aw-code-transcript">
          {blocks.length === 0 ? (
            <EmptyState
              icon={<Code2 aria-hidden />}
              title="这个会话还是空的"
              description="描述你要做的事，比如「把 notes.md 里的待办整理成清单」。"
            />
          ) : (
            <ol className="aw-code-turns">
              {blocks.map((block) => (
                <CodeTurn
                  block={block}
                  files={files}
                  identity={identity}
                  key={block.key}
                  liveThinking={block.live ? thinking : ""}
                  liveThinkingCallId={block.live ? thinkingCallId : ""}
                  liveAnswer={block.live ? answer : ""}
                  onOpen={openByName}
                  onWrote={refreshWorkspace}
                  openedName={viewing?.name ?? null}
                  sessionId={sessionId}
                  // NOT gated on `live`, unlike the three above it, and the
                  // difference is what `live` actually means: `buildTurnBlocks`
                  // sets it from `running`, which is whether *this tab's own*
                  // ask request is still open. That is the right gate for the
                  // thought and the report -- both belong to a model call this
                  // tab started. It is the wrong one here.
                  //
                  // Measured, not reasoned about: a run driven from anywhere
                  // other than this tab's open request -- a reload part way
                  // through, a second tab, a turn posted by something else --
                  // left every step showing 进行中 with nothing under it, while
                  // `ToolProgress` frames arrived on the stream the whole time.
                  // The reader most likely to ask "is this stuck?" is the one
                  // who just reloaded, and they were the one guaranteed to get
                  // no answer.
                  //
                  // Ungating is safe because a *narrower* gate already exists
                  // one level down: `TurnStepRow` draws this only for a step
                  // whose outcome is `running`, and the hook drops a call from
                  // the map the moment it returns. Both are per tool call,
                  // which is the thing progress is actually about.
                  toolProgress={progress}
                />
              ))}
            </ol>
          )}
        </section>

        {/* Stays at page level rather than moving into the turn block, and
            sticks to the composer: an approval is an interruption, and what it
            needs is the reader's eyes on it now. A turn block scrolls. */}
        {pendingApprovals.length === 0 ? null : (
          <section aria-label="待批准的调用" className="aw-code-approvals">
            {pendingApprovals.map((held) => (
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

      {panelOpen ? (
        <>
          <button
            aria-label="关闭预览"
            className="aw-code-preview-backdrop"
            onClick={() => {
              setPanelOpen(false);
            }}
            type="button"
          />
          <PreviewPanel
            directoryOpen={directoryOpen}
            files={files}
            identity={identity}
            onClose={() => {
              setPanelOpen(false);
            }}
            onDownload={() => {
              if (viewing === null) return;
              void downloadCodeWorkspaceFile(
                identity,
                viewing.sessionId,
                viewing.name,
              ).catch((cause: unknown) => {
                setFault({ scope: viewing.sessionId, text: describe(cause) });
              });
            }}
            onOpen={open}
            onWrote={refreshWorkspace}
            orphanRuns={orphanRuns}
            setDirectoryOpen={setDirectoryOpen}
            viewing={viewing}
          />
        </>
      ) : null}
    </div>
  );
}

/**
 * The listing as this page will show it, with unknowable types read off names.
 *
 * Applied at the one place a listing enters the page rather than at each of the
 * four that read one (cards, the panel, the directory fold, the auto-preview
 * choice). Those four must agree about what a file *is* -- a `notes.md` that
 * previews as text in the panel and offers only 下载 on its card is the same
 * class of split this console spent ADR-066 removing -- and agreement is
 * cheapest when there is one answer rather than four call sites remembering to
 * ask the same question.
 *
 * Display only. Downloads read the server's own headers, and every
 * authorization is decided server-side against the stored type; nothing here
 * reaches either.
 */
function displayable(files: WorkspaceEntryView[]): WorkspaceEntryView[] {
  return files.map((file) => {
    const media_type = effectiveMediaType(file.media_type, file.name);
    return media_type === file.media_type ? file : { ...file, media_type };
  });
}

/**
 * One sentence for a turn that stopped without a report.
 *
 * The vocabulary is the runtime's `StopReason`; what every branch has to say
 * is the same two things -- the work so far is safe (writes land per write,
 * not at the end), and the way forward is to just keep talking. A stopped
 * turn used to render as nothing at all, which read as a broken session.
 */
function stopNote(reason: string): string {
  if (reason === "deadline") {
    return "这一轮到时间停下了。已完成的改动都在工作区里，直接说下一步就能继续。";
  }
  if (reason === "max_steps" || reason === "max_tool_calls") {
    return "这一轮把步数用完了。已完成的改动都在工作区里，直接说下一步就能继续。";
  }
  if (reason === "token_budget" || reason === "cost_budget") {
    return "这一轮把预算用完了。已完成的改动都在工作区里，直接说下一步就能继续。";
  }
  if (reason === "cancelled") {
    return "这一轮被取消了。已完成的改动都在工作区里。";
  }
  return `这一轮没有跑完（${reason}）。已完成的改动都在工作区里，直接说下一步就能继续。`;
}

/** How much of an instruction fits in a sidebar row. `DEFAULT_TITLE_LIMIT`. */
const TITLE_LIMIT = 120;

/**
 * The name a session is about to be given, so its row can be drawn now.
 *
 * A deliberate restatement of `application/session_titles.py`, not a second
 * source of truth: the server is the one that names a session (ADR-047), and
 * the name it derives lands in this list at the invalidation that ends the
 * turn. What this buys is the minutes in between, during which the row would
 * otherwise have to read as a bare `ses_2565…` -- an id is not a name, and a
 * reader watching a turn should not have to recognise their own work by one.
 *
 * The three rules are the ones over there, for the reasons written over there:
 * the first non-empty line (a multi-line instruction opens with the request and
 * continues with the details), interior whitespace collapsed (formatting inside
 * a one-line label is noise), and a single ellipsis rather than a hard cut.
 */
function provisionalTitle(text: string): string | null {
  for (const line of text.split("\n")) {
    const collapsed = line.split(/\s+/).filter(Boolean).join(" ");
    if (collapsed === "") continue;
    // Counted in code points, not UTF-16 units: `length` would cut a title of
    // emoji at half the characters it allows a title of Chinese.
    const runes = [...collapsed];
    if (runes.length <= TITLE_LIMIT) return collapsed;
    return runes.slice(0, TITLE_LIMIT).join("").trimEnd() + "…";
  }
  return null;
}

function describe(cause: unknown): string {
  return cause instanceof Error ? cause.message : String(cause);
}
