import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ChevronRight,
  CornerLeftUp,
  Folder,
  FolderOpen,
  FolderPlus,
  X,
} from "lucide-react";
import { useRef, useState, type FormEvent } from "react";
import { ApiError, browseDirectories, createDirectory } from "../../api/client";
import { useIdentity } from "../../app/IdentityContext";
import { ErrorNotice, LoadingLine } from "../../components/ui";

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
 *
 * **每一行自己能被选中，这是改过的。** 上一版照抄了系统文件选择框的约定：走进一个
 * 文件夹，然后按页脚的「使用这个文件夹」。那条注释当时的理由是「点一下不该有两种
 * 后果」——这个担心成立，但它假定了两个后果抢同一个像素。给它们各自的靶子之后就
 * 不抢了：点名字走进去，点行尾那枚「选这个」选中它。代价是每一行多一个可聚焦的
 * 控件，换来的是那个最常见的动作（「就是列表里这一个」）不再需要先走进去、再把视
 * 线移到对角线另一头的页脚。当前这一层仍然选得中，按钮搬到了标题栏那个路径旁边，
 * 挨着它描述的那个东西。
 *
 * **能新建，这是补上的。** ADR-074 写的起始屏是「选一个文件夹，或新建」，而这个
 * 选择器此前只做了前半句：一个想从空文件夹开始的人，得先去终端或访达里 `mkdir`，
 * 再回到这里找到它。系统的文件选择框都带一颗「新建文件夹」，理由和这里一样——
 * 「在哪里干活」这个问题的答案常常还不存在。
 *
 * 建好之后**走进去**，而不是直接选中它：这是 Finder 和 NSOpenPanel 的约定，也留住
 * 了「选中」只有一种发生方式（标题栏那颗按钮或行尾那枚）。走进去之后那颗
 * 「就用这一层」拿到焦点，所以键盘上是：输入名字、回车、再回车——三下，中间
 * 不用找任何东西。
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
  const [naming, setNaming] = useState(false);
  const [draftName, setDraftName] = useState("");
  // 点开头的文件夹默认藏起来。家目录里它们排在最前面——实测这台机器上是
  // `.antigravity`、`.cache`、`.claude`……十五个，一屏都是它们，真正的文件夹要滚
  // 过去才看得见。Finder 的选择框就是藏的。后端照旧只列名字、什么都不判断；藏不
  // 藏是这一边的产品选择，所以一行「显示」把它们还回来，不是永远看不见。
  const [showHidden, setShowHidden] = useState(false);
  const { identity } = useIdentity();
  const queries = useQueryClient();
  const hereRef = useRef<HTMLButtonElement | null>(null);
  const listing = useQuery({
    queryKey: ["directories", identity, path],
    queryFn: ({ signal }) => browseDirectories(identity, { path, signal }),
    // 目录内容随时会变，而这个选择器是一次性的动作。缓存旧的一层会让人看到一个
    // 刚被删掉的文件夹然后选中它。
    staleTime: 0,
  });

  const current = listing.data?.path ?? null;
  const parent = listing.data?.parent ?? null;
  const entries = listing.data?.entries ?? [];
  const hiddenCount = entries.filter((entry) => entry.name.startsWith(".")).length;
  const shown = showHidden
    ? entries
    : entries.filter((entry) => !entry.name.startsWith("."));

  const create = useMutation({
    mutationFn: (name: string) => {
      if (current === null) throw new Error("还没读到当前目录");
      return createDirectory(identity, { parent: current, name });
    },
    onSuccess: async (made) => {
      setNaming(false);
      setDraftName("");
      // 这一层的列表已经变了；别的层没变，但这个查询本来就是 `staleTime: 0`，
      // 整个前缀一起作废最省事也最不会漏。
      await queries.invalidateQueries({ queryKey: ["directories", identity] });
      setPath(made.path);
      // 建完就站在新文件夹里，而下一步九成是「就用它」。焦点直接落在那颗按钮
      // 上，回车即选中；不想用的话 Tab 一下就走开了，什么也没替读者决定。
      window.requestAnimationFrame(() => hereRef.current?.focus());
    },
  });

  // 服务端也会拒，但那一趟是能省的：一个名字里带斜杠的请求注定失败，而失败的
  // 那句话是服务端的英文。这里只拦最明显的三种，其余仍交给服务端——它是唯一
  // 知道那块盘规矩的一方。
  const nameProblem = (() => {
    const trimmed = draftName.trim();
    if (trimmed === "") return null;
    if (trimmed === "." || trimmed === "..") {
      return "「.」和「..」不是名字，它们指的是这一层和上一层。";
    }
    if (/[\\/]/.test(trimmed)) return "名字里不能有斜杠——一次只建一层。";
    return null;
  })();

  const submitName = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const trimmed = draftName.trim();
    if (trimmed === "" || nameProblem !== null || create.isPending) return;
    create.mutate(trimmed);
  };

  const closeNaming = () => {
    setNaming(false);
    setDraftName("");
    create.reset();
  };

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
        {/* 「我要的就是现在站着这一层」。没有它，选中当前目录就得先退到上一级
            再从列表里点回来——一个纯粹由控件布局逼出来的往返。 */}
        <button
          className="aw-folder-picker-here"
          disabled={busy || current === null}
          onClick={() => {
            if (current !== null) onChoose(current);
          }}
          ref={hereRef}
          type="button"
        >
          {busy ? "正在打开…" : "就用这一层"}
        </button>
      </header>

      <div className="aw-folder-picker-list">
        {listing.isPending ? <LoadingLine label="正在读取目录" /> : null}
        {listing.isError ? (
          <p className="aw-project-file-note">
            这个目录打不开：{String(listing.error)}
          </p>
        ) : null}

        {naming ? (
          // 长在列表顶上，而不是弹一个框：它建的东西就会出现在这份列表里，
          // 所以它该在列表里被输入。表单只有一个字段，回车即提交，Escape 收起。
          <form
            aria-label="新建文件夹"
            className="aw-folder-picker-new"
            onSubmit={submitName}
          >
            <FolderPlus aria-hidden="true" size={14} />
            <label className="aw-sr-only" htmlFor="aw-folder-picker-name">
              新文件夹的名字
            </label>
            <input
              aria-invalid={nameProblem !== null || undefined}
              autoFocus
              disabled={create.isPending}
              id="aw-folder-picker-name"
              onChange={(event) => {
                setDraftName(event.target.value);
                if (create.isError) create.reset();
              }}
              onKeyDown={(event) => {
                if (event.key !== "Escape") return;
                event.preventDefault();
                event.stopPropagation();
                closeNaming();
              }}
              placeholder="文件夹名字"
              spellCheck={false}
              value={draftName}
            />
            <button
              className="aw-folder-picker-create"
              disabled={
                create.isPending || draftName.trim() === "" || nameProblem !== null
              }
              type="submit"
            >
              {create.isPending ? "正在建…" : "建好并进入"}
            </button>
            <button
              aria-label="不建了"
              className="aw-folder-picker-new-close"
              disabled={create.isPending}
              onClick={closeNaming}
              title="不建了"
              type="button"
            >
              <X aria-hidden="true" size={13} />
            </button>
          </form>
        ) : null}
        {naming && nameProblem !== null ? (
          <p className="aw-folder-picker-problem" role="alert">
            {nameProblem}
          </p>
        ) : null}
        {naming && create.isError ? (
          <div className="aw-folder-picker-problem">
            <ErrorNotice message={createFailureText(create.error)} />
          </div>
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

        {shown.map((entry) => (
          // 行是一个 div 而不是一个按钮：里头装着两个按钮，而按钮不能套按钮。
          <div className="aw-folder-picker-row is-entry" key={entry.path}>
            <button
              className="aw-folder-picker-enter"
              disabled={busy}
              onClick={() => setPath(entry.path)}
              title={entry.path}
              type="button"
            >
              <Folder aria-hidden="true" size={14} />
              <span className="aw-folder-picker-name">{entry.name}</span>
              <ChevronRight
                aria-hidden="true"
                className="aw-folder-picker-into"
                size={13}
              />
            </button>
            {/* 那枚人字形（chevron）留在「走进去」这颗按钮里，而不是像稿子上
                那样单独站在行尾：它描述的是走进去这个动作，放在选中按钮的另一
                侧会把它读成「选中之后会发生什么」。 */}
            <button
              className="aw-folder-picker-select"
              disabled={busy}
              onClick={() => onChoose(entry.path)}
              type="button"
            >
              选这个
            </button>
          </div>
        ))}

        {listing.data !== undefined && entries.length === 0 ? (
          // 空目录是完全可以选的——新建一个空文件夹再开始编码是常见做法。
          // 所以这句说的是「这里面没有子目录」，不是「这里不能选」。
          <p className="aw-project-file-note">这个文件夹里没有子文件夹。</p>
        ) : null}

        {hiddenCount === 0 ? null : (
          // 数出来，和会话栏那行「还有 N 段 · 全部显示」同一个形状：一个不说
          // 数量的「显示隐藏」是在让读者猜自己有没有漏东西。
          <button
            aria-pressed={showHidden}
            className="aw-folder-picker-hidden"
            onClick={() => setShowHidden((held) => !held)}
            type="button"
          >
            {showHidden
              ? "收起隐藏的文件夹"
              : `还有 ${String(hiddenCount)} 个以 . 开头的隐藏文件夹 · 显示`}
          </button>
        )}

        {listing.data?.truncated === true ? (
          <p className="aw-project-file-note">
            子文件夹太多，只列出了前 {listing.data.entries.length} 个。
          </p>
        ) : null}
      </div>

      <footer>
        {/* 在页脚而不是标题栏：标题栏那颗是「选中」，这颗是「造一个」，两个
            动词不该挨着——一个人扫到路径旁边只该看见一种后果。 */}
        <button
          aria-expanded={naming}
          className="aw-folder-picker-add"
          disabled={busy || current === null || naming}
          onClick={() => {
            setNaming(true);
          }}
          type="button"
        >
          <FolderPlus aria-hidden="true" size={14} />
          新建文件夹
        </button>
        <span className="aw-folder-picker-hint">子文件夹只列名字，不读内容</span>
        {onCancel === undefined ? null : (
          <button disabled={busy} onClick={onCancel} type="button">
            取消
          </button>
        )}
      </footer>
    </section>
  );
}

/**
 * 建不成的时候说什么。
 *
 * 409 单独说：它不是「名字不对」，是「已经有了」，而已经有的那个就在列表里、
 * 能直接走进去。其余照服务端的话说——它给的那一句写明了是哪种拒绝（不是目录、
 * 写不进去、名字太长），压成「失败了」会把唯一有用的部分丢掉。
 */
function createFailureText(cause: unknown): string {
  if (cause instanceof ApiError && cause.status === 409) {
    return "这里已经有一个同名的文件夹了，直接从列表里走进去就行。";
  }
  return `没能建这个文件夹：${cause instanceof Error ? cause.message : String(cause)}`;
}
