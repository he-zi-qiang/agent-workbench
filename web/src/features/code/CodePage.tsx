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
  Folder as FolderIcon,
  LoaderCircle,
  Monitor as MonitorIcon,
  PanelLeft,
  PanelRightOpen,
  Rocket,
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
import { useNavigate, useParams, useSearchParams } from "react-router-dom";
import {
  askCode,
  createCodeSession,
  decideCodeApproval,
  deleteCodeSession,
  deleteProjectFile,
  downloadCodeWorkspaceFile,
  getCodeApprovals,
  getCodeHistory,
  getCodeTools,
  getDeploymentCapabilities,
  getCodeWorkspace,
  getProject,
  listCodeSessions,
  listProjectFiles,
  listProjects,
  moveProjectFile,
  openProjectFileInBrowser,
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
import { COMMANDS_SUBMENU, ComposerMenu } from "./ComposerMenu";
import { SessionMenu } from "./SessionMenu";
import { RISK_LABELS, UNREPEATABLE } from "./toolVocabulary";
import {
  describeFolderUpload,
  MAX_WORKSPACE_ENTRIES,
  planFolderUpload,
} from "./workspaceNames";
import { CodeReach } from "./CodeReach";
import { ProjectChooser } from "./ProjectChooser";
import { ProjectFileTree } from "./ProjectFileTree";
import { RunPanel } from "../../components/RunPanel";
import { buildRunTree } from "../../components/runTree";
import { CodeTurn } from "./CodeTurn";
import { fileKey as workspaceFileKey } from "./FilePreview";
import type { OpenedFile } from "./FilePreview";
import { TurnUsage, sumTurnUsage } from "../../components/TurnUsage";
import { PreviewPanel, type OpenedProjectFile } from "./PreviewPanel";
import { pageAmong, parentOf } from "./previewIntent";
import { buildTurnBlocks, projectWritesIn } from "./turnBlocks";
import { useCodeStream } from "./useCodeStream";
import { stopNote } from "./stopNote";

/** How often to ask what the agent is stopped on, while it is working. */
const APPROVAL_POLL_MS = 1000;

const CODE_STARTERS = [
  {
    title: "规划一个实现",
    prompt: "请先拆解这个功能的实现方案，说明关键决策、风险和验证方式：",
    outcome: "产出：一份实现方案，不改任何文件",
  },
  {
    title: "生成一个文件",
    prompt: "根据下面的要求生成一个完整文件，并检查内容是否可以直接使用：",
    outcome: "产出：写进这个文件夹的一个新文件，右栏可以打开检查",
  },
  {
    title: "编写测试用例",
    prompt:
      "为下面的行为编写清晰的测试用例，覆盖正常路径、边界条件和失败情况：",
    outcome: "产出：一个测试文件，以及它跑出来的结果",
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
 * 第四档「放手做」是 ADR-0116 加的，而且它没有推翻上一段的道理，是把它的前提
 * 用尽了：ADR-0115 之后，容器栈上的命令跑在只挂项目文件夹的 runner 容器里，
 * 「在这台机器上跑一条命令」在那条路上不再成立；而这一档也没有从风险表里拿掉
 * `destructive`——服务端那张表照旧只加不减——它是把那道门预先答了，除了几种会
 * 毁掉工作的命令形状。所以它只在目录（`CodeToolsResponse.unattended_available`）
 * 说提供的时候才画出来：原生路径上的读者看到的仍然是三档，一字不差。
 */
type CodePermission = "plan" | "ask" | "act" | "auto";

const TURN_OF: Readonly<
  Record<CodePermission, { mode: CodeTurnMode; approvals: CodeTurnApprovals }>
> = {
  plan: { mode: "plan", approvals: "standard" },
  ask: { mode: "act", approvals: "before_write" },
  act: { mode: "act", approvals: "standard" },
  auto: { mode: "act", approvals: "unattended" },
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
    hint: "这一轮每一次写入、每一条命令都先问你",
  },
  {
    value: "act",
    label: "自动改动",
    hint: "这一轮写入不问，命令先问你",
  },
  {
    value: "auto",
    label: "放手做",
    hint: "这一轮写入和命令都不问，只有会毁掉工作的几种命令例外，你事后看记录",
  },
];

/**
 * 一台部署只画三档（2026-09-13，用户：「四种权限有交叉请取舍」）。
 *
 * 四档各有各的问题（分别对应 Claude Code 的 plan / default / acceptEdits /
 * bypass），但读者看到的是四句都以「这一轮……」开头、后两句都说「只有……例外」
 * 的话——分不清「自动改动」和「放手做」差在哪，是这四档并排的代价。取舍的
 * 规矩：第三档永远是「这台部署允许的最自动的那一档」。命令跑进 runner 容器的
 * 部署上它是「放手做」，「自动改动」不画——正是那一档在这条路上产生了十张审批卡；
 * 原生路径上「放手做」不存在，第三档就是「自动改动」。于是每一台部署都是
 * 只读 / 都问 / 最自动三档，而不是四档里挑。
 */
function offeredPermissions(unattendedOffered: boolean) {
  return PERMISSIONS.filter((choice) =>
    choice.value === "auto"
      ? unattendedOffered
      : choice.value === "act"
        ? !unattendedOffered
        : true,
  );
}

/**
 * 按下的那一颗一定是画出来的三颗之一（ADR-0117 §2.4 补记）。
 *
 * `offeredPermissions` 取舍的是画几颗，缺省却还是 `act`——而提供「放手做」的部署上
 * `act` 不画。2026-09-13 在 Compose 栈上打开一段会话：三颗的 `aria-pressed` 全是
 * false，不点任何一颗就发出去的一轮是一档看不见的「自动改动」，写入不问、命令先问，
 * 正是那一节取舍掉的十张审批卡。所以「去做」这个槽位跟着第三档走：`act` 与 `auto`
 * 在不画自己的部署上换成对方。计划和改前问我每台部署都画，原样不动。
 *
 * 换的是发出去的那一档，不是存着的那一份：两份「提不提供」的答案都还没到时画的是
 * 「自动改动」，答案一到，同一个位置上换成「放手做」，读者不用再点一次。
 */
function drawnPermission(
  held: CodePermission,
  unattendedOffered: boolean,
): CodePermission {
  if (held === "act" && unattendedOffered) return "auto";
  if (held === "auto" && !unattendedOffered) return "act";
  return held;
}

/** The three answers, and the one that is not always offered. */
const DECISIONS: { decision: ApprovalDecision; label: string }[] = [
  { decision: "approve_once", label: "允许一次" },
  { decision: "approve_for_session", label: "本会话都允许" },
  { decision: "deny", label: "拒绝" },
];

/* `RISK_LABELS` 与 `UNREPEATABLE` 搬去了 `toolVocabulary.ts`：待批准那张卡说的是
   一次已经提议了的调用，输入框菜单里那份清单说的是下一轮**可能**发生的调用
   （ADR-096），而两处说的是同一件事。抄成两份的话，第二份会在有人加一档风险的
   那天开始和第一份不一样。 */

export function CodePage() {
  const { identity } = useIdentity();
  const navigate = useNavigate();
  const { sessionId } = useParams<{ sessionId: string }>();
  const [searchParams, setSearchParams] = useSearchParams();

  const [loadedMessages, setMessages] = useState<MessageView[]>([]);
  const [loadedFiles, setFiles] = useState<WorkspaceEntryView[]>([]);
  //: Which session the two above were loaded for. Without it the page had to
  //: empty them from an effect when the session changed, which is a render too
  //: late -- the previous session's transcript was on screen for a frame, and
  //: for the whole of the next session's fetch.
  const [loadedFor, setLoadedFor] = useState<string | null>(null);
  const [instruction, setInstruction] = useState("");
  //: 输入框下面那颗「+」开着没有，以及开着的时候展开的是哪一栏。
  //:
  //: 提到这里而不是留在 `Menu` 里，是为了那条 `/`：在空输入框里打一个斜杠，开的
  //: 是同一个菜单的同一栏，而不是第二份长得像它的清单。两份清单会分叉，而它们说的
  //: 是同一组东西。
  const [menuOpen, setMenuOpen] = useState(false);
  const [menuSubmenu, setMenuSubmenu] = useState<string | null>(null);
  //: 页头的标题此刻是不是一个输入框。左栏那一行有它自己的一份（那是列表行的动作），
  //: 而这一份归页头，因为左栏在窄屏上是抽屉、在宽屏上折得起来——一颗按下去之后什么
  //: 都没发生的「重命名」是最坏的一种反馈。
  const [headerRenaming, setHeaderRenaming] = useState(false);
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
  // `null` = 读者还没表过态。表过之后一直听读者的；没表过时，有项目目录的会话
  // 默认**开着**——文件夹的结构是这段会话一开始就定下的事实，不该等人先去找一个
  // 开关。之前默认关着，于是「文件夹内容在右边」这件事对新会话等于不存在。
  const [panelChoice, setPanelChoice] = useStoredState<boolean | null>(
    "aw.code.panel.v2",
    null,
  );
  // 「工作区全部文件」那一节开不开。`null` 是「读者还没表过态」，不是「收起」。
  //
  // 右栏停在哪一张标签。`null` = 读者还没表过态，由 `PreviewPanel` 按「有没有
  // 打开的文件」自己落一个默认。
  //
  // 三态而不是一个写死的初值：读者一旦点过任何一张，这个默认就不该再动他；而
  // 在他点之前，「刚点开一个文件」和「什么都没开」要落在不同的地方。
  const [panelTab, setPanelTab] = useState<string | null>(null);
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
  const openProjectFileAt = useCallback(
    (entry: ProjectFileEntryView, options?: { expand?: boolean }) => {
      setOpenProjectFile(entry);
      setOpened(null);
      // 读者点的那一次要展开；自动弹的那一次不要——收起过这一栏是一次表过的态，
      // 而「agent 写出了一个页面」不是推翻它的理由。那时只把文件放好，读者下次
      // 展开就看见它（`panelChoice` 的三态在上面）。
      if (options?.expand !== false) setPanelChoice(true);
      // 点开一个文件就跳到「预览」那一张。写在这里而不是让面板按「有没有打开的
      // 文件」自己推断：读者可能刚刚亲手点到「本次会话」那一张，而 `tab` 一旦有
      // 值就压过面板的默认——不在这里说一句，点开的文件会安静地待在一张没人看的
      // 标签后面。
      setPanelTab("preview");
    },
    [setPanelChoice],
  );
  // 删掉、改名右栏里打开的那个项目文件（2026-09-13，用户：「文件夹中的文件也
  // 不可以删除」）。走的是 `project_delete` / `project_move` 同一个 store，所以
  // 拒绝的口径也一样：目录不删，目标不覆盖。成功之后目录树按项目键失效重取，
  // 打开的那个文件跟着关掉（删）或换成新路径（改名）。`window.confirm` 而不是
  // 自己的对话框，理由和删除会话那一处相同。
  const deleteOpenedFile = useCallback(
    async (file: OpenedProjectFile) => {
      if (
        !window.confirm(
          `删除 ${file.path}？这会删掉磁盘上的真实文件，没有回收站。`,
        )
      ) {
        return;
      }
      try {
        await deleteProjectFile(identity, file.projectId, file.path);
        setOpenProjectFile(null);
        await queries.invalidateQueries({
          queryKey: ["project-files", identity, file.projectId],
        });
      } catch (cause) {
        setFault({ scope: sessionId ?? null, text: describe(cause) });
      }
    },
    [identity, queries, sessionId],
  );
  const renameOpenedFile = useCallback(
    async (file: OpenedProjectFile) => {
      const target = window
        .prompt("新的路径（相对项目根，可以带目录）", file.path)
        ?.trim();
      if (target === undefined || target === "" || target === file.path) return;
      try {
        const entry = await moveProjectFile(
          identity,
          file.projectId,
          file.path,
          target,
        );
        setOpenProjectFile(entry);
        await queries.invalidateQueries({
          queryKey: ["project-files", identity, file.projectId],
        });
      } catch (cause) {
        setFault({ scope: sessionId ?? null, text: describe(cause) });
      }
    },
    [identity, queries, sessionId],
  );
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

  // 文件夹和浏览器那一张互通（ADR-0120，用户：「浏览器要和文件夹里的文件进行
  // 互通」）。一头是右栏里点开的 `.html`「在浏览器中打开」：服务端把项目 id 和
  // 相对路径拼成浏览器容器里的 `file://` 地址打开它，然后跳到「浏览器」那一张——
  // 读者按下这颗按钮，要看的就是那一张。另一头是浏览器画面底下那行地址落在项目
  // 目录里时的「在文件夹中打开」。
  const openInBrowser = useCallback(
    async (file: OpenedProjectFile) => {
      try {
        await openProjectFileInBrowser(identity, file.projectId, file.path);
        setPanelTab("browser");
      } catch (cause) {
        setFault({ scope: sessionId ?? null, text: describe(cause) });
      }
    },
    [identity, sessionId],
  );
  // 从浏览器那一张回到文件夹：拿到的只是相对路径，而预览要那一行的字节数才能
  // 在取正文之前拒绝太大的文件——所以去它所在的那一层列一次，用列出来的那一行
  // 打开，和在树里点开它走同一条路。
  const revealFile = useCallback(
    async (path: string) => {
      if (heldProjectId == null) return;
      const parent = path.includes("/") ? path.slice(0, path.lastIndexOf("/")) : "";
      try {
        const listing = await listProjectFiles(identity, heldProjectId, {
          path: parent,
        });
        const entry = listing.entries.find(
          (one) => one.path === path && one.kind === "file",
        );
        if (entry === undefined) {
          setFault({
            scope: sessionId ?? null,
            text: `${path} 不在这个项目的目录里了。`,
          });
          return;
        }
        openProjectFileAt(entry);
      } catch (cause) {
        setFault({ scope: sessionId ?? null, text: describe(cause) });
      }
    },
    [heldProjectId, identity, openProjectFileAt, sessionId],
  );

  // 下一轮会被给出哪些工具（ADR-096）。
  //
  // 一个进程生命周期内的常量，乘上这段会话的一个属性（它的项目有没有登记目录）
  // ——所以它不轮询，也不跟着回合失效：跑完一轮不会改变下一轮拿到什么。跟着
  // `heldProjectId` 重取，因为改归属确实会改答案。
  const toolOffer = useQuery({
    queryKey: ["code-tools", identity, sessionId, heldProjectId ?? null],
    queryFn: ({ signal }) => getCodeTools(identity, sessionId as string, signal),
    enabled: sessionId !== undefined,
    staleTime: Infinity,
    // 一次就够。取不到的时候菜单里说的是「这次没取到」，而不是转圈——它不挡发送，
    // 重试三次只会让那句话晚三秒出现。
    retry: false,
  });
  // 「放手做」提不提供，是这个进程的事实，不是某段会话的（ADR-0116）。会话的 offer
  // 带着它，但起始屏上还没有会话——第一轮的输入框就画在那里，而第一轮正是人最常发
  // 的那一轮。2026-09-13 夜里实测：栈重建之后，已有会话旁边四档齐全，新建会话只有
  // 三档。所以起始屏读能力清单里 `code.unattended` 那一行（和 `CodeReach` 同一个
  // 查询键，缓存共用），有会话时仍以 offer 为准。
  const reach = useQuery({
    queryKey: ["deployment-capabilities", identity.tenantId, identity.principalId],
    queryFn: () => getDeploymentCapabilities(identity),
    staleTime: Infinity,
    retry: false,
  });
  const unattendedOffered =
    toolOffer.data?.unattended_available ??
    reach.data?.capabilities.some(
      (row) => row.id === "code.unattended" && row.state === "available",
    ) ??
    false;
  // 按下的那一颗，也是随这一轮发出去的那一档——两者是同一个值，不是两个。
  const shownPermission = drawnPermission(permission, unattendedOffered);

  // 被勾掉的工具，按会话记。
  //
  // 只存**被勾掉的**，不存被勾上的：绝大多数会话一个都没勾掉，于是这张表在绝大多数
  // 时候是空的，而「全都要」是它的缺省而不是一份要维护的名单。反过来存的那一版会在
  // 部署新增一个工具的那天，把它从每一段老会话里悄悄收窄掉——名单是上一次读目录时
  // 的那一份。
  //
  // 写回时丢掉空集合，所以这张表的大小是「真的勾掉过东西的会话数」，不是会话数。
  const [excludedBySession, setExcludedBySession] = useStoredState<
    Record<string, string[]>
  >("aw.code.tools.excluded.v1", {});
  /**
   * 这一轮实际生效的勾掉集合。
   *
   * 和存下来的那一份**求交**，理由是存的那一份会过期：offer 会变（改归属、部署改
   * 配置），而一个名字不在 offer 里时，界面上没有任何一行代表它——把它算进「勾掉了
   * 几个」，那个数就会指着一个屏幕上不存在的东西。
   *
   * 交完之后如果**每一个**都被勾掉了，整份勾选一起丢掉。那是一种只有过期能造出来
   * 的状态（界面不让人取消最后一个），而它的两种渲染都是假的：照实画会显示「一个
   * 工具都没有」而实际上服务端会照全部跑（空数组是被 422 的），照全部画又和那些
   * 没打勾的方框对不上。丢掉它，屏幕上写的就是真会跑的那一份。
   */
  const excludedTools = useMemo(() => {
    const stored = sessionId === undefined ? [] : (excludedBySession[sessionId] ?? []);
    const offered = toolOffer.data?.tools;
    if (offered === undefined) return new Set(stored);
    const held = new Set(stored);
    const live = offered.map((tool) => tool.name).filter((name) => held.has(name));
    return new Set(live.length === offered.length ? [] : live);
  }, [excludedBySession, sessionId, toolOffer.data]);
  const setExcludedTools = useCallback(
    (next: ReadonlySet<string>) => {
      if (sessionId === undefined) return;
      setExcludedBySession((held) => {
        const copy = { ...held };
        if (next.size === 0) delete copy[sessionId];
        else copy[sessionId] = [...next].sort();
        return copy;
      });
    },
    [sessionId, setExcludedBySession],
  );

  /**
   * 这一轮真正要发的保留名单，或者 `undefined`（= 全部，不发这个字段）。
   *
   * 交集而不是差集：`excludedTools` 可能还留着一个这一轮根本没被 offer 的名字——
   * 目录是在 `act` 档读的，读者随后把控件拨到了「计划」。发一份含着它的名单，服务端
   * 会照样求交（ADR-096 §3.1），但**发一份碰巧等于全部的名单**是另一回事：那会在
   * 部署新增一个工具的那天变成一次没有人打算做的收窄。所以等于全部时不发。
   */
  const keptTools = useMemo(() => {
    const offered = toolOffer.data?.tools;
    if (offered === undefined || excludedTools.size === 0) return undefined;
    const kept = offered
      .map((tool) => tool.name)
      .filter((name) => !excludedTools.has(name));
    return kept.length === offered.length ? undefined : kept;
  }, [excludedTools, toolOffer.data]);

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
  // 选中哪个运行，`null` 表示全部。
  //
  // **在 URL 里**，与 Work 页那条 `?run=` 同一个形状、同一个理由（第三十四批）：
  // 它让收窄可深链、可分享、刷新后还在，并且**免费**解决了「切会话要不要清掉它」
  // ——会话 id 在路径里，换会话就是换 URL，查询串跟着走。上一批用
  // `{sessionId, runId}` 配对做的事因此可以退掉。
  //
  // `replace` 而不压历史条目：这是一个筛选器，不是一个目的地。每点一次压一条，
  // 会让返回键在读者点过几次之后才轮到「离开这个会话」，而面板本来就有显式的
  // 「显示全部」。
  const selectedRunId = searchParams.get("run");
  const selectRun = useCallback(
    (runId: string | null) => {
      setSearchParams(
        (held) => {
          const next = new URLSearchParams(held);
          if (runId === null) next.delete("run");
          else next.set("run", runId);
          return next;
        },
        { replace: true },
      );
    },
    [setSearchParams],
  );
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
    async (turnPermission: CodePermission = shownPermission, override?: string) => {
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
    // Only what came *from* the composer empties it. A turn started with text
    // of its own -- re-running a plan, and now the automatic verify pass --
    // would otherwise throw away the half-sentence the reader was typing while
    // it fired.
    if (override === undefined) setInstruction("");
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
        undefined,
        // 只在真的收窄了的时候发（ADR-096）。一份碰巧等于全部的名单，会在部署新增
        // 一个工具的那天变成一次没有人打算做的收窄——名单是上一次读目录时的那一份。
        keptTools,
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
      keptTools,
      navigate,
      queries,
      reload,
      running,
      sessionId,
      shownPermission,
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
    (file: WorkspaceEntryView, options?: { expand?: boolean }) => {
      if (sessionId === undefined) return;
      // 同 `openProjectFileAt`：自动弹的那一次不推翻读者收起过的表态。
      if (options?.expand !== false) setPanelChoice(true);
      setPanelTab("preview");
      // 让位给它，理由同 `openProjectFileAt`：一栏，一个文件。
      setOpenProjectFile(null);
      setOpened({
        sessionId,
        name: file.name,
        mediaType: file.media_type,
        sizeBytes: file.size_bytes,
      });
    },
    [sessionId, setPanelChoice],
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

  // 这一轮写出来的那个页面，工作区一份、项目目录一份（ADR-0112）。
  //
  // 两份分开算而不是合成一个：一段会话只在其中一个面上工作（工具清单在每轮开始
  // 时就按有没有项目目录定死了），所以合并只会掩盖「这段会话现在在哪一面」这个
  // 本来就知道的事实——下面那段效果直接按 `heldProjectId` 选一份。
  //
  // 类型从清单里取，取不到就按名字猜：工作区条目带着服务端给的 media type，项目
  // 文件没有（那条路由一律答 `application/octet-stream`），而 `pageAmong` 两种都
  // 接。
  const producedPage = useMemo(
    () =>
      pageAmong(
        blocks.flatMap((block) => block.produced.map((file) => file.name)),
        (name) => files.find((held) => held.name === name)?.media_type,
      ),
    [blocks, files],
  );
  const writtenPage = useMemo(() => pageAmong(projectWrites), [projectWrites]);

  // 写出一个页面之后，它自己到右边那一栏里跑起来。
  //
  // **触发的是「这个标签页刚跑完一轮」，不是「流里有一个 .html」。** 后者看起来
  // 更简单，实际是另一件事：打开一段旧会话时，流里早就有上次写出的页面，而那时
  // 读者要的是文件夹和上次说到哪，不是一张盖住它们的页面。一轮的落定是这个页面
  // 已经知道的事实（`running` 由 `runningIn` 派生），所以「刚生成」不用猜。
  //
  // 代价说清楚：另一个标签页跑的那一轮不会弹——`runningIn` 只记这个标签页发起的
  // 请求。那一侧的产出仍然是对话里的一张卡片。
  //
  // **为什么等落定而不是写出就弹。** 一轮里页面常被改写好几次，而两侧的正文缓存
  // 都不是按内容键的（工作区那份是 `staleTime: Infinity`，项目那份 5 秒）——半路
  // 弹出来的那一版会一直留在屏幕上，看起来却像是最新的。那比不弹更糟。
  const ranIn = useRef<string | null>(null);
  const settled = useRef(false);
  // 自动弹出来的那一张页面的名字。自动验证那一段读它：读者自己从文件夹里点开一个
  // 历史页面，不该触发任何模型调用。
  const autoShown = useRef<string | null>(null);
  useEffect(() => {
    if (running) {
      ranIn.current = sessionId ?? null;
      return;
    }
    // 还要仍然停在跑它的那段会话上：中途切走的读者已经用脚表过态了。
    settled.current = ranIn.current !== null && ranIn.current === sessionId;
    ranIn.current = null;
  }, [running, sessionId]);

  useEffect(() => {
    if (!settled.current || sessionId === undefined) return;
    // 一段会话只在其中一个面上工作（工具清单每轮开始时就按有没有项目目录定死
    // 了），所以这里是选一份，不是合并两份。
    const page = heldProjectId == null ? producedPage : writtenPage;
    if (page === null) return;
    // 展开，除非读者**明确**收起过这一栏。三态在这里是有用的：`null` 是「还没
    // 表过态」，不是「不要」——而没有项目目录的会话，那一栏默认就是不显示的
    // （`panelShown`），所以把页面放进一个不显示的栏里等于什么也没做。`false`
    // 才是表过的态，那就听他的：文件放好，他下次展开就看见。
    const expand = panelChoice !== false;
    if (heldProjectId == null) {
      const entry = files.find((held) => held.name === page);
      // 清单里还没有它就什么也不做，**而且不解除待办**——这一步的顺序是这两段
      // 效果唯一容易写错的地方，因为它在正常路径上必然发生一次：一轮结束时
      // `setRunningIn` 先落，工作区清单是随后那次 `reload` 才回来的，所以
      // 「`running` 变 false」和「清单里有这个新文件」之间隔着一次渲染。在那
      // 次渲染里就当作办过，等于每一次都不弹。
      if (entry === undefined) return;
      settled.current = false;
      autoShown.current = page;
      // 正文缓存按名字键、`staleTime: Infinity`（`FilePreview` 的 `fileKey`），
      // 所以改写过的同名文件会拿着上一版的字节渲染——而这正是「改一下这个按钮」
      // 那种轮次的常态。不失效的话，自动弹出来的是一张看起来最新的旧页面，比不
      // 弹更糟。只失效这一个名字：别的文件没有在屏幕上，而它们的陈旧是这次改动
      // 之前就有的事，顺手半修一半只会让下一个读者以为它被修好了——那一半登记
      // 成 F-38，连同它为什么不是一行能修的。
      //
      // 先失效、再打开，顺序就是理由：查看器一挂上就去取正文，先失效等于它取
      // 到的是新的那一份。在 `.then` 里打开还有第二个作用——这份文件里每一处
      // 「效果里改状态」都躲在 `.then` 后面（`reload` 那几处写着为什么），lint
      // 认的也是这一条。
      void Promise.all(
        ["code-file-text", "code-file-html", "code-file-blob"].map((prefix) =>
          queries.invalidateQueries({
            queryKey: workspaceFileKey(prefix, identity, {
              sessionId,
              name: page,
            }),
          }),
        ),
      )
        // 失效没做成也照样打开：查看器自己会说它取不到，而在这里咽掉预览等于
        // 用一个更小的问题换一个更沉默的问题。
        .catch(() => undefined)
        .then(() => {
          // 这几毫秒里读者可能已经换了一段会话。`shown` 是这份文件里回答「我
          // 现在停在哪」的那个 ref，几处迟到的响应都问它。
          if (shown.current.sessionId !== sessionId) return;
          open(entry, { expand });
        });
      return;
    }
    settled.current = false;
    autoShown.current = page.split("/").pop() ?? page;
    // 项目那一侧同理，键是 `ProjectTextBody` 那条读。默认 5 秒的 staleTime 在
    // 一轮比 5 秒短的时候同样会给出上一版。
    void queries.invalidateQueries({
      queryKey: ["project-file", identity, heldProjectId, page],
    });
    // 项目文件只有路径，而预览在取正文之前要知道字节数（太大的不展开）。那一行
    // 在它所在那一层的目录列表里，所以这里问一次——一次很小的请求，换的是不必
    // 编一个假的大小塞给查看器。
    const controller = new AbortController();
    const projectId = heldProjectId;
    listProjectFiles(identity, projectId, {
      path: parentOf(page),
      signal: controller.signal,
    })
      .then((listing) => {
        const entry = listing.entries.find(
          (held) => held.path === page && held.kind === "file",
        );
        if (entry !== undefined) openProjectFileAt(entry, { expand });
      })
      .catch(() => {
        // 打不开就不开。这是一个便利，不是一条结果——为它弹一条错误，等于把
        // 「你没要求的事没做成」摆到读者面前。文件仍然在文件夹那一张里。
      });
    return () => {
      controller.abort();
    };
  }, [
    files,
    heldProjectId,
    identity,
    open,
    openProjectFileAt,
    panelChoice,
    producedPage,
    queries,
    running,
    sessionId,
    writtenPage,
  ]);

  // 自己验证自己写的页面（第七十七批·第 8 条）。
  //
  // **这条回路里没有人。** 上一条把页面报的错画了出来、给了一颗「交给它」的按钮，
  // 而用户的话是「我是让他自己验证 不是需要我点击」。所以这里把那次点击也拿掉：
  // 一轮落定、页面自己跑起来、它报了错，就直接用那些错误再发一轮。
  //
  // **说清楚这条回路证明得了什么、证明不了什么。** 它证明的是「这页不抛异常了」。
  // 「跳得跟不跟手、关卡好不好玩」它一个字也答不了——那种判断只有人做得了，而这台
  // 部署的项目会话一个执行工具都没有（ADR-0109 §3.3），模型自己永远跑不了它写的
  // 东西。真正在跑的是读者浏览器里那个沙箱帧，这段代码只是把帧说的话送回去。
  //
  // 三道闸，少一道这就是一台烧钱的机器：
  //
  // 1. **最多两轮。** 第三轮还在报同样的错，说明模型修不动它，再发一轮只是重复。
  // 2. **同一组错误不发第二次。** 上一轮发过 A、这一轮还是 A，就是没修好——停。
  // 3. **只对「这个标签页刚跑完的那一轮自己打开的页面」生效。** 读者从文件夹里点开
  //    一个历史页面不该触发任何模型调用。
  //
  // **没有开关，这是被要求的，而且那三道闸就是它的全部刹车。** 曾经有一颗「自动验证」
  // 摆在权限选择器旁边，理由是「它会花钱，一个花钱的行为不该只在代码里存在」。用户
  // 看过之后要求去掉它。留下的约束是硬的而不是自觉的：一轮最多两次额外调用，同一组
  // 错误一次，且只对自动弹出来的那张页面——所以「它会不会一直烧下去」这个问题在这里
  // 有一个上界，而不是一个态度。
  const AUTO_ROUNDS = 2;
  // 这一轮的自动修到第几轮了，以及上一次交出去的是哪一组错误。按会话记，换会话清零。
  const autoPass = useRef<{ session: string | null; rounds: number; sent: string }>(
    { session: null, rounds: 0, sent: "" },
  );
  const onFaults = useCallback(
    (list: readonly string[], name: string) => {
      if (running || sessionId === undefined) return;
      // 只认自动弹出来的那一张：`shownPage` 是那段效果记下的「这一轮产出的页面」。
      if (autoShown.current !== name) return;
      const signature = [...list].sort().join("|");
      const pass =
        autoPass.current.session === sessionId
          ? autoPass.current
          : { session: sessionId, rounds: 0, sent: "" };
      if (pass.rounds >= AUTO_ROUNDS || pass.sent === signature) return;
      autoPass.current = {
        session: sessionId,
        rounds: pass.rounds + 1,
        sent: signature,
      };
      // 这句话原样进转录，所以读者看得见是谁问的、问了什么——一次没有人按下的
      // 模型调用，必须在它花掉之后能被指认出来。
      void send(
        shownPermission,
        `（自动验证，第 ${String(pass.rounds + 1)}/${String(AUTO_ROUNDS)} 轮）` +
          `${name} 在预览里运行时报了这些错误，请修掉它们：\n` +
          list.map((fault) => `- ${fault}`).join("\n"),
      );
    },
    [running, send, sessionId, shownPermission],
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

  /**
   * 一整个文件夹，进这段会话的工作区。
   *
   * 三件事在真的开始传之前发生，因为它们之后就来不及说了：
   *
   * 1. **算一份计划**（`planFolderUpload`）。工作区是平的，所以路径会被压进名字里，
   *    而这是一次有损变换——`src/app/main.ts` 会变成 `src-app-main.ts`。读者事后在
   *    工作区里找不到 `main.ts` 时，会以为上传失败了。
   * 2. **撞上限就先说**。工作区一共收 `MAX_WORKSPACE_ENTRIES` 条，而浏览器的目录
   *    选择器给的是整棵树。传到第 256 个再被服务端拒绝，代价是前面 255 次往返。
   * 3. **让人确认**。`window.confirm` 而不是自己的对话框，理由和删除会话那一处
   *    一样：这个控制台没有模态组件，为这里发明一个是第二件要审的东西。
   */
  const attachFolder = useCallback(
    async (chosen: FileList | null) => {
      if (chosen === null || chosen.length === 0) return;
      if (sessionId === undefined) {
        setFault({
          scope: null,
          text: "先说一句要做的事，会话开起来之后就能上传文件夹了。",
        });
        return;
      }
      const plan = planFolderUpload(chosen);
      if (plan.entries.length === 0) {
        setFault({
          scope: sessionId,
          text: "这个文件夹里没有可以上传的文件（node_modules / .git 这些不传）。",
        });
        return;
      }
      if (plan.entries.length > MAX_WORKSPACE_ENTRIES) {
        setFault({
          scope: sessionId,
          text: `这个文件夹有 ${String(plan.entries.length)} 个文件，工作区一共只收 ${String(MAX_WORKSPACE_ENTRIES)} 条。挑一个小一点的目录。`,
        });
        return;
      }
      if (!window.confirm(describeFolderUpload(plan))) return;
      setUploading(true);
      setFault(null);
      try {
        // 顺序传，和单文件那一条同一个理由：每一次写都拿它读到的版本做一次
        // compare-and-set，两个在飞的写会撞，输的那个被拒。
        for (const entry of plan.entries) {
          const listing = await putCodeWorkspaceFile(
            identity,
            sessionId,
            entry.file,
            entry.name,
          );
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
      {/* 只剩会话列表。项目目录树搬去了右栏（见 `directory`）：一条 260px 宽的
          侧栏同时装「这个项目有哪些文件」和「我开过哪些会话」，两份列表互相挤，
          而它们回答的是完全不同的两个问题。左边留给「我在哪段对话里」。 */}
      <div className="aw-code-sidebar-stack">
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
              // 拿到的是它自己的信封，和没有先计划过时一模一样。「去做」是这台
              // 部署画出来的那一档（`drawnPermission`）。
              void send(drawnPermission("act", unattendedOffered), planned.text);
            }}
            type="button"
          >
            按这个计划执行
          </button>
        </div>
      )}
      {/* 这一轮会动哪个文件夹，写在提问的正上方。
          此前它只在页头那颗「文件夹」按钮上，而那颗按钮说的是「这一栏开着没」
          ——两件事。发指令的人要知道的是「我这句话会落在哪」，那句话要挨着输入框。

          三枚，不是设计稿上的五枚。稿子上还画了分支和 worktree，而 `ProjectView`
          只有 `project_id / name / root_path`：这个后端没有分支的概念，也没有
          worktree 的概念。画出来会让人以为可以切——那和一颗点不动的重试按钮是
          同一类错，只是更贵，因为它伪装成的是一个已经存在的功能。 */}
      {projectRoot === null ? null : (
        <div className="aw-code-picks">
          <span className="aw-code-pick" title="这个进程绑在环回地址上，指令在这台机器上执行">
            <MonitorIcon aria-hidden="true" size={13} />
            本地
          </span>
          <span className="aw-code-pick" title={projectRoot}>
            <FolderIcon aria-hidden="true" size={13} />
            <strong>{project.data?.name ?? "项目"}</strong>
          </span>
          {/* 「看这个文件夹」和「换文件夹」曾经就在这一行上，长成两枚会动的 chip。
              它们搬进了输入框下面那颗「+」，而这一行退回它本来的样子：**只说这一
              轮落在哪**，不做事。
              两个理由。一是这一行原来同时是状态和动作——两枚灰底的 chip 里，前两枚
              点不动、后两枚点得动，而它们长得一样。二是那两件事和「上传文件」「换
              一个文件夹」本来就是一组：这一句话之外我还想给这一轮加点什么——而它
              们此前散成四个入口、四种形状。 */}
        </div>
      )}
      {/* 整段会话的合计。和 Chat 同一个位置、同一个零件。 */}
      <TurnUsage
        label="这段会话"
        usage={sumTurnUsage(blocks.map((block) => block.usage))}
      />
      <div className="aw-code-composer-row aw-mode-composer-card">
        {/* 输入框在 DOM 里排第一，因为它在版面上也排第一：卡片的第一行归它，
            「+」、权限、发送在下面一条（为什么不分宽窄都是两行，见 app.css 的
            `.aw-code-composer-row`）。那条规则只给发送键写了位置，其余三个按这里的
            顺序落进轨道——这里换顺序，版面就跟着换。 */}
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
          onKeyDown={(event) => {
            // 空输入框里的一个 `/`，开的是「+」菜单的「快捷指令」那一栏——不是第二份
            // 长得像它的清单。两份会分叉，而它们说的是同一组东西。
            //
            // 只在**空**输入框里，且不在拼音输入过程中：一个正在被输入法组合的按键
            // 会同时报出 `isComposing`，而在一句中文中间打斜杠是完全正常的事。
            if (
              event.key === "/" &&
              instruction === "" &&
              !event.nativeEvent.isComposing
            ) {
              event.preventDefault();
              setMenuSubmenu(COMMANDS_SUBMENU);
              setMenuOpen(true);
              return;
            }
            submitTextareaOnEnter(event);
          }}
          placeholder="描述你要做的事"
          ref={instructionRef}
          rows={3}
          value={instruction}
        />
        <ComposerMenu
          catalogue={toolOffer.data}
          catalogueFailed={toolOffer.isError}
          disabled={running}
          excluded={excludedTools}
          onInsertPrompt={(prompt) => {
            // 追加而不是替换：读者可能已经打了半句。前面有字就先补一个换行，
            // 免得模板和那半句黏成一行。
            setInstruction((held) =>
              held.trim() === "" ? prompt : `${held.replace(/\s+$/, "")}\n${prompt}`,
            );
            window.requestAnimationFrame(() => instructionRef.current?.focus());
          }}
          onOpenChange={(next) => {
            setMenuOpen(next);
            if (!next) setMenuSubmenu(null);
          }}
          onPickFiles={(chosen) => void attach(chosen)}
          onPickFolder={(chosen) => void attachFolder(chosen)}
          onResetTools={() => setExcludedTools(new Set())}
          onShowFolder={
            projectRoot === null
              ? null
              : () => {
                  setPanelChoice(true);
                  setPanelTab("directory");
                }
          }
          onSwitchFolder={() => {
            setStartingIn(null);
            void navigate("/code");
          }}
          onToggleTool={(name) => {
            const next = new Set(excludedTools);
            if (next.has(name)) next.delete(name);
            else next.add(name);
            setExcludedTools(next);
          }}
          open={menuOpen}
          openSubmenu={menuSubmenu}
          planning={shownPermission === "plan"}
          sessionId={sessionId}
          starters={CODE_STARTERS}
          unattended={shownPermission === "auto"}
          uploading={uploading}
          writeGate={shownPermission === "ask"}
        />
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
          {/* 三档，不是四档：`offeredPermissions` 说了为什么。两份「提不提供
              放手做」的答案都还没到时按不提供画——一颗在下一帧消失的按钮，和
              一颗按下去换来 422 的按钮，教给读者的都是错的规则。 */}
          {offeredPermissions(unattendedOffered).map((choice) => (
            <button
              aria-pressed={shownPermission === choice.value}
              className={shownPermission === choice.value ? "is-active" : ""}
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
              {choice.value === "auto" ? <Rocket aria-hidden size={13} /> : null}
              {choice.label}
            </button>
          ))}
        </div>
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
          <div className="aw-code-start">
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
                description={`在 ${startingIn.root_path ?? startingIn.name} 里编码。Agent 修改的是这个文件夹里的真实文件：每一次写入都记在回合里，右栏能打开改过的文件检查。`}
                title="开始编码"
              />
              {/* 换文件夹的入口。此前只有「一个项目都没有」时才画得出选择器，
                  选过一次之后它就再也回不来了——屏幕上只剩一句「在 X 里编码」，
                  而那句话是个陈述，不是一个可以点的地方。 */}
              <button
                className="aw-code-switch-folder"
                onClick={() => {
                  setStartingIn(null);
                }}
                type="button"
              >
                <FolderIcon aria-hidden="true" size={13} />
                换一个文件夹
              </button>
              <CodeReach />
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
  // 这条门原来只认「工作区里有文件」或「点开了某个文件」，理由是：大多数会话在
  // 第一轮之前一个文件都没有，而一条 440px 宽、只写着「工作区全部文件（0）」的
  // 空栏，占的宽度和一屏代码一样多。
  //
  // 那个理由现在只对一半。**项目目录不需要等任何事情发生就存在**——它是这段会话
  // 一开始就选定的那个文件夹，也是读者问「我现在在哪个文件夹里」时唯一的答案。
  // 所以有 `projectRoot` 的会话，这一栏从第一秒就有内容可显示，空栏的那个顾虑
  // 不成立；没有目录的会话（`agent-cli` 之外建的老会话）仍然按老规矩来。
  const panelShown =
    (panelChoice ?? projectRoot !== null) &&
    (files.length > 0 || panelProjectFile !== null || projectRoot !== null);

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
            {/* 改名就地发生，不把人送去左栏那一行。
                左栏那份 inline rename 还在（它是列表里那一行自己的动作），但页头
                这一颗够不着它：那一栏在窄屏上是抽屉，在宽屏上可以被折起来，而
                「重命名」按下去之后什么都没发生是最坏的一种反馈。 */}
            {headerRenaming && sessionId !== undefined ? (
              <form
                className="aw-code-header-rename"
                onSubmit={(event) => {
                  event.preventDefault();
                  const field = new FormData(event.currentTarget).get("title");
                  const next = typeof field === "string" ? field.trim() : "";
                  setHeaderRenaming(false);
                  if (next !== "" && next !== (title ?? "")) {
                    void rename(sessionId, next);
                  }
                }}
              >
                <label className="aw-sr-only" htmlFor="aw-code-header-rename">
                  会话名字
                </label>
                <input
                  autoFocus
                  defaultValue={title ?? ""}
                  id="aw-code-header-rename"
                  name="title"
                  onBlur={() => setHeaderRenaming(false)}
                  onFocus={(event) => event.currentTarget.select()}
                  onKeyDown={(event) => {
                    if (event.key !== "Escape") return;
                    event.preventDefault();
                    event.stopPropagation();
                    setHeaderRenaming(false);
                  }}
                />
              </form>
            ) : (
              <h1>{title ?? "新会话"}</h1>
            )}
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
          {files.length === 0 && projectRoot === null ? null : (
            // 有目录的会话也画这颗开关（2026-09-13）。此前它只在会话产出过文件
            // 时出现，于是一段只读了目录、什么也没写的项目会话，读者收起右栏
            // 之后没有任何看得见的东西能把它叫回来——`aw.code.panel.v2` 记着
            // 那次收起，刷新也不放；唯一的路是 ⋮ 菜单里的「看这个文件夹」。
            // 实测里这正是「预览不见了」的样子。
            <button
              aria-controls="aw-code-panel"
              aria-expanded={panelShown}
              className={`aw-button aw-code-workspace-entry ${
                panelShown ? "is-open" : ""
              }`}
              onClick={() => {
                if (panelShown) {
                  setPanelChoice(false);
                  return;
                }
                setPanelChoice(true);
                setPanelTab(files.length > 0 ? "workspace" : "directory");
              }}
              type="button"
            >
              <PanelRightOpen aria-hidden size={15} />
              {files.length > 0 ? `工作区 ${String(files.length)}` : "文件夹"}
            </button>
          )}
          {/* 「工作区 N」那颗留着，没有被这颗 ⋮ 吃掉：它是一个带 `aria-expanded`
              的**开关**，读者需要它一眼看得见开着没有。菜单里那一项只会打开，
              两者不是同一件事——一个说状态，一个是入口。 */}
          {sessionId === undefined ? null : (
            <SessionMenu
              fileCount={files.length}
              onDelete={() => remove(sessionId)}
              onRename={() => setHeaderRenaming(true)}
              onShowFolder={
                projectRoot === null
                  ? null
                  : () => {
                      setPanelChoice(true);
                      setPanelTab("directory");
                    }
              }
              onShowWorkspace={
                files.length === 0
                  ? null
                  : () => {
                      setPanelChoice(true);
                      setPanelTab("workspace");
                    }
              }
            />
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
            onSelect={selectRun}
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
              setPanelChoice(false);
            }}
            type="button"
          />
          <PreviewPanel
            events={shownSteps}
            directory={
              heldProjectId != null && projectRoot !== null ? (
                <ProjectFileTree
                  onOpenFile={openProjectFileAt}
                  projectId={heldProjectId}
                  rootPath={projectRoot}
                  selectedPath={openProjectFile?.path ?? null}
                  writtenPaths={projectWrites}
                />
              ) : null
            }
            files={files}
            identity={identity}
            onDeleteFile={(file) => void deleteOpenedFile(file)}
            onOpenInBrowser={(file) => void openInBrowser(file)}
            onRenameFile={(file) => void renameOpenedFile(file)}
            onRevealFile={(path) => void revealFile(path)}
            projectRoot={projectRoot}
            onCollapse={() => {
              setPanelChoice(false);
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
            onFaults={onFaults}
            onOpen={open}
            onReport={(report) => {
              // 追加而不是替换，和「快捷指令」那一处同一个理由：读者可能已经
              // 打了半句。落焦点是这次点击的全部意思——他要的下一步是补一句
              // 「顺便把跳跃调高一点」然后发送，而不是再去找输入框。
              setInstruction((held) =>
                held.trim() === ""
                  ? report
                  : `${held.replace(/\s+$/, "")}\n${report}`,
              );
              window.requestAnimationFrame(() =>
                instructionRef.current?.focus(),
              );
            }}
            onTab={setPanelTab}
            onWrote={refreshWorkspace}
            orphanRuns={orphanRuns}
            projectFile={panelProjectFile}
            tab={panelTab}
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
