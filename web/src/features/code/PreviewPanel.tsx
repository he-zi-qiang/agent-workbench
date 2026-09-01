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
 * **四样东西，四张标签。** 这一栏里住着四份互不相同的东西：这个项目在磁盘上
 * 长什么样（文件夹）、正在看的那一个（预览）、这段会话自己产出和收到的那些
 * （本次会话），以及底下那条没被加工过的事件流（事件）。前三份此前是往下堆
 * 的——预览在上、目录在中、会话文件收在一个 `<details>` 里——于是最后那一份要
 * 滚过前两份才看得见，而它是四类文件唯一的入口。标签页把「有哪些」和「现在看
 * 哪个」分开，四份因此地位相同。
 *
 * **「本次会话」这个名字是有意的。** 它此前叫「工作区全部文件」，而旁边就是
 * 「项目目录」——两个名字都在说「文件」，谁也没说自己和对方差在哪，于是它们
 * 看起来是重复的两份。差别是**来源**：一份是这段会话产出或收到的，另一份是
 * 磁盘上本来就有的。名字说出来源，重复感就消失了，因为它本来就不是重复。
 *
 * 那一份留着，它不是装饰。四类文件没有卡片，这是它们唯一的入口：上传进来而
 * 不是跑出来的、来自比事件窗口 `KEPT_EVENTS` 更早那一轮的、ADR-063 之前写的
 * （名字在参数摘要里被截断了）、以及尾锚配对丢掉的那些 run 产出的。
 */

import { PanelRightClose } from "lucide-react";
import type {
  EventEnvelope,
  PrincipalIdentity,
  WorkspaceEntryView,
} from "../../api/types";
import { EventLog } from "../../components/EventLog";
import { PanelTabs } from "../../components/PanelTabs";
import { formatSize, IconButton } from "../../components/ui";
import { FilePreview, type OpenedFile } from "./FilePreview";
import { ProjectFileBody } from "./ProjectFileTree";

/** 项目目录里被点开的那个文件。 */
export interface OpenedProjectFile {
  projectId: string;
  path: string;
  /** 目录列表那一行给的字节数——预览要在取正文之前拿它拒绝太大的文件。 */
  sizeBytes: number;
}

export function PreviewPanel({
  directory,
  events,
  files,
  identity,
  onCollapse,
  onDownload,
  onOpen,
  onWrote,
  orphanRuns,
  onTab,
  projectFile,
  tab,
  viewing,
}: {
  /**
   * 这个项目的目录树，由页面构造好传进来。
   *
   * 传元素而不是传 projectId/rootPath：这块面已经在管两个来源（会话产出的文件、
   * 点开的那个文件），再让它自己去查第三个，就得知道项目、写入路径和选中项怎么
   * 来的。它画，页面知道。
   *
   * 它此前长在**左**边的会话栏里。一条 260px 宽的侧栏同时装「这个项目有哪些
   * 文件」和「我开过哪些会话」，两份列表互相挤，而它们回答的是完全不同的两个
   * 问题。目录属于「我在改哪个文件夹」，那和右边这一栏是同一件事。
   */
  directory: React.ReactNode;
  /**
   * 这段会话留下的持久事件，原样。
   *
   * 传的是 `steps`——页面已经拿它画对话了，这里不再去要第二份。它是这一栏里唯一
   * 不解释的东西：其余几张画的是「这些事件合起来意味着什么」，而它们不够用的时
   * 刻是存在的（一次运行说成功而文件没出来、一颗工具卡住而对话上什么也没写）。
   */
  events: readonly EventEnvelope[];
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
  onTab: (id: string) => void;
  /** 项目目录里点开的文件，没有就是 null。 */
  projectFile: OpenedProjectFile | null;
  /**
   * 现在停在哪一张，`null` 表示读者还没表过态（落到 `fallback` 上）。
   *
   * 由页面持有而不是这块面自己 `useState`：页面上有三个入口要求跳到指定的一张
   * （「看这个文件夹」、「工作区 N」那颗开关、会话菜单里那两项）。这块面自己
   * 存的话，那三个入口就得靠一个 ref 或者一个 key 去伸手改它的内部状态——两种
   * 都是把「谁说了算」写得比现在含糊。
   */
  tab: string | null;
  viewing: OpenedFile | null;
}) {
  // 项目文件优先：它是后点的那个。两个来源共用一栏，谁在上面由「谁是最后被
  // 点开的」决定，而页面在打开任一个时清掉另一个，所以这里最多只有一个非空。
  const opened = projectFile !== null || viewing !== null;
  const heading =
    projectFile !== null
      ? (projectFile.path.split("/").pop() ?? projectFile.path)
      : (viewing?.name ?? "工作区");

  // 读者还没表过态时落在哪一张。
  //
  // 「点开一个文件就跳到预览」不在这里——它由页面在两个打开回调里说出来
  // （`setPanelTab("preview")`），因为那时 `tab` 通常已经有值，而有值就压过这个
  // 默认。这里只管一件事：一栏刚展开、还没人点过任何东西时，先给他看什么。
  const fallback =
    opened ? "preview" : directory === null ? "workspace" : "directory";

  return (
    <aside aria-label="预览" className="aw-code-panel" id="aw-code-panel">
      <PanelTabs
        active={tab ?? fallback}
        entries={[
          {
            id: "directory",
            label: "文件夹",
            available: directory !== null,
            body: directory,
          },
          {
            id: "preview",
            label: "预览",
            available: opened,
            body: (
              <>
                <header className="aw-drawer-header">
                  {/* `title` 带完整的那个：项目文件的标题只取最后一段（路径写
                      全会把这一栏撑爆），而「它在哪个目录下」在同名文件之间是
                      唯一分得清的信息。 */}
                  <h2 title={projectFile?.path ?? viewing?.name ?? "工作区"}>
                    {heading}
                  </h2>
                  {/* 下载只对工作区里的产出给。项目目录里的文件已经在读者自己
                      的硬盘上了，给一个「下载」等于把它复制到 ~/Downloads 再
                      放一份。 */}
                  {viewing === null || projectFile !== null ? null : (
                    <button
                      className="aw-button"
                      onClick={onDownload}
                      type="button"
                    >
                      下载
                    </button>
                  )}
                </header>
                {projectFile !== null ? (
                  <div className="aw-drawer-body">
                    <ProjectFileBody
                      path={projectFile.path}
                      projectId={projectFile.projectId}
                      sizeBytes={projectFile.sizeBytes}
                    />
                  </div>
                ) : viewing === null ? null : (
                  <section
                    aria-label={`文件 ${viewing.name}`}
                    className="aw-drawer-body"
                  >
                    <FilePreview
                      files={files}
                      identity={identity}
                      // Converted here rather than asking the page for a second
                      // callback: this panel already holds the listing a name
                      // has to be resolved against, and a name that is not in
                      // it is a name this panel could not have drawn a card for.
                      onOpen={(name) => {
                        const entry = files.find((held) => held.name === name);
                        if (entry !== undefined) onOpen(entry);
                      }}
                      onWrote={onWrote}
                      viewing={viewing}
                    />
                  </section>
                )}
              </>
            ),
          },
          {
            id: "events",
            label: "事件",
            count: events.length,
            available: events.length > 0,
            body: <EventLog events={events} />,
          },
          {
            id: "workspace",
            label: "本次会话",
            count: files.length,
            body: (
              <div className="aw-code-workspace">
                {files.length === 0 ? (
                  <p className="aw-code-workspace-empty">
                    这段会话还没有产出或收到文件。那个文件夹里本来就有的东西在「文件夹」那一张。
                  </p>
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
                {/* Said out loud rather than left as a silent gap. Non-zero
                    means another tab ran turns in this session, so the stream
                    holds runs this transcript has no instruction for and their
                    cards were dropped rather than guessed onto the wrong turn. */}
                {orphanRuns === 0 ? null : (
                  <p className="aw-code-workspace-empty">
                    有 {orphanRuns} 轮的产出没能归位。
                  </p>
                )}
              </div>
            ),
          },
        ]}
        label="右栏的几张"
        onSelect={onTab}
        trailing={
          <IconButton
            expanded
            label="收起预览栏"
            onClick={onCollapse}
          >
            <PanelRightClose aria-hidden size={17} />
          </IconButton>
        }
      />
    </aside>
  );
}
