/**
 * 会话页头右端那颗 ⋮。
 *
 * 它收的是**这一段会话**的动作，和输入框那颗「+」收的「给这一轮加点什么」分得干净：
 * 一个作用在会话上（改名、删掉、看它产出了什么），一个作用在下一句话上。此前这两
 * 类东西混在一起——改名只在左栏那一行的悬停里有，而「工作区 N」是页头上一颗单独的
 * 按钮，删除则只有左栏够得着。
 *
 * **少画了四项，四项都是有意的。** 对着 Claude Desktop 那颗 ⋮ 逐条看：
 *
 * * `Fork`：这个部署没有分叉一段会话的能力。一段会话是 `conversation_sessions` 的
 *   一行加一串消息，复制它需要一条谁都还没写的路由。
 * * `Archive`：没有归档位。会话只有「在」和「删了」两种状态。
 * * `Transcript view`：转录已经在屏幕正中间了，这一项在这里没有第二个视图可切。
 * * `iOS Simulator` / `Open in`：这个进程环回绑定在这台机器上（ADR-044），但它没有
 *   任何一条「用别的程序打开」的出口。
 *
 * 画出来会让人以为可以点。这条规矩这个仓库写过好几遍，最近一次是 `.aw-code-picks`
 * 那三枚 chip——稿子上有五枚，而 `ProjectView` 里没有分支和 worktree 这两个概念。
 */

import { FolderTree, Pencil, PanelRightOpen, Trash2 } from "lucide-react";
import { MoreVertical } from "lucide-react";
import { Menu, type MenuEntry } from "../../components/Menu";

export function SessionMenu({
  fileCount,
  onDelete,
  onRename,
  onShowFolder,
  onShowWorkspace,
}: {
  fileCount: number;
  onDelete: () => void;
  onRename: () => void;
  /** `null` 表示这段会话没有目录可看。 */
  onShowFolder: (() => void) | null;
  /** `null` 表示工作区还是空的，没有可看的东西。 */
  onShowWorkspace: (() => void) | null;
}) {
  const entries: MenuEntry[] = [
    ...(onShowWorkspace === null
      ? []
      : ([
          {
            kind: "action",
            id: "workspace",
            label: "工作区文件",
            // 截图上这一项叫 Artifacts。这里叫工作区文件，因为在这一侧它们**就是**
            // 同一样东西——ADR-088 的题目就是这句话：一条工作集条目已经是一个产物。
            // 借那个词会让人以为还有第二个存放处。
            icon: <PanelRightOpen aria-hidden size={15} />,
            trailing: String(fileCount),
            onSelect: onShowWorkspace,
          },
        ] satisfies MenuEntry[])),
    ...(onShowFolder === null
      ? []
      : ([
          {
            kind: "action",
            id: "folder",
            label: "项目目录",
            icon: <FolderTree aria-hidden size={15} />,
            onSelect: onShowFolder,
          },
        ] satisfies MenuEntry[])),
    ...(onShowWorkspace === null && onShowFolder === null
      ? []
      : [{ kind: "separator", id: "r1" } as MenuEntry]),
    {
      kind: "action",
      id: "rename",
      label: "重命名",
      icon: <Pencil aria-hidden size={15} />,
      onSelect: onRename,
    },
    {
      kind: "action",
      id: "delete",
      label: "删除这段会话",
      icon: <Trash2 aria-hidden size={15} />,
      danger: true,
      onSelect: onDelete,
    },
  ];

  return (
    <Menu
      align="end"
      entries={entries}
      label="这段会话"
      placement="bottom"
      trigger={<MoreVertical aria-hidden size={17} />}
      triggerClassName="aw-session-menu-button"
      triggerLabel="这段会话的更多操作"
    />
  );
}
