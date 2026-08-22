import { useQuery } from "@tanstack/react-query";
import { ChevronRight, CornerLeftUp, Folder, FolderOpen } from "lucide-react";
import { useState } from "react";
import { browseDirectories } from "../../api/client";
import { useIdentity } from "../../app/IdentityContext";
import { LoadingLine } from "../../components/ui";

/**
 * 在服务端的文件系统上选一个目录（ADR-074）。
 *
 * **为什么是服务端浏览，而不是系统的文件选择框。** Claude Desktop 用的是 Electron
 * 的 `dialog.showOpenDialog`，而在 macOS 上那个框不只是 UI——它**就是授权**（TCC）：
 * 用户在框里选中一个文件夹，应用才拿到那个文件夹的权限。这就是为什么它还带一条
 * 重选流程（"Claude lost permission to access X. Select the folder again."）。
 *
 * 这个控制台是浏览器页面，那条机制拿不到：`showDirectoryPicker()` 给的是一个
 * handle，**永远不给绝对路径**，而服务端要的正是绝对路径。所以浏览发生在持有磁盘
 * 的那一端，这里画的是它的返回。
 *
 * 换来的差别写在 `adapters/filesystem/browser.py`：原生框在用户动手之前什么都不
 * 枚举，而这个端点可以按请求枚举目录**名字**。它只给名字、只给目录、且进程是环回
 * 绑定的本地开发进程（ADR-044）。
 *
 * CLI 那条路没有这个问题：进程已经在目录里，绝对路径就是 cwd，`agent-cli project
 * use` 因此不需要任何选择器。
 */
export function FolderPicker({
  busy = false,
  onCancel,
  onChoose,
}: {
  busy?: boolean;
  onCancel?: () => void;
  onChoose: (path: string) => void;
}) {
  // `null` 表示「还没导航过」，由服务端决定从哪开始（家目录）。不在这里写死
  // `~`：家目录是服务端那台机器的事实，客户端猜它等于多一个会过时的答案。
  const [path, setPath] = useState<string | null>(null);
  const { identity } = useIdentity();
  const listing = useQuery({
    queryKey: ["directories", identity, path],
    queryFn: ({ signal }) => browseDirectories(identity, { path, signal }),
    // 目录内容随时会变，而这个选择器是一次性的动作。缓存旧的一层会让人看到一个
    // 刚被删掉的文件夹然后选中它。
    staleTime: 0,
  });

  const current = listing.data?.path ?? null;
  const parent = listing.data?.parent ?? null;

  return (
    <section aria-label="选择文件夹" className="aw-folder-picker">
      <header>
        <FolderOpen aria-hidden="true" size={15} />
        {/* 当前位置用只读文本而不是输入框：这个控件的契约是「浏览着选」，
            而一个可编辑的路径框会让人以为可以打字，然后在打错时得到一个
            服务端的路径错误——那是 CLI 的交互，不是这里的。 */}
        <code dir="ltr" title={current ?? undefined}>
          {current ?? "…"}
        </code>
      </header>

      <div className="aw-folder-picker-list">
        {listing.isPending ? <LoadingLine label="正在读取目录" /> : null}
        {listing.isError ? (
          <p className="aw-project-file-note">
            这个目录打不开：{String(listing.error)}
          </p>
        ) : null}

        {parent !== null ? (
          <button
            className="aw-folder-picker-row is-up"
            disabled={busy}
            // 取到局部再用，而不是在回调里读 `listing.data.parent`：那个回调在
            // 点击时才跑，那时 `listing.data` 已经可能是另一次查询的结果。
            onClick={() => setPath(parent)}
            type="button"
          >
            <CornerLeftUp aria-hidden="true" size={14} />
            <span>上一级</span>
          </button>
        ) : null}

        {listing.data?.entries.map((entry) => (
          <button
            className="aw-folder-picker-row"
            disabled={busy}
            key={entry.path}
            onClick={() => setPath(entry.path)}
            title={entry.path}
            type="button"
          >
            <Folder aria-hidden="true" size={14} />
            <span className="aw-folder-picker-name">{entry.name}</span>
            <ChevronRight aria-hidden="true" size={13} />
          </button>
        ))}

        {listing.data?.entries.length === 0 ? (
          // 空目录是完全可以选的——新建一个空文件夹再开始编码是常见做法。
          // 所以这句说的是「这里面没有子目录」，不是「这里不能选」。
          <p className="aw-project-file-note">这个文件夹里没有子文件夹。</p>
        ) : null}

        {listing.data?.truncated === true ? (
          <p className="aw-project-file-note">
            子文件夹太多，只列出了前 {listing.data.entries.length} 个。
          </p>
        ) : null}
      </div>

      <footer>
        {onCancel === undefined ? null : (
          <button disabled={busy} onClick={onCancel} type="button">
            取消
          </button>
        )}
        {/* 选中的是**当前所在的**目录，不是列表里某一行。这和系统的文件选择框
            是同一个约定：你走进一个文件夹，然后按「选择」。列表里的行是走进去，
            不是选中——两者用一个控件表达会让「点一下」有两种后果。 */}
        <button
          className="aw-primary-button"
          disabled={busy || current === null}
          onClick={() => current !== null && onChoose(current)}
          type="button"
        >
          {busy ? "正在打开…" : "使用这个文件夹"}
        </button>
      </footer>
    </section>
  );
}
