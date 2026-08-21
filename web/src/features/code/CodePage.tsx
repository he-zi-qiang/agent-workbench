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
import { ArrowUp, Code2, LoaderCircle, PanelLeft, Paperclip } from "lucide-react";
import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";
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
import {
  useWorkspaceSidebar,
  WorkspaceSidebarPortal,
} from "../../app/WorkspaceSidebar";
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
    title: "规划一个实现",
    prompt: "请先拆解这个功能的实现方案，说明关键决策、风险和验证方式：",
  },
  {
    title: "生成一个文件",
    prompt: "根据下面的要求生成一个完整文件，并检查内容是否可以直接使用：",
  },
  {
    title: "编写测试用例",
    prompt: "为下面的行为编写清晰的测试用例，覆盖正常路径、边界条件和失败情况：",
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

/**
 * What a risk means, said to the person being asked.
 *
 * `risk` has been on the wire since approvals existed (`api/types.ts:119`) and
 * this console used it for exactly one thing: deciding whether to *remove* the
 * 本次会话都允许 button. So the reader was asked to approve a tool call with no
 * indication of what kind of call it was, and -- when the third button was
 * missing -- no explanation of why the choice they had last time was gone.
 * That is not "conveyed by colour alone"; it is not conveyed at all.
 *
 * Unknown values fall through to the raw string rather than to silence: a risk
 * this console has not been taught is still a fact the reader should see.
 */
const RISK_LABELS: Readonly<Record<string, string>> = {
  read: "只读取",
  write: "会写入",
  external: "会连到外部",
  destructive: "不可撤销",
};

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
  //: Which sessions have a turn open — a set, not a boolean, and scoped for
  //: the same reason `loadedFor`, `fault.scope`, `pending.sessionId` and
  //: `viewing.sessionId` are. A page-wide flag meant that while a turn ran in
  //: A and the reader was in B: B's composer was disabled and wore A's
  //: spinner, so they could not send anything; B's approvals rendered as
  //: though B were running; and `buildTurnBlocks` marked B's newest run live
  //: even when it had no terminal event — `turnBlocks.ts` states plainly that
  //: the dead-run exclusion holds only while `running` is false, so a session
  //: holding a crashed run span forever whenever an unrelated turn was open.
  //:
  //: `null` is the key for the one instant before a session exists. The turn
  //: that opens a session navigates mid-request, so that key is moved to the
  //: real id the moment the server hands one over — otherwise `running` would
  //: go false under the reader for the whole of the turn that created
  //: everything they are watching.
  const [runningIn, setRunningIn] = useState<ReadonlySet<string | null>>(
    () => new Set(),
  );
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
  const workspaceSidebar = useWorkspaceSidebar();
  const [panelOpen, setPanelOpen] = useState(false);
  const [directoryOpen, setDirectoryOpen] = useState(false);
  const queries = useQueryClient();

  //: Where the page is *now*, for continuations that were started under
  //: something else. The state above is derived rather than reset (`loadedFor`,
  //: `fault.scope`, `viewing.sessionId`) precisely because a render is too late
  //: -- and a promise resolving is later still, so a request that outlives the
  //: route it was made from cannot ask the closure it was created in.
  //:
  //: `useLayoutEffect`, not `useEffect`: it has to be true before anything a
  //: render started can resolve against it.
  const mounted = useRef(true);
  const shown = useRef({ identity, sessionId });
  useLayoutEffect(() => {
    mounted.current = true;
    shown.current = { identity, sessionId };
    return () => {
      mounted.current = false;
    };
  }, [identity, sessionId]);

  // A query rather than an effect, because two things invalidate it -- opening
  // a session and renaming one -- and both happen somewhere other than where
  // the list is rendered.
  const sessions = useQuery({
    queryKey: ["code-sessions", identity],
    queryFn: () => listCodeSessions(identity),
  });
  const known = useMemo(
    () => sessions.data?.sessions ?? [],
    [sessions.data?.sessions],
  );

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
  const running = runningIn.has(sessionId ?? null);
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
          //: Only into the session the page is showing. The route effect below
          //: aborts its own fetch when the session changes, but the reload at
          //: the end of a turn has no signal and no route to check -- it is
          //: addressed to the session the instruction was typed into, which
          //: may be minutes old by the time a coding turn comes back.
          //:
          //: Writing `loadedFor` unconditionally there did not show A's
          //: transcript under B. It showed *nothing*: `messages` is derived as
          //: `loadedFor === sessionId ? loadedMessages : []`, so landing A's
          //: id while the route says B collapsed the pane to
          //: 这个会话还是空的 over a session with a full history, and took the
          //: 工作区 count and the preview directory with it. Nothing recovered
          //: it either -- the route effect's deps had not changed and the
          //: orphan-reload effect returns early on exactly this mismatch, so
          //: the session stayed blank until the reader navigated away and back.
          if (shown.current.sessionId !== id) return;
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
          if (shown.current.sessionId !== id) return;
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
    const startedIn = sessionId ?? null;
    setRunningIn((held) => new Set(held).add(startedIn));
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
        // Rekeyed from `null` to the session that now exists, before the
        // navigation below moves the route onto it.
        setRunningIn((held) => {
          const next = new Set(held);
          next.delete(null);
          next.add(opened);
          return next;
        });
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
      setRunningIn((held) => {
        const next = new Set(held);
        next.delete(target ?? startedIn);
        return next;
      });
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
        // Same question as `reload`: a script that writes a file can finish
        // after the reader has moved on, and landing this session's id then
        // empties whichever session they are looking at now.
        if (shown.current.sessionId !== target) return;
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
      if (trimmed === "") return;
      if (known.find((held) => held.session_id === target)?.title === trimmed) return;
      await renameCodeSession(identity, target, trimmed);
      await queries.invalidateQueries({ queryKey: ["code-sessions", identity] });
    },
    [identity, known, queries],
  );

  const attach = useCallback(
    async (chosen: FileList | null) => {
      if (chosen === null || chosen.length === 0) return;
      // The session has to exist before a file can go in it, and the composer
      // is reachable before one does. Rather than open a session here -- which
      // would create one whose first act was not an instruction, leaving it
      // unnamed in the list. The start page keeps upload out of the way and
      // states this boundary beside the first instruction.
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
          if (shown.current.sessionId !== sessionId) return;
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
      const submittedIdentity = identity;
      try {
        await deleteCodeSession(identity, target);
        await queries.invalidateQueries({ queryKey: ["code-sessions", identity] });
        // A DELETE and a list refresh are two round trips, and the rail that
        // starts them is on screen the whole time -- so by the time this line
        // runs the reader may be somewhere else entirely, or somebody else
        // entirely. Both are refusals, not adjustments: navigating under a
        // principal who did not ask for it, or reporting one identity's
        // failure on another's page, is worse than the delete going quiet.
        if (!mounted.current || shown.current.identity !== submittedIdentity) return;
        // Only when the reader was looking at it -- asked of the route as it
        // stands now, not of the one the click was made under. The closure's
        // `sessionId` was the session open when the trash was clicked, and it
        // sent readers who had since opened another session back to /code for
        // nothing.
        if (shown.current.sessionId === target) await navigate("/code");
      } catch (cause: unknown) {
        if (!mounted.current || shown.current.identity !== submittedIdentity) return;
        // Scoped to where the reader is now for the same reason: `fault.scope`
        // is compared against the current route at render, so a failure filed
        // under the session they have left renders nowhere at all.
        setFault({ scope: shown.current.sessionId ?? null, text: describe(cause) });
      }
    },
    [identity, navigate, queries],
  );

  const rail = (
    <WorkspaceSidebarPortal>
      <CodeSessionRail
        known={known}
        mobileOpen={workspaceSidebar.drawerOpen}
        onCloseMobile={workspaceSidebar.close}
        onDelete={(target) => void remove(target)}
        onNew={() => {
          workspaceSidebar.close();
          void navigate("/code");
        }}
        onOpen={(target) => {
          workspaceSidebar.close();
          void navigate(`/code/${target}`);
        }}
        onRename={rename}
        renaming={renaming}
        sessionId={sessionId}
        setRenaming={setRenaming}
      />
    </WorkspaceSidebarPortal>
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
        {sessionId === undefined ? null : (
          <label
            className={`aw-code-attach ${uploading ? "is-busy" : ""}`}
            title="上传文件到工作区"
          >
            <Paperclip aria-hidden size={16} />
            <span className="aw-sr-only">
              {uploading ? "正在上传" : "上传文件"}
            </span>
            <input
              disabled={uploading}
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
        )}
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
          aria-label={running ? "正在处理" : "发送"}
          className="aw-button is-primary"
          disabled={running || instruction.trim() === ""}
          type="submit"
        >
          {running ? (
            <LoaderCircle aria-hidden className="aw-spin" size={17} />
          ) : (
            <ArrowUp aria-hidden size={17} />
          )}
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
      <div className="aw-code-page">
        {rail}
        <main className="aw-code-main">
          <div className="aw-code-start">
            <div className="aw-code-start-inner">
              <header className="aw-code-start-head">
                <IconButton
                  className="aw-code-mobile-sessions"
                  controls="workspace-sidebar-context"
                  expanded={workspaceSidebar.drawerOpen}
                  label="打开会话列表"
                  onClick={workspaceSidebar.open}
                >
                  <PanelLeft aria-hidden size={18} />
                </IconButton>
                <h1>开始编码</h1>
                <p>
                  描述目标开始。会话建立后可添加文件，Agent 会修改并验证结果。
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
      className={`aw-code-page${panelOpen ? " has-preview" : ""}`}
    >
      {rail}

      <main className="aw-code-main">
        <header className="aw-code-header">
          <IconButton
            className="aw-code-mobile-sessions"
            controls="workspace-sidebar-context"
            expanded={workspaceSidebar.drawerOpen}
            label="打开会话列表"
            onClick={workspaceSidebar.open}
          >
            <PanelLeft aria-hidden size={18} />
          </IconButton>
          <div className="aw-code-header-copy">
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

        {/* Same as Chat's transcript: not a live region. Announcing every
            step, file card and disclosure in a coding turn is a torrent,
            and a torrent is indistinguishable from silence. The approval
            section above says the one thing worth interrupting for. */}
        <section aria-label="编码会话" className="aw-code-transcript">
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
            {/* Announced, because this section is a sibling of the
                transcript rather than inside it: a turn that stops to ask
                permission produced no sound at all before this line. One
                sentence, not the questions themselves -- the questions
                are right here to read once the reader knows to look. */}
            <p aria-atomic="true" className="aw-sr-only" role="status">
              有 {pendingApprovals.length} 个调用等待你批准
            </p>
            {pendingApprovals.map((held) => (
              <article className="aw-code-approval" key={held.approval_id}>
                <h3>
                  {held.tool_name} 需要你批准
                  {held.risk === null ? null : (
                    <span
                      className="aw-code-approval-risk"
                      data-risk={held.risk}
                    >
                      {RISK_LABELS[held.risk] ?? held.risk}
                    </span>
                  )}
                </h3>
                <p className="aw-code-value">{held.argument_digest}</p>
                {held.risk !== null && UNREPEATABLE.has(held.risk) ? (
                  // The missing third button, explained where it is missing.
                  // Without this the reader sees two buttons where they saw
                  // three a moment ago and has to guess why.
                  <p className="aw-code-approval-note">
                    这一类调用每次都要单独问，不能一次答应整个会话。
                  </p>
                ) : null}
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
