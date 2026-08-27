import { ChevronDown, ChevronRight, File, Folder } from "lucide-react";
import { useCallback, useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  MAX_PREVIEW_BYTES,
  listProjectFiles,
  readProjectFile,
} from "../../api/client";
import type { ProjectFileEntryView } from "../../api/types";
import { useIdentity } from "../../app/IdentityContext";
import { HtmlPreview } from "../../components/HtmlPreview";
import { effectiveMediaType, previewKind } from "../../components/media";
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
  touched,
}: {
  depth: number;
  entry: ProjectFileEntryView;
  expanded: boolean;
  onToggle: () => void;
  onOpenFile: (entry: ProjectFileEntryView) => void;
  selected: boolean;
  /** 这段会话写过它（文件），或写过它里面的东西（目录）。 */
  touched: boolean;
}) {
  const isDirectory = entry.kind === "directory";
  // 只取最后一段来显示。服务端给的是项目内的完整相对路径，那是对的——客户端拿它
  // 原样回传就是合法请求；但树里每一行的缩进已经说明了它在哪一层，再把整条路径
  // 写出来就是同一个信息说两遍，还会把窄侧栏撑爆。
  const name = entry.path.split("/").pop() ?? entry.path;
  return (
    <button
      className={`aw-project-file-row ${selected ? "is-selected" : ""}`}
      onClick={() => (isDirectory ? onToggle() : onOpenFile(entry))}
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
      {/* 一个点，不是一个颜色。这一行本来就靠底色区分选中态，再用色相说第二
          件事，读者要同时分辨两种「这一行不一样」，而其中一种对色觉障碍者不
          存在。点是形状，`title` 和 sr-only 文本是它的名字——三条路说同一件
          事，任何一条单独成立。 */}
      {touched ? (
        <span
          className="aw-project-file-touched"
          title="这段会话写过它"
        >
          <span className="aw-sr-only">这段会话写过它</span>
        </span>
      ) : null}
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
  touched,
}: {
  depth: number;
  expanded: ReadonlySet<string>;
  onOpenFile: (entry: ProjectFileEntryView) => void;
  onToggle: (path: string) => void;
  path: string;
  projectId: string;
  selectedPath: string | null;
  touched: ReadonlySet<string>;
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
            touched={touched.has(entry.path)}
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
              touched={touched}
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

/**
 * 一条项目内路径和它上面每一层目录。
 *
 * `docs/a/b.md` → `docs/a/b.md`、`docs/a`、`docs`。写入要在树上留下痕迹，而
 * 读者看得见的那一层通常不是文件本身所在的那一层：一个刚被写进 `src/x/y.py`
 * 的文件，在树收着的时候唯一能说话的位置是 `src` 那一行。
 */
function pathAndAncestors(path: string): string[] {
  const segments = path.split("/").filter((segment) => segment !== "");
  const out: string[] = [];
  for (let end = segments.length; end > 0; end -= 1) {
    out.push(segments.slice(0, end).join("/"));
  }
  return out;
}

export function ProjectFileTree({
  onOpenFile,
  projectId,
  rootPath,
  selectedPath = null,
  writtenPaths,
}: {
  /**
   * 整行传出去，不只是 `entry.path`。
   *
   * 预览要在**取正文之前**知道大小才能拒绝一个太大的文件，而这一行是这个
   * 事实唯一免费的来源：目录列表已经带着它了。只传路径的话，那次拒绝只能
   * 改成「读回来之后发现太大」，也就是先把它放进内存再说不。
   */
  onOpenFile: (entry: ProjectFileEntryView) => void;
  projectId: string;
  rootPath: string;
  selectedPath?: string | null;
  /**
   * 这段会话写过的项目内路径（ADR-086）。
   *
   * 只进不出，也只说「看见过」：它来自事件流，而事件流有窗口，所以少标一个
   * 是可能的，标错一个不是。`turnBlocks.ts` 的 `projectWritesIn` 那段注释里
   * 说的是同一件事的另一半。
   */
  writtenPaths: readonly string[];
}) {
  // 两块状态，都记**读者说过的话**，而不是「现在是开还是关」。
  //
  // 上一版只有一个 `expanded`，再加一个 effect 把写入的祖先目录塞进去。那样有
  // 两个毛病，而 lint 只报了后一个：写进去的东西要多一帧才浮上来；以及
  // 「读者收起过它」得靠第三块 ref 记着，否则下一帧又被塞回去。
  //
  // 现在展开与否是**渲染时算出来的**：写入的祖先默认是开的，读者显式收起过的
  // 一定是关的，读者显式展开过的一定是开的。没有 effect，因此也没有那一帧；
  // 「收起过的不会自己弹回来」不再是一条要维护的规则，而是集合减法的结果。
  //
  // 由此还带来一个决定：一个读者收起过的目录，**即使这一轮又往它里面写了东西
  // 也不会自己打开**。这是对的——他说过不想看这一支，而行尾那个点仍然在收起的
  // 那一行上，所以「这里面有新东西」照样说得出来。
  const [opened, setOpened] = useState<ReadonlySet<string>>(new Set());
  const [closed, setClosed] = useState<ReadonlySet<string>>(new Set());

  // 文件本身，加上它上面每一层目录。目录也要标，否则一个写进三层深处的产物
  // 在树收着的时候完全不存在——而「树上什么都没变」正是这次要修的那句话。
  const touched = useMemo(() => {
    const marked = new Set<string>();
    for (const path of writtenPaths) {
      for (const step of pathAndAncestors(path)) marked.add(step);
    }
    return marked;
  }, [writtenPaths]);

  const expanded = useMemo(() => {
    const open = new Set(opened);
    for (const path of writtenPaths) {
      // `slice(1)`：第一项是文件自己，展开一个文件没有意义。
      for (const step of pathAndAncestors(path).slice(1)) open.add(step);
    }
    for (const path of closed) open.delete(path);
    return open;
  }, [closed, opened, writtenPaths]);

  // `expanded` 在依赖里，因为「点一下是开还是关」要看它此刻算出来是什么——
  // 读一个只记着读者自己那一半的集合，会让第一次点击一个被自动展开的目录变成
  // 「再展开一次」，也就是什么都没发生。
  const toggle = useCallback(
    (path: string) => {
      if (expanded.has(path)) {
        setOpened((current) => {
          const next = new Set(current);
          next.delete(path);
          return next;
        });
        setClosed((current) => new Set(current).add(path));
        return;
      }
      setClosed((current) => {
        const next = new Set(current);
        next.delete(path);
        return next;
      });
      setOpened((current) => new Set(current).add(path));
    },
    [expanded],
  );

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
          touched={touched}
        />
      </div>
    </section>
  );
}

/**
 * 被点开的那个项目文件的正文。
 *
 * 单独一个组件而不是塞进树里：树的职责是「有哪些文件」，读一个文件是另一次请求、
 * 另一种失败（太大、不是文本），把两者放在一起会让树的加载态和文件的加载态互相
 * 顶掉。
 *
 * 只有正文，没有自己的头部和关闭键。它此前是一个自带页眉、浮在对话上、宽
 * 720px 的独立浮层，而右边那块预览栏是另一个宽 420px 的浮层——同一个动作
 * （「让我看看这个文件」）在屏幕上有两种样子，还能互相盖住。现在它是
 * `PreviewPanel` 里的一块正文，文件名和收起键归那一栏的页眉管，两个来源
 * 因此长得一模一样。
 *
 * `aria-label` 留着：这一块在那一栏里仍然是一个有名字的区域，而名字里带着
 * 完整相对路径——页眉只放得下最后一段。
 *
 * ## 为什么这里不再是一个 `<pre>`
 *
 * 它曾经只是。项目目录里的每一个文件——包括 agent 上一轮刚写出来的那个
 * `report.html`——都只以源码的样子出现；而同一个 `.html` 落在会话工作区里
 * 时，`FilePreview` 给的是沙箱 iframe 里渲染好的页面，带渲染／源码切换和
 * 全屏。同一份字节，两种待遇，分界线是它被写到哪一侧。
 *
 * 这条线对读者不可见，而且正好和他要问的问题相反：他点开的时候想的是
 * 「让我看看这个产物」，不是「让我看看这个产物碰巧存在哪一侧」。更糟的是
 * 分界线偏得很稳定——`config.demo-local.toml` 下每段会话都有项目目录
 * （ADR-072/074），所以**默认那一侧恰好是没有预览的那一侧**。
 *
 * 现在两侧共用同一张分派表（`previewKind`）和同一批查看器组件。这不是为了
 * 少写几行：`FilePreview` 的注释里已经为工作区那侧写过同一句理由——两份
 * 拷贝会在这个组件唯一要回答的问题上分叉，而分叉表现为同一个文件在两处
 * 长得不一样。
 *
 * ## 为什么分派看的是名字，正文却听服务端的
 *
 * 项目文件在服务端没有 media type，磁盘上的文件没人给它标过，所以「用哪个
 * 查看器」只能按后缀猜。但「它到底是不是文本」不猜：`ProjectFileContent.
 * is_text` 是服务端解码的结果，而项目目录里到处是没有后缀的文本文件
 * （`Makefile`、`.gitignore`、`LICENSE`）——把它们按「未知类型」拒掉，是拿
 * 一个已经答得出的问题去换一次猜测。
 */
export function ProjectFileBody({
  path,
  projectId,
  sizeBytes,
}: {
  path: string;
  projectId: string;
  /**
   * 从目录列表那一行拿的，不是读回来之后才知道的。
   *
   * 顺序就是理由：下面这条上限要挡的是「整份正文进内存」，读完再说太大等于
   * 已经付过一次了。会话工作区那一侧用清单里的字节数做同一件事
   * （`FilePreview` / `BlobPreview`），这里用目录列表里的。
   */
  sizeBytes: number;
}) {
  const { identity } = useIdentity();
  const name = path.split("/").pop() ?? path;
  // 项目文件在服务端没有 media type——它就是磁盘上的一个文件，没人给它标过。
  // 所以按名字猜，猜法用的是全站同一张表：`effectiveMediaType` 本来就是为
  // 「存的类型什么也没说」这一种情况写的。
  const kind = previewKind(effectiveMediaType("", name));
  // 服务端另有一条 2 MiB 的读上限（`ports/project_files.py` MAX_READ_BYTES），
  // 它答的是别的问题——「这次读能不能做」。这里答的是「这一屏要不要展开」，
  // 两个数不一样，也不该合并。
  const oversized = sizeBytes > MAX_PREVIEW_BYTES;
  const file = useQuery({
    enabled: !oversized,
    queryKey: ["project-file", identity, projectId, path],
    queryFn: ({ signal }) => readProjectFile(identity, projectId, path, signal),
  });

  const body = file.data;
  return (
    <section aria-label={`文件 ${path}`} className="aw-project-file-view">
      {oversized ? (
        <p className="aw-project-file-note">这个文件太大，页面里不展开。</p>
      ) : null}
      {file.isPending && !oversized ? (
        <LoadingLine label="正在读取文件" />
      ) : null}
      {file.isError ? (
        <p className="aw-project-file-note">读不到这个文件：{String(file.error)}</p>
      ) : null}
      {body !== undefined && !body.is_text ? (
        // 说它是什么，不把它当文本画出来。二进制用替换字符解出来是一屏 U+FFFD，
        // 而那看起来像是文件坏了——坏的是显示方式。
        //
        // 图片和 PDF 也停在这句话上，而这不是忘了接。取字节的路由不存在：
        // `GET /v1/projects/{id}/file` 给的是解码后的文本
        // （`ports/project_files.py` 的 `ProjectFileContent`），补一条要动
        // `ProjectFileStore` 这个 Protocol、它的实现和 `tests/contracts/` 的
        // 参数化套件（ADR-086 §4，缺口记在 known-gaps）。
        //
        // 这里**不要**写成「agent 往项目目录里放不进二进制」。那句话看起来成
        // 立——`project_write` 的入参确实是 `str`——但 ADR-077 之后回合还握着
        // `project_run`，一条命令能在项目目录里写出任何东西，两个 local
        // profile 都打开了 `policy.shell_tools_enabled`。所以这一侧的二进制
        // 可以是产物，只是接它的成本落在别处。
        <p className="aw-project-file-note">
          这是一个二进制文件（{body.size_bytes} 字节），不显示内容。
        </p>
      ) : null}
      {body?.is_text === true && kind === "html" ? (
        // 已经读回来的字节，交给和工作区那侧同一个查看器。`load` 立即 resolve，
        // 所以这里没有第二次请求——`HtmlPreview` 自己的 `useQuery` 拿的是一个
        // 已完成的 promise，留着它是为了不动那个组件的契约。
        //
        // `modified_at` 在键里：那个查询是 `staleTime: Infinity`，没有它，
        // 文件改过之后外层重新读到的新正文进不了内层那份缓存，读者看到的还是
        // 上一版渲染结果。
        <HtmlPreview
          load={() => Promise.resolve({ text: body.text ?? "", truncated: false })}
          name={name}
          queryKey={[
            "project-file-html",
            identity,
            projectId,
            path,
            body.modified_at,
          ]}
          sizeBytes={sizeBytes}
        />
      ) : null}
      {body?.is_text === true && kind !== "html" ? (
        <pre>
          <code>{body.text}</code>
        </pre>
      ) : null}
    </section>
  );
}
