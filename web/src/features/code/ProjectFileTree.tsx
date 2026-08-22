import { ChevronDown, ChevronRight, File, Folder } from "lucide-react";
import { useCallback, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { listProjectFiles, readProjectFile } from "../../api/client";
import type { ProjectFileEntryView } from "../../api/types";
import { useIdentity } from "../../app/IdentityContext";
import { LoadingLine } from "../../components/ui";

/**
 * 一个项目目录的文件树（ADR-072）。
 *
 * **按层取，不是一次取整棵。** 递归接口是有的，但一棵 `node_modules` 规模的树会
 * 在第一次渲染就把上限吃满，然后这个组件只能显示「被截断了」——而人想看的通常是
 * 前两层。按层取的代价是每展开一个目录一次请求，收益是每次请求的大小由那个目录
 * 决定，而不是由整个项目决定。
 *
 * 展开状态存在组件里而不是 URL 里：它是「我正在看哪儿」，不是「这个页面是什么」，
 * 而深链应该打开一个项目，不是打开某人上次展开过的七个目录。
 */

function Row({
  depth,
  entry,
  expanded,
  onToggle,
  onOpenFile,
  selected,
}: {
  depth: number;
  entry: ProjectFileEntryView;
  expanded: boolean;
  onToggle: () => void;
  onOpenFile: (path: string) => void;
  selected: boolean;
}) {
  const isDirectory = entry.kind === "directory";
  // 只取最后一段来显示。服务端给的是项目内的完整相对路径，那是对的——客户端拿它
  // 原样回传就是合法请求；但树里每一行的缩进已经说明了它在哪一层，再把整条路径
  // 写出来就是同一个信息说两遍，还会把窄侧栏撑爆。
  const name = entry.path.split("/").pop() ?? entry.path;
  return (
    <button
      className={`aw-project-file-row ${selected ? "is-selected" : ""}`}
      onClick={() => (isDirectory ? onToggle() : onOpenFile(entry.path))}
      style={{ paddingLeft: `${8 + depth * 14}px` }}
      title={entry.path}
      type="button"
    >
      {isDirectory ? (
        expanded ? (
          <ChevronDown aria-hidden="true" size={13} />
        ) : (
          <ChevronRight aria-hidden="true" size={13} />
        )
      ) : (
        // 占位，让文件名和同层目录名的左边缘对齐。没有它，文件会因为少一个
        // 折叠箭头而整体左移，看起来像浅了一层。
        <span aria-hidden="true" className="aw-project-file-gutter" />
      )}
      {isDirectory ? (
        <Folder aria-hidden="true" size={13} />
      ) : (
        <File aria-hidden="true" size={13} />
      )}
      <span className="aw-project-file-name">{name}</span>
    </button>
  );
}

function Level({
  depth,
  expanded,
  onOpenFile,
  onToggle,
  path,
  projectId,
  selectedPath,
}: {
  depth: number;
  expanded: ReadonlySet<string>;
  onOpenFile: (path: string) => void;
  onToggle: (path: string) => void;
  path: string;
  projectId: string;
  selectedPath: string | null;
}) {
  const { identity } = useIdentity();
  const listing = useQuery({
    queryKey: ["project-files", identity, projectId, path],
    queryFn: ({ signal }) =>
      listProjectFiles(identity, projectId, { path, signal }),
  });

  if (listing.isPending) return <LoadingLine label="正在读取目录" />;
  if (listing.isError) {
    return (
      <p className="aw-project-file-note">
        这个目录读不到：{String(listing.error)}
      </p>
    );
  }
  if (listing.data.entries.length === 0) {
    return <p className="aw-project-file-note">空目录。</p>;
  }

  return (
    <>
      {listing.data.entries.map((entry) => (
        <div key={entry.path}>
          <Row
            depth={depth}
            entry={entry}
            expanded={expanded.has(entry.path)}
            onOpenFile={onOpenFile}
            onToggle={() => onToggle(entry.path)}
            selected={selectedPath === entry.path}
          />
          {entry.kind === "directory" && expanded.has(entry.path) ? (
            <Level
              depth={depth + 1}
              expanded={expanded}
              onOpenFile={onOpenFile}
              onToggle={onToggle}
              path={entry.path}
              projectId={projectId}
              selectedPath={selectedPath}
            />
          ) : null}
        </div>
      ))}
      {listing.data.truncated ? (
        // 说出来，而不是安静地少画几行。一棵被截断却看起来完整的树，读者得到的
        // 结论是「这个项目就这些文件」——比什么都不显示更糟。
        <p className="aw-project-file-note">
          这一层文件太多，只显示了前 {listing.data.entries.length} 项。
        </p>
      ) : null}
    </>
  );
}

export function ProjectFileTree({
  onOpenFile,
  projectId,
  rootPath,
  selectedPath = null,
}: {
  onOpenFile: (path: string) => void;
  projectId: string;
  rootPath: string;
  selectedPath?: string | null;
}) {
  const [expanded, setExpanded] = useState<ReadonlySet<string>>(new Set());
  const toggle = useCallback((path: string) => {
    setExpanded((current) => {
      const next = new Set(current);
      if (next.has(path)) next.delete(path);
      else next.add(path);
      return next;
    });
  }, []);

  return (
    <section aria-label="项目文件" className="aw-project-files">
      <header>
        <span className="aw-eyebrow">项目目录</span>
        {/* 绝对路径显示出来，因为它是这个界面上唯一能回答「agent 会写到哪」的
            东西。
            省略号要落在**开头**（见 app.css），办法是外层 `direction: rtl`；
            而内容本身必须按 LTR 排。第一版把 `dir="ltr"` 写在这个 `<code>` 上，
            结果属性压过了同一元素上的 CSS，省略号又回到了行尾——正好丢掉能认出
            是哪个项目的那一半。`<bdi>` 只隔离**内容**的方向，不动这个元素自己的
            溢出方向，两件事因此可以各归各。 */}
        <code title={rootPath}>
          <bdi>{rootPath}</bdi>
        </code>
      </header>
      <div className="aw-project-file-list">
        <Level
          depth={0}
          expanded={expanded}
          onOpenFile={onOpenFile}
          onToggle={toggle}
          path=""
          projectId={projectId}
          selectedPath={selectedPath}
        />
      </div>
    </section>
  );
}

/**
 * 被点开的那个项目文件。
 *
 * 单独一个组件而不是塞进树里：树的职责是「有哪些文件」，读一个文件是另一次请求、
 * 另一种失败（太大、不是文本），把两者放在一起会让树的加载态和文件的加载态互相
 * 顶掉。
 */
export function ProjectFileViewer({
  onClose,
  path,
  projectId,
}: {
  onClose: () => void;
  path: string;
  projectId: string;
}) {
  const { identity } = useIdentity();
  const file = useQuery({
    queryKey: ["project-file", identity, projectId, path],
    queryFn: ({ signal }) => readProjectFile(identity, projectId, path, signal),
  });

  return (
    <section aria-label={`文件 ${path}`} className="aw-project-file-view">
      <header>
        <code dir="ltr">{path}</code>
        <button onClick={onClose} type="button">
          关闭
        </button>
      </header>
      {file.isPending ? <LoadingLine label="正在读取文件" /> : null}
      {file.isError ? (
        <p className="aw-project-file-note">读不到这个文件：{String(file.error)}</p>
      ) : null}
      {file.data !== undefined && !file.data.is_text ? (
        // 说它是什么，不把它当文本画出来。二进制用替换字符解出来是一屏 U+FFFD，
        // 而那看起来像是文件坏了——坏的是显示方式。
        <p className="aw-project-file-note">
          这是一个二进制文件（{file.data.size_bytes} 字节），不显示内容。
        </p>
      ) : null}
      {file.data?.is_text === true ? (
        <pre>
          <code>{file.data.text}</code>
        </pre>
      ) : null}
    </section>
  );
}
