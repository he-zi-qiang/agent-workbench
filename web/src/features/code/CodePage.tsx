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
import {
  ArrowUp,
  ClipboardList,
  Code2,
  LoaderCircle,
  PanelLeft,
  PanelRightOpen,
  Paperclip,
  UserCheck,
  Zap,
} from "lucide-react";
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
  getProject,
  listCodeSessions,
  listProjects,
  newIdempotencyKey,
  putCodeWorkspaceFile,
  renameCodeSession,
  setCodeSessionProject,
} from "../../api/client";
import type {
  ApprovalDecision,
  CodeSessionListResponse,
  CodeTurnApprovals,
  CodeTurnMode,
  MessageView,
  PendingApprovalView,
  ProjectFileEntryView,
  ProjectView,
  WorkspaceEntryView,
} from "../../api/types";
import { useIdentity } from "../../app/IdentityContext";
import {
  useWorkspaceSidebar,
  WorkspaceSidebarPortal,
} from "../../app/WorkspaceSidebar";
import { effectiveMediaType } from "../../components/media";
import { useStoredState } from "../../hooks/useStoredState";
import { ProjectPicker } from "../../components/ProjectPicker";
import { EmptyState, ErrorNotice, IconButton } from "../../components/ui";
import {
  ModeStarterPrompts,
  ModeStartHeader,
  submitTextareaOnEnter,
} from "../../components/ModeStart";
import { CodeSessionRail } from "./CodeSessionRail";
import { ProjectChooser } from "./ProjectChooser";
import { ProjectFileTree } from "./ProjectFileTree";
import { RunPanel } from "../../components/RunPanel";
import { buildRunTree } from "../../components/runTree";
import { CodeTurn } from "./CodeTurn";
import type { OpenedFile } from "./FilePreview";
import { PreviewPanel } from "./PreviewPanel";
import { buildTurnBlocks, projectWritesIn } from "./turnBlocks";
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
    prompt:
      "为下面的行为编写清晰的测试用例，覆盖正常路径、边界条件和失败情况：",
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

/**
 * 这一轮的权限，作为一条梯子上的三个位置。
 *
 * 界面上是一个控件，信封里是两半：`mode` 收紧的是工具清单（ADR-0079），
 * `approvals` 收紧的是「哪些风险要停在人面前」（ADR-087）。合成一个控件，是
 * 因为读者问的是一个问题——「这一轮能干到什么程度」——而这三档确实是有序的：
 * 不能改 < 改之前问我 < 直接改。Claude Code 的模式循环是同一个形状。
 *
 * 分成两个字段发出去，是因为服务端那两半各自有各自的不变量，而把它们并成一个
 * 四值枚举，只会让唯一读它的那个地方再拆一次。这张表是那次合并唯一存在的位置。
 *
 * 没有第四档。「什么都别问我」要拿掉的是 `destructive`——在这台机器上跑一条
 * 命令，而 ADR-077 说它跑之前要给人看见。这个控件只会往上收紧，收不松：一个
 * 想要比部署配置更少提问的人，要去改部署，不是改这一轮。
 */
type CodePermission = "plan" | "ask" | "act";

const TURN_OF: Readonly<
  Record<CodePermission, { mode: CodeTurnMode; approvals: CodeTurnApprovals }>
> = {
  plan: { mode: "plan", approvals: "standard" },
  ask: { mode: "act", approvals: "before_write" },
  act: { mode: "act", approvals: "standard" },
};

/**
 * 每一档说给读者的话。
 *
 * 三条都用同一个主语句式（「这一轮……」），因为它们是同一个问题的三个答案，
 * 而不是三件不同的事。副标题说的是**后果**，不是设置名——「只读」是设置名，
 * 「不会动任何文件」是读者要的那句。
 */
const PERMISSIONS: ReadonlyArray<{
  value: CodePermission;
  label: string;
  hint: string;
}> = [
  {
    value: "plan",
    label: "只做计划",
    hint: "这一轮只会读，不会动任何文件",
  },
  {
    value: "ask",
    label: "改前问我",
    hint: "这一轮可以改文件，但每次写入都会停下来等你允许",
  },
  {
    value: "act",
    label: "自动改动",
    hint: "这一轮可以直接改文件；只有不可撤销的操作才会停下来问你",
  },
];

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
  //: 这一轮的权限（ADR-0079 + ADR-087）。留在组件里而不是写进 URL 或
  //: localStorage：它是**一轮**的属性，回合起始就被冻进信封，一个跨会话记住
  //: 的开关会让「这一轮到底能不能写」变成一个读者要去别处查的问题。默认
  //: `act`，因为绝大多数请求就是要它去做。
  const [permission, setPermission] = useState<CodePermission>("act");
  //: 最近一轮计划的指令原文，用来支持「按这个计划执行」。存指令而不是存计划正文：
  //: 重发的是**同一个请求**，只是换成 act 模式——计划本身是散文，它不授权任何东西
  //: （ADR-0079 不变量 3），所以后面那一轮不该被它约束，也不该假装被它约束。
  const [planned, setPlanned] = useState<{ session: string; text: string } | null>(
    null,
  );
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
  const [fault, setFault] = useState<{
    scope: string | null;
    text: string;
  } | null>(null);
  const error =
    fault !== null && fault.scope === (sessionId ?? null) ? fault.text : null;
  const [approvals, setApprovals] = useState<PendingApprovalView[]>([]);
  const [opened, setOpened] = useState<OpenedFile | null>(null);
  const [renaming, setRenaming] = useState<string | null>(null);
  const [uploading, setUploading] = useState(false);
  const workspaceSidebar = useWorkspaceSidebar();
  // 展开还是收起，记在这台机器上。
  //
  // 和左边那条导航同一个道理：折叠是一次表态，不是每开一个会话都要重做一遍的
  // 动作。此前这是一个 `useState(false)` 的抽屉——每次进来都是关着的，而一个
  // 「我就是要一边读一边看文件」的人，每换一个会话就得再点一次。
  //
  // 也因此不再有 Escape 关闭：那是给盖住内容的浮层用的退路，而这一栏不盖任何
  // 东西。留着它的坏处是很具体的——在下面那个输入框里按 Escape（不少输入法和
  // 补全都用这个键）会把这一栏收起来，而且**记住**这次收起。
  const [panelOpen, setPanelOpen] = useStoredState("aw.code.panel.v1", false);
  const [directoryOpen, setDirectoryOpen] = useState(false);
  const queries = useQueryClient();
  // 哪个项目文件正被查看。只在这一层保存：它是「我在看哪个文件」，属于这次浏览，
  // 不属于会话——换个会话再回来，从头开始看是对的。
  //
  // 存的是整行而不是路径：预览要在取正文之前知道字节数才能拒绝一个太大的
  // 文件，而目录列表那一行本来就带着它（见 `ProjectFileTree` 的 `onOpenFile`）。
  const [openProjectFile, setOpenProjectFile] =
    useState<ProjectFileEntryView | null>(null);
  // 点开项目目录里的一个文件：它和会话产出共用右边那一栏，所以另一个要让位。
  // 两个都留着的话，那一栏得决定谁在上面，而读者刚点的那个显然应该在上面——
  // 与其在渲染时判断先后，不如在这里就只留一个。
  const openProjectFileAt = useCallback((entry: ProjectFileEntryView) => {
    setOpenProjectFile(entry);
    setOpened(null);
    setPanelOpen(true);
  }, [setPanelOpen]);
  // 起始屏选中的项目（ADR-074）。只在「还没有会话」时用得上——会话一旦存在，
  // 归属就在会话行上，读它比读这个 state 可靠：刷新页面之后 state 没了，行还在。
  const [startingIn, setStartingIn] = useState<ProjectView | null>(null);
  // 归属改完要把会话列表标脏：那份列表带着 project_id，而项目页读的是同一个
  // 事实。不刷新的话，切回来看到的是改之前的答案。
  const assignProject = async (projectId: string | null) => {
    if (sessionId === undefined) return;
    await setCodeSessionProject(identity, sessionId, projectId);
    await queries.invalidateQueries({ queryKey: ["code-sessions", identity] });
  };

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
    running &&
    pending !== null &&
    (pending.sessionId ?? sessionId) === sessionId
      ? pending.text
      : null;
  // Memoised, unlike the three above, only because `openByName` closes over it:
  // the `[]` branch is a fresh array every render, which would give that
  // callback a new identity on every frame the event stream delivers.
  const files = useMemo(
    () => (loadedFor === sessionId ? loadedFiles : []),
    [loadedFiles, loadedFor, sessionId],
  );

  // 这条流里被写过的项目文件（ADR-086）。派生的，和上面几个同一个理由。
  const projectWrites = useMemo(() => projectWritesIn(steps), [steps]);

  // 这个会话里跑过哪些运行，按谁派生谁（ADR-089）。
  //
  // 与 Work 页共用 `buildRunTree` 与 `RunPanel`——两边问的是同一个问题，而
  // 委派之后两边的步骤流都会交织两个 agent 的调用。从这里再发一个请求去问服务端
  // 是用第二个请求学第一个请求已经带回来的东西：`steps` 就是这条流的持久事件。
  //
  // 注意它读的是 `steps` 而不是收窄之后的那份：一个只剩被选中那一行的面板，
  // 会把「换一个运行去看」这件事本身拿掉。
  const runTree = useMemo(() => buildRunTree(steps), [steps]);
  // 选中哪个运行，`null` 表示全部。带着 sessionId，在渲染时比较而不是用 effect
  // 事后清除——与 Work 页那条同一个形状、同一个理由：一个 run id 属于一条流，
  // 带过去只会把下一个会话过滤成空的。
  const [runSelection, setRunSelection] = useState<{
    sessionId: string;
    runId: string;
  } | null>(null);
  const selectedRunId =
    runSelection !== null && runSelection.sessionId === sessionId
      ? runSelection.runId
      : null;
  const shownSteps = useMemo(
    () =>
      selectedRunId === null
        ? steps
        : steps.filter((event) => event.run_id === selectedRunId),
    [steps, selectedRunId],
  );

  // Which run is live is derived inside `buildTurnBlocks`, from the run
  // bookkeeping in the events themselves rather than from anything this
  // component remembers about the moment it pressed send.
  const { blocks, orphanRuns, orphanRunIds } = buildTurnBlocks({
    messages,
    events: shownSteps,
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
              (message) =>
                message.role === "user" && message.text === held.text,
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

  const send = useCallback(
    async (turnPermission: CodePermission = permission, override?: string) => {
      const { mode: turnMode, approvals: turnApprovals } =
        TURN_OF[turnPermission];
      const text = (override ?? instruction).trim();
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
        // 立刻归到选中的项目下，而不是等一次单独的保存动作。ADR-074 §7.1 那条
        // 不变量是「每个编码会话都属于一个有目录的项目」——中间存在一个还没归属
        // 的瞬间，就等于这条不变量只是通常成立。
        if (startingIn !== null) {
          await setCodeSessionProject(
            identity,
            created.session_id,
            startingIn.project_id,
          );
        }
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
                // 归属写在这一行上，不留 null。上面那次 `setCodeSessionProject`
                // 刚把它归到这个文件夹下，服务端那份会在下面那次 invalidate 之后
                // 替掉这一行——中间这段时间，侧栏是按文件夹收窄的，一行 project_id
                // 是 null 的会话会被它自己刚开的那个文件夹过滤掉。也就是说：不写
                // 这一句，这个乐观插入就白做了，而它存在的全部理由正是「一轮编码
                // 要跑几分钟，那几分钟里读者看着的会话不该是列表里唯一没有的那个」。
                project_id: startingIn?.project_id ?? null,
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
      const answer = await askCode(
        identity,
        target,
        text,
        newIdempotencyKey("code"),
        turnMode,
        turnApprovals,
      );
      // Remembered only on the way out of a plan turn that produced something.
      // A plan turn that failed has nothing to run, and an act turn clears it:
      // the button is an offer to run *the plan just made*, and leaving it up
      // after ordinary work would offer to re-run something older than what
      // the reader is looking at.
      setPlanned(
        turnMode === "plan" && answer.status === "completed"
          ? { session: target, text }
          : null,
      );
      // A turn that dies on its budget appends no assistant message at all
      // (the server declines to invent one), so without this the transcript
      // shows the instruction and then silence -- which reads as "it cannot
      // do anything any more", not as "that turn ran out".
      if (answer.status !== "completed") {
        setFault({
          scope: target,
          text: stopNote(
            answer.stop_reason,
            answer.error_code,
            answer.error_message,
          ),
        });
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
    // `startingIn` 在依赖里，而不是被省掉：这个回调**读**它（新会话就是靠它归到
    // 项目下的），漏掉依赖会让它捕获一个旧的 `null`——选完文件夹立刻发送，会话
    // 就不会被归属，而 ADR-074 §7.1 那条不变量只是通常成立。lint 报的正是这个。
    },
    [
      identity,
      instruction,
      navigate,
      permission,
      queries,
      reload,
      running,
      sessionId,
      startingIn,
    ],
  );

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
      // 让位给它，理由同 `openProjectFileAt`：一栏，一个文件。
      setOpenProjectFile(null);
      setOpened({
        sessionId,
        name: file.name,
        mediaType: file.media_type,
        sizeBytes: file.size_bytes,
      });
    },
    [sessionId, setPanelOpen],
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
      if (known.find((held) => held.session_id === target)?.title === trimmed)
        return;
      await renameCodeSession(identity, target, trimmed);
      await queries.invalidateQueries({
        queryKey: ["code-sessions", identity],
      });
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
        await queries.invalidateQueries({
          queryKey: ["code-sessions", identity],
        });
        // A DELETE and a list refresh are two round trips, and the rail that
        // starts them is on screen the whole time -- so by the time this line
        // runs the reader may be somewhere else entirely, or somebody else
        // entirely. Both are refusals, not adjustments: navigating under a
        // principal who did not ask for it, or reporting one identity's
        // failure on another's page, is worse than the delete going quiet.
        if (!mounted.current || shown.current.identity !== submittedIdentity)
          return;
        // Only when the reader was looking at it -- asked of the route as it
        // stands now, not of the one the click was made under. The closure's
        // `sessionId` was the session open when the trash was clicked, and it
        // sent readers who had since opened another session back to /code for
        // nothing.
        if (shown.current.sessionId === target) await navigate("/code");
      } catch (cause: unknown) {
        if (!mounted.current || shown.current.identity !== submittedIdentity)
          return;
        // Scoped to where the reader is now for the same reason: `fault.scope`
        // is compared against the current route at render, so a failure filed
        // under the session they have left renders nowhere at all.
        setFault({
          scope: shown.current.sessionId ?? null,
          text: describe(cause),
        });
      }
    },
    [identity, navigate, queries],
  );

  // 这段会话所属项目的目录，没有就是 null。ADR-072 的 `root_path` 可空，而空是
  // 正常状态——绝大多数会话没有目录，树整块不出现，而不是出现一个空的树。
  const heldProjectId = known.find(
    (one) => one.session_id === sessionId,
  )?.project_id;
  const project = useQuery({
    queryKey: ["project", identity, heldProjectId],
    queryFn: ({ signal }) =>
      getProject(identity, heldProjectId as string, signal),
    enabled: heldProjectId != null,
  });
  const projectRoot = project.data?.root_path ?? null;

  // 目录树跟着写入走。
  //
  // 树是按层取的，每一层一个 `["project-files", identity, projectId, path]`
  // 查询，`staleTime` 默认，但没有人去碰它——所以在这一行之前，agent 刚写出
  // 来的文件要等到读者手动折叠再展开那一层才会出现。屏幕上的样子是「产物没有
  // 落到文件夹里」，而实际上文件在磁盘上，只有那一份缓存不知道。
  //
  // 按前缀失效，不按具体那一层：写入的是 `docs/a/b.md` 时该刷新的是 `docs/a`，
  // 而算出那个前缀等于在客户端做路径算术——`ProjectFileTree` 的注释里为另一
  // 件事拒绝过同一种做法。整棵树的层数是读者展开过的那几层，重取它们便宜得
  // 多，也不会漏掉「这次写入新建了一个目录」这种连父层都变了的情况。
  //
  // 触发条件是这个集合**变了**，不是「有事件到了」：事件在一轮里以每秒几十条
  // 的速度来，而写入一轮通常只有几次。`joined` 是比较用的那个值，不是渲染用的。
  const joinedWrites = projectWrites.join("\n");
  const refreshedFor = useRef("");
  useEffect(() => {
    if (heldProjectId == null || joinedWrites === refreshedFor.current) return;
    refreshedFor.current = joinedWrites;
    void queries.invalidateQueries({
      queryKey: ["project-files", identity, heldProjectId],
    });
  }, [heldProjectId, identity, joinedWrites, queries]);


  // 读者此刻在哪个文件夹里。
  //
  // 会话上写着的那个优先，起始屏刚选的那个次之——顺序不能反过来：`startingIn`
  // 是这个标签页里选过的最后一个文件夹，它不会因为读者打开了另一个文件夹下的
  // 会话就消失，所以让它压过会话自己的归属，会让侧栏把 B 的会话列在 A 的名下。
  const currentProjectId = heldProjectId ?? startingIn?.project_id ?? null;

  // 会话列表按文件夹收窄（ADR-074：文件夹就是项目）。
  //
  // 收窄之前，这一栏列的是这个人**所有**的编码会话，而屏幕上其余的一切——目录树、
  // 起始屏那句「在 … 里编码」、agent 实际读写的文件——说的都是一个文件夹。一栏
  // 里两种范围，读者要自己在每一行上判断「这条是不是这儿的」。
  //
  // 在本地过滤，而不是给 `/v1/code/sessions` 加一个 project_id 参数：那个接口
  // 一次给的是这个人最近的若干段会话（服务端上限 200），列表本来就是「最近」
  // 而不是「全部」，在这上面再加一个服务端过滤，只会让「最近」变成两个意思。
  // 代价说在下面那行字里——过滤掉了几条，就说几条。
  const [scopedToProject, setScopedToProject] = useState(true);
  const scoping = currentProjectId !== null && scopedToProject;
  const inThisProject = useMemo(
    () =>
      currentProjectId === null
        ? known
        : known.filter((held) => held.project_id === currentProjectId),
    [currentProjectId, known],
  );
  const visibleSessions = scoping ? inThisProject : known;
  const outsideCount = known.length - inThisProject.length;

  // 文件夹名，只为「全部会话」那一档准备。
  //
  // 取的是同一个 `["projects", identity]`——`ProjectChooser` 和 `ProjectPicker`
  // 已经在取它，所以这是第三个**订阅者**而不是第三次请求：react-query 认的是
  // 键。`enabled` 挂在那一档上，因为收窄状态下没有人会读这张表，而一份没人读
  // 的列表不值得在每次打开会话时都去取一次。
  const projectList = useQuery({
    enabled: !scoping,
    queryKey: ["projects", identity],
    queryFn: ({ signal }) => listProjects(identity, { signal }),
  });
  const projectNames = useMemo(
    () =>
      new Map(
        (projectList.data?.projects ?? []).map((one) => [
          one.project_id,
          one.name,
        ]),
      ),
    [projectList.data],
  );

  const rail = (
    <WorkspaceSidebarPortal>
      {/* 一个纵向的壳，而不是把两块直接丢进 portal。portal 的容器是
          `flex-direction: row`——第一版没有这层，于是文件树和会话列表被并排
          放进一条 260px 宽的侧栏里，列表整个被挤出了可视区。 */}
      <div className="aw-code-sidebar-stack">
        {heldProjectId != null && projectRoot !== null ? (
          <ProjectFileTree
            onOpenFile={openProjectFileAt}
            projectId={heldProjectId}
            rootPath={projectRoot}
            selectedPath={openProjectFile?.path ?? null}
            writtenPaths={projectWrites}
          />
        ) : null}
        <CodeSessionRail
          known={visibleSessions}
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
          onToggleScope={() => {
            setScopedToProject((held) => !held);
          }}
          outsideCount={outsideCount}
          projectNames={projectNames}
          renaming={renaming}
          runningIds={runningIn}
          scoped={scoping}
          sessionId={sessionId}
          setRenaming={setRenaming}
        />
      </div>
    </WorkspaceSidebarPortal>
  );

  // One composer for both shapes of the page: attaching a file is part of
  // asking, so the control sits where the asking happens. The label wraps a
  // hidden input rather than a button clicking one through a ref -- the one
  // control shape a keyboard and a screen reader both already understand.
  const composer = (
    <form
      aria-busy={running}
      className="aw-code-composer"
      onSubmit={(event) => {
        event.preventDefault();
        void send();
      }}
    >
      {planned === null || planned.session !== sessionId ? null : (
        <div className="aw-code-plan-offer">
          <span>上面是一份计划，还没有动过任何文件。</span>
          <button
            className="aw-button"
            disabled={running}
            onClick={() => {
              // 同一条指令重发一次，模式换成 act。**不是**把计划正文发过去：
              // 计划是散文，它不授权任何东西（ADR-0079 不变量 3），后面这一轮
              // 拿到的是它自己的信封，和没有先计划过时一模一样。
              void send("act", planned.text);
            }}
            type="button"
          >
            按这个计划执行
          </button>
        </div>
      )}
      <div className="aw-code-composer-row aw-mode-composer-card">
        {/* 一个三档的选择器，不是一个「只做计划」的复选框。
            复选框只答得出一个是非题，而读者要问的是三档里的哪一档——它把
            「谁来拍板一次写入」整个留在了界面之外：没有这个控件的时候，
            那件事由部署配置决定，而屏幕上没有任何地方说得出它是什么。
            `aw-segmented` 是这份代码里已有的那个形状（`HtmlPreview` 的
            渲染／源码用的是同一个类），因为这三个也是「同一件事的几种看法，
            选一个」，不是三个各自独立的开关。 */}
        <div
          aria-label="这一轮的权限"
          className="aw-segmented aw-code-permission"
          role="group"
        >
          {PERMISSIONS.map((choice) => (
            <button
              aria-pressed={permission === choice.value}
              className={permission === choice.value ? "is-active" : ""}
              disabled={running}
              key={choice.value}
              onClick={() => {
                setPermission(choice.value);
              }}
              title={choice.hint}
              type="button"
            >
              {choice.value === "plan" ? (
                <ClipboardList aria-hidden size={13} />
              ) : null}
              {choice.value === "ask" ? <UserCheck aria-hidden size={13} /> : null}
              {choice.value === "act" ? <Zap aria-hidden size={13} /> : null}
              {choice.label}
            </button>
          ))}
        </div>
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
          aria-keyshortcuts="Enter"
          disabled={running}
          enterKeyHint="send"
          id="aw-code-instruction"
          maxLength={8192}
          onChange={(event) => {
            setInstruction(event.target.value);
          }}
          onKeyDown={submitTextareaOnEnter}
          placeholder="描述你要做的事"
          ref={instructionRef}
          rows={3}
          value={instruction}
        />
        <button
          aria-label={running ? "正在处理" : "发送"}
          className="aw-button is-primary aw-mode-send"
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
    // 没选文件夹就没有起始屏。这是 Code 的门而不是一个可跳过的设置项：允许
    // 「先开始、回头再选」会让「产物存哪了」重新有两个答案。
    if (startingIn === null) {
      return (
        <div className="aw-code-page">
          {rail}
          <main className="aw-code-main is-start">
            <ProjectChooser onChoose={setStartingIn} />
          </main>
        </div>
      );
    }
    return (
      <div className="aw-code-page">
        {rail}
        <main className="aw-code-main is-start">
          <div className="aw-code-start aw-mode-start">
            <div className="aw-code-start-inner">
              <ModeStartHeader
                action={
                  <IconButton
                    className="aw-code-mobile-sessions"
                    controls="workspace-sidebar-context"
                    expanded={workspaceSidebar.drawerOpen}
                    label="打开会话列表"
                    onClick={workspaceSidebar.open}
                  >
                    <PanelLeft aria-hidden size={18} />
                  </IconButton>
                }
                description={`在 ${startingIn.root_path ?? startingIn.name} 里编码。Agent 读写的是这个文件夹里的真实文件。`}
                title="开始编码"
              />
              {error === null ? null : <ErrorNotice message={error} />}
              {composer}
              <ModeStarterPrompts
                disabled={running}
                items={CODE_STARTERS}
                label="编码任务起点"
                onChoose={(prompt) => {
                  setInstruction(prompt);
                  window.requestAnimationFrame(() =>
                    instructionRef.current?.focus(),
                  );
                }}
              />
            </div>
          </div>
        </main>
      </div>
    );
  }

  const held = known.find((one) => one.session_id === sessionId);
  const title = held?.title;

  // 右边那一栏此刻在显示什么。项目文件优先，理由写在 `openProjectFileAt`：
  // 两个来源共用一栏，后点开的那个说了算，而页面在打开任一个时清掉另一个。
  const panelProjectFile =
    heldProjectId != null && openProjectFile !== null
      ? {
          projectId: heldProjectId,
          path: openProjectFile.path,
          // `?? 0` 到不了：`size_bytes` 只在目录上是 null（`ports/project_files.py`
          // 说的是「目录没有大小，不是大小为零」），而目录点开是展开，不是打开。
          // 写成兜底而不是断言，是因为这里没有值得为它抛异常的事——真到了那一
          // 步，0 会让预览照常打开，而服务端 2 MiB 的读上限仍然在。
          sizeBytes: openProjectFile.size_bytes ?? 0,
        }
      : null;
  // 两个条件：读者要它展开，而且这一栏有东西可显示。
  //
  // 后一个不是保险，是这一栏「记得住」带来的必然情形：展开状态跨会话保留，而
  // 大多数会话在第一轮之前一个文件都没有。少了它，每开一段新会话都会先看到
  // 一条 440px 宽、只写着「工作区全部文件（0）」的空栏——那是一句真话，但它
  // 占的宽度和一屏代码一样多。有东西可显示的那一刻它自己回来，读者不用再表态
  // 一次。
  const panelShown =
    panelOpen && (files.length > 0 || panelProjectFile !== null);

  return (
    // 没有 `has-panel` 之类的类名，这一点是刻意的。预览栏展开时多出来的那一列
    // 是一条隐式网格轨道，宽度由 `.aw-code-panel` 自己的 width 定——收起时它整个
    // 不渲染，轨道跟着消失。写一个类名让 CSS 去改 `grid-template-columns`，就要
    // 求两层样式加两个断点一共四处都记得多写一条轨道；`has-preview` 当年正是这么
    // 变成死类的（app.css 那半改了，minimal-theme 那半没改，而它加载得更晚）。
    <div className="aw-code-page">
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
            {/* 归属长在这一段自己的头部，和对话页同一个位置和同一个组件。
                一段编码会话此前是这三个工作区里唯一不能归到项目下的——
                服务端一直允许（它就是一行 mode="code" 的会话），只是界面
                没有给出说这句话的地方，于是「项目收着同一件事做过的东西」
                在编码这一半是空的。 */}
            {held === undefined ? null : (
              <ProjectPicker
                identity={identity}
                label="这段编码会话属于哪个项目"
                onAssign={assignProject}
                projectId={held.project_id}
              />
            )}
          </div>
          {/* The way to the whole working set, including everything no card
              could account for. Absent entirely when there is nothing in it.

              收起来之后，这颗按钮就是把那一栏叫回来的地方——所以它是一个
              带 `aria-expanded` 的开关，不是一个只会打开的按钮。展开着的时候
              再点一下是收起：一个点开了就再也不管用的控件，读者会以为它坏了。 */}
          {files.length === 0 ? null : (
            <button
              aria-controls="aw-code-panel"
              aria-expanded={panelShown}
              className={`aw-button aw-code-workspace-entry ${
                panelShown ? "is-open" : ""
              }`}
              onClick={() => {
                if (panelShown) {
                  setPanelOpen(false);
                  return;
                }
                setPanelOpen(true);
                setDirectoryOpen(true);
              }}
              type="button"
            >
              <PanelRightOpen aria-hidden size={15} />
              工作区 {files.length}
            </button>
          )}
        </header>

        {/* Same as Chat's transcript: not a live region. Announcing every
            step, file card and disclosure in a coding turn is a torrent,
            and a torrent is indistinguishable from silence. The approval
            section above says the one thing worth interrupting for. */}
        <section aria-label="编码会话" className="aw-code-transcript">
          {/* 只在真的发生过委派时出现，与 Work 页同一条规则：没派生过的会话里
              每个运行都是这一回合本身，再来一块面板只是家具。 */}
          <RunPanel
            onSelect={(runId) => {
              setRunSelection(
                runId === null || sessionId === undefined
                  ? null
                  : { sessionId, runId },
              );
            }}
            roots={runTree}
            selectedRunId={selectedRunId}
          />
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
                  key={block.key}
                  liveThinking={block.live ? thinking : ""}
                  liveThinkingCallId={block.live ? thinkingCallId : ""}
                  liveAnswer={block.live ? answer : ""}
                  onOpen={openByName}
                  openedName={viewing?.name ?? null}
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
                {/* 先给要批准的东西，再给它的身份。顺序就是理由：人同意的是
                    这次调用的参数，而摘要是给事后对着事件流核对的人用的。
                    在这一行之前，卡片上只有那 64 个十六进制字符——它把
                    `rm -rf .` 和 `ls` 问成了同一个问题，而 Code 会话一旦能在
                    本机跑命令，参数就不再是效果的细节，它本身就是效果。 */}
                <pre className="aw-code-approval-preview">
                  {held.approval_preview}
                </pre>
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

      {panelShown ? (
        <>
          {/* 只在窄屏看得见：宽屏由 `@media (width >= 901px)` 把它关掉。
              宽屏上这一栏是一条真的列，谁也没盖住，一层点了就收起的透明遮罩
              只会让人误点；窄屏上三列排不下，它退回浮层，那时候「点旁边关掉」
              又是必须有的。 */}
          <button
            aria-label="收起预览栏"
            className="aw-drawer-backdrop"
            onClick={() => {
              setPanelOpen(false);
            }}
            type="button"
          />
          <PreviewPanel
            directoryOpen={directoryOpen}
            files={files}
            identity={identity}
            onCollapse={() => {
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
            projectFile={panelProjectFile}
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
 *
 * `StopReason` alone turned out not to be enough vocabulary (ADR-0084). Every
 * provider failure arrives here as `"error"`, so the last branch was rendering
 * `这一轮没有跑完（error）` for an exhausted account, a rejected key, a retired
 * model id and a 500 alike -- four different things to go do. The failure's
 * own code and message are now passed alongside, and read first.
 */
function stopNote(
  reason: string,
  errorCode?: string | null,
  errorMessage?: string | null,
): string {
  if (errorCode === "provider_account_rejected") {
    // Ahead of the `reason` branches because this one contradicts them. The
    // other notes end with 「直接说下一步就能继续」, and that advice is wrong
    // here for the same reason it was wrong for `context_limit`: the next turn
    // in this session calls the same account and fails the same way. Nothing
    // inside this console can fix it.
    return "模型服务拒绝了这个部署的账号：余额用尽，或者密钥失效。已完成的改动都在工作区里；重试没有用，要先去模型服务商那边充值或者换一把密钥。";
  }
  if (reason === "deadline") {
    return "这一轮到时间停下了。已完成的改动都在工作区里，直接说下一步就能继续。";
  }
  if (reason === "max_steps" || reason === "max_tool_calls") {
    return "这一轮把步数用完了。已完成的改动都在工作区里，直接说下一步就能继续。";
  }
  if (reason === "token_budget" || reason === "cost_budget") {
    return "这一轮把预算用完了。已完成的改动都在工作区里，直接说下一步就能继续。";
  }
  if (reason === "context_limit") {
    // ADR-0080。这一条和上面几条不一样：它不是"额度用完了"，而是"这段对话本身
    // 长到装不下了"，所以「直接说下一步就能继续」在这里是错的建议——同一个会话
    // 的下一轮会带着同样长的历史再撞一次。
    return "这一轮的对话长到模型装不下了。已完成的改动都在工作区里；开一个新会话继续，或者把要做的事拆小一点。";
  }
  if (reason === "cancelled") {
    return "这一轮被取消了。已完成的改动都在工作区里。";
  }
  // The message, then the code, then the bare stop reason. `explainFailure`
  // settled this rule for Task details and it holds here: a string this
  // function has no words for is still the most specific thing anyone has,
  // and `error` is the least specific thing there is. The message is the
  // server's English -- shown rather than dropped, because a reader chasing a
  // provider fault would otherwise have to go read the event log to learn
  // which one it was.
  const detail = errorMessage ?? errorCode ?? reason;
  return `这一轮没有跑完（${detail}）。已完成的改动都在工作区里，直接说下一步就能继续。`;
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
