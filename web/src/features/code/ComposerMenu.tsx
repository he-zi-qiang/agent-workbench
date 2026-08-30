/**
 * 输入框旁边那颗「+」。
 *
 * 它替掉的是一颗回形针。那颗回形针只做一件事（上传文件），而它旁边本来就还站着两颗
 * 按钮（「看这个文件夹」「换文件夹」）和一排起始提示——四个入口，四种形状，各自占一
 * 块地方，谁也不知道自己和另外三个是一组的。它们是一组的：**这一句话之外，我还想给
 * 这一轮加点什么**。一颗「+」加一份清单，是那句话的形状。
 *
 * 清单是**声明**的（`MenuEntry[]`），不是一段 JSX。这一条是冲着「以后的扩展」写的：
 * 加一项就是往数组里加一个对象，而它出不出现由它自己的条件决定——
 *
 * * 没有会话时，上传两项不出现。会话要等第一句指令才开（ADR-047），而一个点了会说
 *   「先说一句话」的上传按钮，是把一条规则做成一次失败。
 * * 没有目录时，「看这个文件夹」不出现。
 * * 工具那一栏在目录还没取回来时不出现，取失败时变成一句话。
 *
 * 三条都是**不画**，不是画一个 disabled 的。这个仓库为这件事收过账：一个画得出来
 * 但点不动的控件，读者会以为功能坏了，而不是以为它不适用。
 *
 * **不做的两件事**，都是照着截图里有、而这一侧没有的东西划的线：
 *
 * * **Import GitHub issue**：这个部署没有任何一条通往 GitHub 的路。
 * * **Plugins**：没有插件这一层。
 *
 * 画出来会让人以为可以点。这和 `.aw-code-picks` 那三枚 chip 的注释是同一条规矩：
 * 稿子上画了分支和 worktree，而 `ProjectView` 里没有这两个概念，所以那两枚没画。
 */

import {
  FolderOpen,
  FolderUp,
  Plus,
  Slash,
  Upload,
  Wrench,
} from "lucide-react";
import { useEffect, useRef } from "react";
import type { CodeToolsResponse, ToolRisk } from "../../api/types";
import { Menu, type MenuEntry } from "../../components/Menu";
import type { ModeStarter } from "../../components/ModeStart";
import { RISK_LABELS } from "./toolVocabulary";

/** 快捷指令那一栏的 id，输入框里打 `/` 时要点名展开它。 */
export const COMMANDS_SUBMENU = "commands";

/** 工具那一栏的 id。 */
export const TOOLS_SUBMENU = "tools";

/**
 * 这一轮真正会停在人这里的风险。
 *
 * 部署自己那一份是地板（`approval_required_risks`），会话选的「改前问我」只能往上
 * 加 `write`，减不掉任何东西——ADR-087 把这条不变量写成了后端一个函数的形状，这里
 * 是它在界面上的第二次表达。**只加不减**，所以这里是并集而不是替换。
 */
function stopsAtAPerson(
  catalogue: CodeToolsResponse,
  writeGate: boolean,
): ReadonlySet<ToolRisk> {
  const risks = new Set<ToolRisk>(catalogue.approval_required_risks);
  if (writeGate) risks.add("write");
  return risks;
}

export function ComposerMenu({
  catalogue,
  catalogueFailed,
  disabled,
  excluded,
  onInsertPrompt,
  onOpenChange,
  onPickFiles,
  onPickFolder,
  onResetTools,
  onShowFolder,
  onSwitchFolder,
  onToggleTool,
  open,
  openSubmenu,
  planning,
  sessionId,
  starters,
  uploading,
  writeGate,
}: {
  /** 下一轮会拿到什么（ADR-096）。`undefined` 是「还没取到」。 */
  catalogue: CodeToolsResponse | undefined;
  catalogueFailed: boolean;
  disabled: boolean;
  /** 被勾掉的工具名。空集是「全都要」，也是默认。 */
  excluded: ReadonlySet<string>;
  onInsertPrompt: (prompt: string) => void;
  onOpenChange: (open: boolean) => void;
  onPickFiles: (chosen: FileList | null) => void;
  onPickFolder: (chosen: FileList | null) => void;
  onResetTools: () => void;
  /** `null` 表示这段会话没有目录可看。 */
  onShowFolder: (() => void) | null;
  onSwitchFolder: () => void;
  onToggleTool: (name: string) => void;
  open: boolean;
  openSubmenu: string | null;
  /** 这一轮是不是计划模式。计划模式已经收走了会改东西的那几个（ADR-0079）。 */
  planning: boolean;
  sessionId: string | undefined;
  starters: readonly ModeStarter[];
  uploading: boolean;
  /** 这一轮是不是「改前问我」。 */
  writeGate: boolean;
}) {
  const filesRef = useRef<HTMLInputElement | null>(null);
  const folderRef = useRef<HTMLInputElement | null>(null);

  // ⌘U，真的绑上。
  //
  // 菜单里那一行右端印着「⌘U」，而一个印出来却按不动的快捷键，比不印更糟：读者会
  // 认为自己按错了，然后再也不试第二次。`AppShell` 的 ⌘K 是同一个形状（window 上
  // 的一个 keydown），这里跟着它写。
  //
  // 有对话框开着的时候不响应，理由和 ⌘K 那一处一样：一个盖在上面的模态正在等一次
  // 表态，而弹出系统的文件选择框会把那次表态挤到看不见的地方去。`FolderPicker` 就
  // 是这样一个对话框。
  useEffect(() => {
    if (sessionId === undefined) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key.toLocaleLowerCase() !== "u") return;
      if (!(event.metaKey || event.ctrlKey) || event.altKey || event.shiftKey) {
        return;
      }
      if (disabled || uploading) return;
      if (document.querySelector('[role="dialog"]') !== null) return;
      event.preventDefault();
      filesRef.current?.click();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [disabled, sessionId, uploading]);

  const toolEntries = (): MenuEntry[] => {
    if (catalogue === undefined) return [];
    const gated = stopsAtAPerson(catalogue, writeGate);
    const entries: MenuEntry[] = [
      {
        kind: "note",
        id: "tools-note",
        // 「这就是全部」是这句话里唯一重要的部分。它不是一个筛选器的默认值，是这一轮
        // 的完整能力面——而它来自服务端的同一条表达式，不是这里写死的一份（ADR-096 §2）。
        text: "下一轮会拿到的全部工具。勾掉的这一轮不会被给出；只能减，不能加。",
      },
    ];
    // 还剩几个勾着的。用来挡住「取消最后一个」——一个工具都没有的信封会被服务端
    // 422（空数组不是一个合法请求），而在读者点下去之后才用一次失败告诉他这件事，
    // 比一开始就说勾不动要差。全勾掉的意思是「这一轮别动任何东西」，而那是左边
    // 那个三档控件的第一档，不是一个空信封。
    const stillChecked = catalogue.tools.filter(
      (tool) => (!planning || tool.kept_in_plan) && !excluded.has(tool.name),
    ).length;
    for (const tool of catalogue.tools) {
      const removedByPlan = planning && !tool.kept_in_plan;
      const checked = !removedByPlan && !excluded.has(tool.name);
      const last = checked && stillChecked === 1;
      const label = RISK_LABELS[tool.risk] ?? tool.risk;
      entries.push({
        kind: "checkbox",
        id: `tool-${tool.name}`,
        label: tool.name,
        // 工具自己的描述，模型读到的那一句。第二套措辞会让「它为什么不用那个
        // grep」变成一个要先 diff 才回答得了的问题。
        hint: tool.description,
        // 计划模式收走的那几个，勾不勾都一样，所以它们**说自己为什么勾不动**，
        // 而不是安静地消失——消失会让读者以为这个部署根本没有写入工具。
        checked,
        disabled: removedByPlan || last,
        trailing: removedByPlan
          ? "计划模式已收走"
          : last
            ? "至少留一个"
            : gated.has(tool.risk)
              ? `${label} · 会先问你`
              : label,
        onSelect: () => onToggleTool(tool.name),
      });
    }
    if (excluded.size > 0) {
      entries.push({ kind: "separator", id: "tools-reset-rule" });
      entries.push({
        kind: "action",
        id: "tools-reset",
        label: "全部勾回来",
        trailing: `已勾掉 ${String(excluded.size)} 个`,
        onSelect: onResetTools,
      });
    }
    return entries;
  };

  // 每一项自己决定出不出现。加一项就是往这个数组里加一个对象。
  const entries: MenuEntry[] = [
    ...(sessionId === undefined
      ? []
      : ([
          {
            kind: "action",
            id: "upload-files",
            label: "上传文件",
            icon: <Upload aria-hidden size={15} />,
            // ⌘U 而不是 Ctrl+U：这个控制台在 macOS 上跑，而 `AppShell` 的快捷键
            // 已经是 ⌘K。两种写法混在一起会让人以为它们是两套。
            trailing: "⌘U",
            disabled: uploading,
            onSelect: () => filesRef.current?.click(),
          },
          {
            kind: "action",
            id: "upload-folder",
            label: "上传文件夹",
            icon: <FolderUp aria-hidden size={15} />,
            hint: "工作区没有目录，路径会压进名字里",
            disabled: uploading,
            onSelect: () => folderRef.current?.click(),
          },
        ] satisfies MenuEntry[])),
    ...(sessionId === undefined ? [] : [{ kind: "separator", id: "r1" } as MenuEntry]),
    ...(onShowFolder === null
      ? []
      : ([
          {
            kind: "action",
            id: "show-folder",
            label: "看这个文件夹",
            icon: <FolderOpen aria-hidden size={15} />,
            onSelect: onShowFolder,
          },
        ] satisfies MenuEntry[])),
    {
      kind: "action",
      id: "switch-folder",
      label: "换一个文件夹",
      icon: <FolderOpen aria-hidden size={15} />,
      // 换文件夹是**开一段新会话**，不是把这一段搬走：一段会话属于一个项目
      // （ADR-074 §7.1），搬走会让「产物存哪了」重新有两个答案。这句话写在这里，
      // 是因为点下去之后人就到起始屏了，那时已经来不及说。
      //
      // 起始屏上它和 `.aw-code-switch-folder` 那颗按钮重了，而那是**留着的**：
      // 那一屏整屏就在问「在哪个文件夹里编码」，一个显眼的换法属于那个问题；
      // 菜单里这一份是给已经进了会话的人的，两处的读者不是同一个。会话里那颗
      // 按钮已经没有了——这一项就是它搬过来的地方。
      hint: "会回到起始屏，另开一段会话",
      onSelect: onSwitchFolder,
    },
    { kind: "separator", id: "r2" },
    {
      kind: "submenu",
      id: COMMANDS_SUBMENU,
      label: "快捷指令",
      icon: <Slash aria-hidden size={15} />,
      // 在输入框里打一个 `/` 也到这里。写出来是因为一条没有人知道的快捷键等于没有。
      hint: "在输入框里打 / 也能开",
      entries: starters.map((starter) => ({
        kind: "action" as const,
        id: `starter-${starter.title}`,
        label: starter.title,
        hint: starter.prompt,
        onSelect: () => onInsertPrompt(starter.prompt),
      })),
    },
    ...(sessionId === undefined
      ? []
      : catalogueFailed
        ? ([
            {
              kind: "note",
              id: "tools-failed",
              // 取不到就说取不到。一个空的工具栏读起来是「这一轮什么工具都没有」，
              // 而那是一句关于这个回合的假话。
              text: "这一轮会拿到哪些工具，这次没取到——发送不受影响，收窄不可用。",
            },
          ] satisfies MenuEntry[])
        : catalogue === undefined
          ? []
          : ([
              {
                kind: "submenu",
                id: TOOLS_SUBMENU,
                label: "工具",
                icon: <Wrench aria-hidden size={15} />,
                hint:
                  catalogue.surface === "project"
                    ? "这一轮读写的是项目目录里的真实文件"
                    : "这一轮读写的是这段会话的工作区",
                trailing:
                  excluded.size === 0
                    ? `${String(catalogue.tools.length)} 个`
                    : `${String(catalogue.tools.length - excluded.size)}/${String(catalogue.tools.length)}`,
                entries: toolEntries(),
              },
            ] satisfies MenuEntry[])),
  ];

  return (
    <span className={`aw-composer-plus ${uploading ? "is-busy" : ""}`}>
      <Menu
        align="start"
        entries={entries}
        label="给这一轮加点什么"
        onOpenChange={onOpenChange}
        open={open}
        openSubmenu={openSubmenu}
        placement="top"
        trigger={<Plus aria-hidden size={17} />}
        triggerClassName="aw-composer-plus-button"
        triggerLabel={uploading ? "正在上传" : "添加文件、文件夹与工具"}
      />
      {/* 会话还没开时这两个 input 也不在 DOM 里，不只是菜单里没有那两项。
          它们带着 `aria-label`，所以留着就是留下两个读屏软件念得出来、却没有
          任何入口能触发的控件——比一个 disabled 的按钮更糟，因为连「它现在
          用不了」都没说。 */}
      {sessionId === undefined ? null : (
        <>
        {/* 两个真的 `<input type="file">`，藏在菜单外面而不是菜单项里面。
            菜单项必须是 `role="menuitem"`，而一个 `<label>` 包着 input 的写法——这个
            仓库别处用的、也确实更好的那一种——在菜单里做不成 menuitem，而且键盘按
            Enter 时 label 不会触发它包着的 input（点击会，回车不会）。所以这里退回
            「按钮 + ref.click()」，代价写在这条注释里。 */}
        <input
          aria-label="上传文件到工作区"
          disabled={disabled || uploading}
          multiple
          onChange={(event) => {
            onPickFiles(event.target.files);
            // 清掉，好让同一个文件第二次选中仍然触发 `change`——改完再传一次是很平常
            // 的事，不清的话第二次什么都不会发生。
            event.target.value = "";
          }}
          ref={filesRef}
          type="file"
        />
        <input
          aria-label="上传整个文件夹到工作区"
          disabled={disabled || uploading}
          multiple
          onChange={(event) => {
            onPickFolder(event.target.files);
            event.target.value = "";
          }}
          ref={(node) => {
            folderRef.current = node;
            if (node === null) return;
            // React 不认识这两个属性——它们不在 HTML 标准里，是三个引擎各自实现的
            // 那一个——所以只能拿到真实节点之后写上去。两个都写：`webkitdirectory`
            // 是 Chrome/Safari 认的，`directory` 是标准草案里的名字。
            node.setAttribute("webkitdirectory", "");
            node.setAttribute("directory", "");
          }}
          type="file"
        />
        </>
      )}
    </span>
  );
}
