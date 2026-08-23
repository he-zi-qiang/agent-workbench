/**
 * The right column: the file you are looking at, and the way to the rest.
 *
 * **它是一栏，不是一个浮层。** 这一点改过两次，所以两次的理由都留在这里。
 *
 * 第一版是常驻的「工作区」——只要会话里有文件就占住 `clamp(320px, 40%, 560px)`，
 * 不管有没有人想看。第二版把它改成抽屉：点开才有，浮在对话上，带一层压暗的
 * 背景。抽屉解决了「没人要看的时候不该占地方」，代价是解决错了方式——它盖住
 * 的正是读者刚刚点开它的理由。想一边读那段话一边看它写出来的文件，做不到：
 * 文件在上面，话在下面，中间还隔着一层灰。
 *
 * 现在是第三版：一栏，但可以折叠，和左边那条导航同一个道理。展开时它把对话
 * 挤窄（对话仍然有 `minmax(0, 1fr)`，不会被挤没），收起时它整栏不存在，宽度
 * 一分不占。折叠状态记在 localStorage 里，所以「我不看文件」和「我一直看着
 * 文件」都只需要表态一次。
 *
 * 两个来源，一栏里：会话工作区里的产出（`viewing`），和项目目录里被点开的
 * 那个文件（`projectFile`）。它们此前各有各的浮层，宽度还不一样（420 和
 * 720），于是同一个动作——「让我看看这个文件」——在屏幕上有两种样子，而且能
 * 互相盖住。合成一栏之后，后点的那个替掉先点的那个，这也是读者本来就以为的
 * 行为。
 *
 * 底下那个折叠的全量列表留着，它不是装饰。四类文件没有卡片，这是它们唯一的
 * 入口：上传进来而不是跑出来的、来自比事件窗口 `KEPT_EVENTS` 更早那一轮的、
 * ADR-063 之前写的（名字在参数摘要里被截断了）、以及尾锚配对丢掉的那些 run
 * 产出的。把它叫「其他文件」会暗示卡片覆盖了剩下的，而卡片没有。
 */

import { PanelRightClose } from "lucide-react";
import type { PrincipalIdentity, WorkspaceEntryView } from "../../api/types";
import { formatSize, IconButton } from "../../components/ui";
import { FilePreview, type OpenedFile } from "./FilePreview";
import { ProjectFileBody } from "./ProjectFileTree";

/** 项目目录里被点开的那个文件。 */
export interface OpenedProjectFile {
  projectId: string;
  path: string;
}

export function PreviewPanel({
  directoryOpen,
  files,
  identity,
  onCollapse,
  onDownload,
  onOpen,
  onWrote,
  orphanRuns,
  projectFile,
  setDirectoryOpen,
  viewing,
}: {
  directoryOpen: boolean;
  files: WorkspaceEntryView[];
  identity: PrincipalIdentity;
  /** 把这一栏收起来。收起是折叠，不是关闭：它记得住。 */
  onCollapse: () => void;
  onDownload: () => void;
  onOpen: (file: WorkspaceEntryView) => void;
  /** A run in here can write files; the page owns the listing they land in. */
  onWrote: (names: string[]) => void;
  /** Runs the pairing could not attribute; surfaced rather than swallowed. */
  orphanRuns: number;
  /** 项目目录里点开的文件，没有就是 null。 */
  projectFile: OpenedProjectFile | null;
  setDirectoryOpen: (open: boolean) => void;
  viewing: OpenedFile | null;
}) {
  // 项目文件优先：它是后点的那个。两个来源共用一栏，谁在上面由「谁是最后被
  // 点开的」决定，而页面在打开任一个时清掉另一个，所以这里最多只有一个非空。
  const heading =
    projectFile !== null
      ? (projectFile.path.split("/").pop() ?? projectFile.path)
      : (viewing?.name ?? "工作区");

  return (
    <aside aria-label="预览" className="aw-code-panel" id="aw-code-panel">
      <header className="aw-drawer-header">
        {/* `title` 带完整的那个：项目文件的标题只取最后一段（路径写全会把
            一条 380px 的栏撑爆），而「它在哪个目录下」在同名文件之间是唯一
            分得清的信息。 */}
        <h2 title={projectFile?.path ?? viewing?.name ?? "工作区"}>
          {heading}
        </h2>
        <div className="aw-drawer-actions">
          {/* 下载只对工作区里的产出给。项目目录里的文件已经在读者自己的硬盘
              上了，给一个「下载」等于把它复制到 ~/Downloads 再放一份。 */}
          {viewing === null || projectFile !== null ? null : (
            <button className="aw-button" onClick={onDownload} type="button">
              下载
            </button>
          )}
          <IconButton
            className="aw-code-panel-collapse"
            expanded
            label="收起预览栏"
            onClick={onCollapse}
          >
            <PanelRightClose aria-hidden size={17} />
          </IconButton>
        </div>
      </header>

      {projectFile !== null ? (
        <div className="aw-drawer-body">
          <ProjectFileBody
            path={projectFile.path}
            projectId={projectFile.projectId}
          />
        </div>
      ) : viewing === null ? null : (
        <section aria-label={`文件 ${viewing.name}`} className="aw-drawer-body">
          <FilePreview
            files={files}
            identity={identity}
            // Converted here rather than asking the page for a second
            // callback: this panel already holds the listing a name has to be
            // resolved against, and a name that is not in it is a name this
            // panel could not have drawn a card for.
            onOpen={(name) => {
              const entry = files.find((held) => held.name === name);
              if (entry !== undefined) onOpen(entry);
            }}
            onWrote={onWrote}
            viewing={viewing}
          />
        </section>
      )}

      <details
        className="aw-code-directory"
        onToggle={(event) => {
          setDirectoryOpen(event.currentTarget.open);
        }}
        open={directoryOpen}
      >
        <summary>
          工作区全部文件（{files.length}）
          {/* Said out loud rather than left as a silent gap. Non-zero means
              another tab ran turns in this session, so the stream holds runs
              this transcript has no instruction for and their cards were
              dropped rather than guessed onto the wrong turn. */}
          {orphanRuns === 0 ? null : (
            <span className="aw-code-value">
              （有 {orphanRuns} 轮的产出没能归位）
            </span>
          )}
        </summary>
        {files.length === 0 ? (
          <p className="aw-code-workspace-empty">还没有文件。</p>
        ) : (
          <ul>
            {files.map((file) => (
              <li key={file.name}>
                <button
                  aria-current={
                    file.name === viewing?.name ? "true" : undefined
                  }
                  className="aw-code-file-open"
                  onClick={() => {
                    onOpen(file);
                  }}
                  type="button"
                >
                  <span className="aw-code-file-name">{file.name}</span>
                  <span className="aw-code-value">
                    {formatSize(file.size_bytes)}
                  </span>
                </button>
              </li>
            ))}
          </ul>
        )}
      </details>
    </aside>
  );
}
