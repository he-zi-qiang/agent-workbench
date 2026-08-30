import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  browseDirectories,
  createProjectAtDirectory,
  listProjects,
} from "../../api/client";
import { IdentityProvider } from "../../app/IdentityContext";
import { ProjectChooser } from "./ProjectChooser";

vi.mock("../../api/client", () => ({
  listProjects: vi.fn(),
  browseDirectories: vi.fn(),
  createProjectAtDirectory: vi.fn(),
}));

function project(overrides: Record<string, unknown> = {}) {
  return {
    project_id: "prj_1",
    name: "demo",
    created_at: "2026-08-22T00:00:00Z",
    updated_at: "2026-08-22T00:00:00Z",
    archived_at: null,
    root_path: "/Users/alice/demo",
    ...overrides,
  };
}

const onChoose = vi.fn();

function mount() {
  const queries = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queries}>
      <IdentityProvider>
        <ProjectChooser onChoose={onChoose} />
      </IdentityProvider>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(browseDirectories).mockResolvedValue({
    path: "/Users/alice",
    parent: "/Users",
    entries: [{ name: "demo", path: "/Users/alice/demo" }],
    truncated: false,
  });
});

describe("ProjectChooser", () => {
  it("lists only projects that actually hold a folder", async () => {
    vi.mocked(listProjects).mockResolvedValue({
      projects: [
        project(),
        // 没有目录的项目在 ADR-074 之后不该存在，但库里可能还留着早先建的。
        // 列出来会让人选中一个然后打不开，而那看起来像是坏了。
        project({ project_id: "prj_2", name: "旧项目", root_path: null }),
        // 归档的同理：它已经从视野里收起来了。
        project({
          project_id: "prj_3",
          name: "归档的",
          archived_at: "2026-08-21T00:00:00Z",
        }),
      ],
    });

    mount();

    expect(await screen.findByText("demo")).toBeInTheDocument();
    expect(screen.queryByText("旧项目")).not.toBeInTheDocument();
    expect(screen.queryByText("归档的")).not.toBeInTheDocument();
  });

  it("shows the folder beside the name, because two projects may share a name", async () => {
    vi.mocked(listProjects).mockResolvedValue({ projects: [project()] });

    mount();

    // 名字默认取自文件夹名，所以两个不同的文件夹完全可能同名。路径是唯一能把
    // 它们分开的东西。
    expect(await screen.findByText("/Users/alice/demo")).toBeInTheDocument();
  });

  it("hands back the project that was picked", async () => {
    const user = userEvent.setup();
    vi.mocked(listProjects).mockResolvedValue({ projects: [project()] });

    mount();
    await user.click(await screen.findByRole("button", { name: /demo/ }));

    expect(onChoose).toHaveBeenCalledWith(project());
  });

  it("creates a project named after the folder that was chosen", async () => {
    const user = userEvent.setup();
    vi.mocked(listProjects).mockResolvedValue({ projects: [] });
    vi.mocked(createProjectAtDirectory).mockResolvedValue(project());

    mount();
    await user.click(await screen.findByRole("button", { name: /选择一个文件夹/ }));
    await user.click(await screen.findByRole("button", { name: "就用这一层" }));

    await waitFor(() => {
      // 文件夹就是项目——再问一次名字是在问一个屏幕上已有答案的问题。
      expect(vi.mocked(createProjectAtDirectory).mock.calls[0]?.slice(1)).toEqual(
        ["alice", "/Users/alice"],
      );
    });
    expect(onChoose).toHaveBeenCalledWith(project());
  });

  it("takes the folder from the row that was clicked, not the level it is in", async () => {
    const user = userEvent.setup();
    vi.mocked(listProjects).mockResolvedValue({ projects: [] });
    vi.mocked(createProjectAtDirectory).mockResolvedValue(project());

    mount();
    await user.click(await screen.findByRole("button", { name: /选择一个文件夹/ }));
    await user.click(await screen.findByRole("button", { name: "选这个" }));

    await waitFor(() => {
      // `/Users/alice/demo`，不是 `/Users/alice`。行尾那枚按钮和标题栏那枚各
      // 自选中不同的目录，这一条守的就是它们没有接错线——接错了的样子是「点了
      // 列表里的某一行，开出来的却是它的父目录」，一个会一路走到磁盘上的错。
      expect(vi.mocked(createProjectAtDirectory).mock.calls[0]?.slice(1)).toEqual(
        ["demo", "/Users/alice/demo"],
      );
    });
  });

  it("still lets a row be walked into rather than chosen", async () => {
    const user = userEvent.setup();
    vi.mocked(listProjects).mockResolvedValue({ projects: [] });

    mount();
    await user.click(await screen.findByRole("button", { name: /选择一个文件夹/ }));
    await user.click(await screen.findByRole("button", { name: /^demo/ }));

    // 点名字是导航，不是选中：这一条守的是新加的那枚按钮没有把整行都变成
    // 「选中」——那样的话就再也走不进任何一个子目录了。
    await waitFor(() => {
      expect(vi.mocked(browseDirectories).mock.calls.at(-1)?.[1]).toMatchObject({
        path: "/Users/alice/demo",
      });
    });
    expect(vi.mocked(createProjectAtDirectory)).not.toHaveBeenCalled();
  });

  it("offers no way past the picker when there is nothing else to choose", async () => {
    const user = userEvent.setup();
    vi.mocked(listProjects).mockResolvedValue({ projects: [] });

    mount();
    await user.click(await screen.findByRole("button", { name: /选择一个文件夹/ }));

    // 一个项目都没有的时候「取消」会把人送回一个同样只能选文件夹的屏幕 —— 一个
    // 什么也不改变的按钮。这条同时守着 ADR-074 §7.1：没有文件夹就开不了会话，
    // 所以这里不能有一条绕过去的路。
    expect(screen.queryByRole("button", { name: "取消" })).not.toBeInTheDocument();
  });

  it("can be backed out of when there is an existing project to fall back to", async () => {
    const user = userEvent.setup();
    vi.mocked(listProjects).mockResolvedValue({ projects: [project()] });

    mount();
    await user.click(
      await screen.findByRole("button", { name: /选择另一个文件夹/ }),
    );
    await user.click(await screen.findByRole("button", { name: "取消" }));

    expect(await screen.findByText("demo")).toBeInTheDocument();
  });

  it("reports a refused folder instead of pretending it opened", async () => {
    const user = userEvent.setup();
    vi.mocked(listProjects).mockResolvedValue({ projects: [] });
    vi.mocked(createProjectAtDirectory).mockRejectedValue(
      new Error("a project root must be an existing directory"),
    );

    mount();
    await user.click(await screen.findByRole("button", { name: /选择一个文件夹/ }));
    await user.click(await screen.findByRole("button", { name: "就用这一层" }));

    await waitFor(() => {
      // 服务端自己的理由要到达屏幕：只说「失败了」的话，人会再点一次同一个
      // 文件夹。
      expect(screen.getByText(/an existing directory/)).toBeInTheDocument();
    });
    expect(onChoose).not.toHaveBeenCalled();
  });
});
