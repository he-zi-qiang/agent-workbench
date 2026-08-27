import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type * as ApiClient from "../../api/client";
import { listProjectFiles, readProjectFile } from "../../api/client";
import { IdentityProvider } from "../../app/IdentityContext";
import { ProjectFileBody, ProjectFileTree } from "./ProjectFileTree";

// `importOriginal`，不是一个字面量替身：这个模块除了两个请求函数，还导出
// `MAX_PREVIEW_BYTES`——预览用它在取正文之前拒绝一个太大的文件。整块替掉会让
// 那个常量变成 `undefined`，而 `undefined > x` 是 false，于是测试里那道闸门
// 永远不触发，而生产里它是触发的。替身只替该替的那两个。
vi.mock("../../api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof ApiClient>();
  return { ...actual, listProjectFiles: vi.fn(), readProjectFile: vi.fn() };
});

const ROOT = "/Users/someone/agent工作台";

function entry(path: string, kind: "file" | "directory", size: number | null) {
  return {
    path,
    kind,
    size_bytes: size,
    modified_at: "2026-08-22T00:00:00Z",
  };
}

function mount(node: React.ReactNode) {
  const queries = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queries}>
      <IdentityProvider>{node}</IdentityProvider>
    </QueryClientProvider>,
  );
}

function tree(
  selectedPath: string | null = null,
  writtenPaths: readonly string[] = [],
) {
  return mount(
    <ProjectFileTree
      onOpenFile={onOpenFile}
      projectId="prj_1"
      rootPath={ROOT}
      selectedPath={selectedPath}
      writtenPaths={writtenPaths}
    />,
  );
}

const onOpenFile = vi.fn();

beforeEach(() => {
  vi.clearAllMocks();
});

describe("ProjectFileTree", () => {
  it("shows the root path, because it is the only thing that answers where the agent writes", async () => {
    vi.mocked(listProjectFiles).mockResolvedValue({
      path: "",
      entries: [entry("README.md", "file", 10)],
      truncated: false,
    });

    tree();

    // The absolute path is the one piece of server-side detail this UI shows on
    // purpose: a person about to let an agent write files needs to know into
    // which directory, and nothing else on the page says.
    expect(await screen.findByText(ROOT)).toBeInTheDocument();
  });

  it("fetches one level at a time rather than the whole tree", async () => {
    const user = userEvent.setup();
    vi.mocked(listProjectFiles).mockImplementation((_identity, _projectId, options) =>
      Promise.resolve(
        options?.path === "src"
          ? {
              path: "src",
              entries: [entry("src/main.py", "file", 12)],
              truncated: false,
            }
          : {
              path: "",
              entries: [entry("src", "directory", null)],
              truncated: false,
            },
      ),
    );

    tree();
    // Nothing under `src` before it is expanded: a component that pre-fetched
    // would hit the listing ceiling on any real repository and then be able to
    // show only "truncated".
    expect(await screen.findByRole("button", { name: /src/ })).toBeInTheDocument();
    expect(screen.queryByText("main.py")).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /src/ }));
    expect(await screen.findByText("main.py")).toBeInTheDocument();
    expect(vi.mocked(listProjectFiles).mock.calls.map(([, , o]) => o?.path)).toEqual(
      ["", "src"],
    );
  });

  it("shows only the last segment, because the indent already says the depth", async () => {
    vi.mocked(listProjectFiles).mockResolvedValue({
      path: "",
      entries: [entry("docs/adr/0072.md", "file", 10)],
      truncated: false,
    });

    tree();

    expect(await screen.findByText("0072.md")).toBeInTheDocument();
    expect(screen.queryByText("docs/adr/0072.md")).not.toBeInTheDocument();
  });

  it("says when a level was truncated", async () => {
    vi.mocked(listProjectFiles).mockResolvedValue({
      path: "",
      entries: [entry("a.txt", "file", 1)],
      truncated: true,
    });

    tree();

    // A truncated tree drawn as a whole one reads to a person as *this project
    // has one file*, which is worse than showing nothing.
    expect(await screen.findByText(/文件太多/)).toBeInTheDocument();
  });

  it("opens a file when one is clicked, and toggles a directory instead", async () => {
    const user = userEvent.setup();
    // Per-path, not one canned answer for every path. A mock that returned the
    // root listing for `src` too would hand the component an entry whose path
    // equals its own parent, and the recursion would not terminate -- which is
    // exactly what happened the first time this was written. The real server
    // cannot produce that (a child's path always extends its parent's), so the
    // mock must not either.
    vi.mocked(listProjectFiles).mockImplementation((_identity, _projectId, options) =>
      Promise.resolve(
        options?.path === "src"
          ? { path: "src", entries: [], truncated: false }
          : {
              path: "",
              entries: [
                entry("src", "directory", null),
                entry("a.txt", "file", 1),
              ],
              truncated: false,
            },
      ),
    );

    tree();

    await user.click(await screen.findByRole("button", { name: /a\.txt/ }));
    // 整行，不只是路径：预览要在取正文之前拿到字节数才能拒绝一个太大的文件。
    expect(onOpenFile).toHaveBeenCalledWith(
      expect.objectContaining({ path: "a.txt", size_bytes: 1 }),
    );

    onOpenFile.mockClear();
    await user.click(screen.getByRole("button", { name: /src/ }));
    // A directory is not a file: clicking it must not ask the page to open one.
    expect(onOpenFile).not.toHaveBeenCalled();
  });

  it("marks the rows this session wrote, and the folders above them", async () => {
    // 按层取的树，写在深处的文件在它被展开之前根本不存在于 DOM 里——所以标记如果
    // 只标文件本身，一次写进 `src/main.py` 的产出在收起状态下完全没有痕迹，而那
    // 正是「产物没在文件夹里体现」说的那件事。
    vi.mocked(listProjectFiles).mockImplementation((_i, _p, options) =>
      Promise.resolve(
        options?.path === "src"
          ? {
              path: "src",
              entries: [entry("src/main.py", "file", 20)],
              truncated: false,
            }
          : {
              path: "",
              entries: [
                entry("src", "directory", null),
                entry("README.md", "file", 10),
              ],
              truncated: false,
            },
      ),
    );

    tree(null, ["src/main.py"]);

    // 祖先目录被自动展开，读者不必先猜它在哪一层。
    const written = await screen.findByRole("button", { name: /main\.py/ });
    expect(within(written).getByText("这段会话写过它")).toBeInTheDocument();

    const folder = screen.getByRole("button", { name: /src/ });
    expect(within(folder).getByText("这段会话写过它")).toBeInTheDocument();

    // 没被写过的行不带标记。少标一个是漏说，标错一个是撒谎，而这一条钉的是后者。
    const untouched = screen.getByRole("button", { name: /README\.md/ });
    expect(within(untouched).queryByText("这段会话写过它")).toBeNull();
  });

  it("does not re-open a folder the reader collapsed after it was revealed", async () => {
    const user = userEvent.setup();
    vi.mocked(listProjectFiles).mockImplementation((_i, _p, options) =>
      Promise.resolve(
        options?.path === "src"
          ? {
              path: "src",
              entries: [entry("src/main.py", "file", 20)],
              truncated: false,
            }
          : {
              path: "",
              entries: [entry("src", "directory", null)],
              truncated: false,
            },
      ),
    );

    // 同一个 QueryClient 跨两次渲染，否则 `rerender` 会把整棵子树重挂——那样
    // `revealed` 也一起重置，这条用例就变成了在考验 React 而不是考验这个守卫。
    const queries = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    const subject = (written: readonly string[]) => (
      <QueryClientProvider client={queries}>
        <IdentityProvider>
          <ProjectFileTree
            onOpenFile={onOpenFile}
            projectId="prj_1"
            rootPath={ROOT}
            selectedPath={null}
            writtenPaths={written}
          />
        </IdentityProvider>
      </QueryClientProvider>
    );

    const view = render(subject(["src/main.py"]));
    expect(await screen.findByText("main.py")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /src/ }));
    expect(screen.queryByText("main.py")).not.toBeInTheDocument();

    // 同一份 `writtenPaths` 再渲染一次。每条路径只带读者去一次——少了这一条，
    // 读者收起的目录会在下一帧自己弹回来，而一个会弹回来的折叠比不能折叠更糟。
    view.rerender(subject(["src/main.py"]));
    expect(screen.queryByText("main.py")).not.toBeInTheDocument();
  });
});

describe("ProjectFileBody", () => {
  it("shows text", async () => {
    vi.mocked(readProjectFile).mockResolvedValue({
      path: "a.txt",
      text: "hello\n",
      size_bytes: 6,
      is_text: true,
      modified_at: "2026-08-22T00:00:00Z",
    });

    mount(
      <ProjectFileBody path="a.txt" projectId="prj_1" sizeBytes={6} />,
    );

    expect(await screen.findByText("hello")).toBeInTheDocument();
  });

  it("describes a binary file rather than drawing it", async () => {
    vi.mocked(readProjectFile).mockResolvedValue({
      path: "logo.png",
      text: null,
      size_bytes: 2048,
      is_text: false,
      modified_at: "2026-08-22T00:00:00Z",
    });

    mount(
      <ProjectFileBody path="logo.png" projectId="prj_1" sizeBytes={2048} />,
    );

    // Decoded with replacement it would be a screenful of U+FFFD, which reads
    // as a corrupt file. What is wrong there is the rendering, not the file.
    expect(await screen.findByText(/二进制文件/)).toBeInTheDocument();
  });

  it("reports a refusal instead of showing an empty file", async () => {
    vi.mocked(readProjectFile).mockRejectedValue(
      new Error("path escapes the project root"),
    );

    mount(
      <ProjectFileBody path="../etc/passwd" projectId="prj_1" sizeBytes={12} />,
    );

    const view = await screen.findByRole("region", { name: /\.\.\/etc\/passwd/ });
    // `waitFor` on `textContent`, not `getByText`, and both halves matter.
    // The region renders immediately whatever the query is doing, so a bare
    // assertion runs against the loading state; and the message is two text
    // nodes (the prose and the interpolated error), which a text query cannot
    // span. The sandbox's own reason has to reach the screen -- a person told
    // only "failed" retries the same path.
    await waitFor(() => {
      expect(view.textContent).toContain("读不到这个文件");
      expect(view.textContent).toContain("path escapes the project root");
    });
  });
});
