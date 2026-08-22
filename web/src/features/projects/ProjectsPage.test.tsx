import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  cleanup,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  createProject,
  deleteProject,
  getProjectItems,
  listProjects,
  renameProject,
  setProjectArchived,
} from "../../api/client";
import type { PrincipalIdentity } from "../../api/types";
import { useIdentity } from "../../app/IdentityContext";
import { ProjectsPage } from "./ProjectsPage";

vi.mock("../../api/client", () => ({
  createProject: vi.fn(),
  deleteProject: vi.fn(),
  getProjectItems: vi.fn(),
  listProjects: vi.fn(),
  renameProject: vi.fn(),
  setProjectArchived: vi.fn(),
}));

vi.mock("../../app/IdentityContext", () => ({
  useIdentity: vi.fn(),
}));

const ALICE: PrincipalIdentity = {
  tenantId: "tenant_a",
  principalId: "alice",
  scopes: [],
};

function project(
  projectId: string,
  name: string,
  archivedAt: string | null = null,
) {
  return {
    project_id: projectId,
    name,
    created_at: "2026-08-20T00:00:00Z",
    updated_at: "2026-08-20T00:00:00Z",
    archived_at: archivedAt,
    // ADR-072. `null` 而不是省略：这个字段在 ProjectView 上是必填的，而
    // 「没登记目录」和「这个后端不支持目录」必须能被区分开。
    root_path: null,
  };
}

function renderProjects(entry = "/projects") {
  const queries = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queries}>
      <MemoryRouter initialEntries={[entry]}>
        <Routes>
          <Route element={<ProjectsPage />} path="/projects" />
          <Route element={<ProjectsPage />} path="/projects/:projectId" />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(useIdentity).mockReturnValue({
    identity: ALICE,
    updateIdentity: vi.fn(),
    editorOpen: false,
    setEditorOpen: vi.fn(),
  });
  vi.mocked(listProjects).mockResolvedValue({ projects: [] });
  vi.mocked(getProjectItems).mockResolvedValue({
    project_id: "prj_1",
    items: [],
  });
});

afterEach(() => cleanup());

describe("ProjectsPage", () => {
  it("does not push anybody into creating a project", async () => {
    renderProjects();

    // 归属是可空的，空是正常状态。空状态说的是「可以这么用」，不是「你还缺一个
    // 项目」——后者会把一个可选的组织方式说成必须先过的一道门。
    expect(
      await screen.findByText(/把同一件事的对话、任务、编码会话和资料放到一起/),
    ).toBeInTheDocument();
    expect(screen.queryByText(/还没有项目/)).not.toBeInTheDocument();
  });

  it("says out loud that deleting a project keeps what is in it", async () => {
    const user = userEvent.setup();
    vi.mocked(listProjects).mockResolvedValue({
      projects: [project("prj_1", "季度复盘")],
    });
    vi.mocked(deleteProject).mockResolvedValue(undefined);
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);

    renderProjects();
    await user.click(
      await screen.findByRole("button", { name: "删除项目 季度复盘" }),
    );

    // 「删除项目」这四个字本身会让人以为里面的东西也没了，而 ON DELETE SET NULL
    // 说的恰恰相反。确认框必须把这件事说出来，否则它在替读者做一个他没做的判断。
    expect(confirm.mock.calls[0]?.[0]).toContain("都会留下");
    expect(deleteProject).not.toHaveBeenCalled();

    confirm.mockReturnValue(true);
    await user.click(screen.getByRole("button", { name: "删除项目 季度复盘" }));
    await waitFor(() => {
      expect(vi.mocked(deleteProject).mock.calls[0]?.[1]).toBe("prj_1");
    });
  });

  it("sends every item to the page that owns it", async () => {
    vi.mocked(listProjects).mockResolvedValue({
      projects: [project("prj_1", "季度复盘")],
    });
    vi.mocked(getProjectItems).mockResolvedValue({
      project_id: "prj_1",
      items: [
        {
          kind: "chat",
          item_id: "ses_1",
          title: "问过的问题",
          ordered_at: "2026-08-20T10:00:00Z",
        },
        {
          kind: "task",
          item_id: "task_1",
          title: "导出一份报告",
          ordered_at: "2026-08-20T09:00:00Z",
        },
        {
          kind: "knowledge_base",
          item_id: "kb_1",
          title: "产品手册",
          ordered_at: "2026-08-20T08:00:00Z",
        },
      ],
    });

    renderProjects("/projects/prj_1");

    // 项目不替任何产品页回答问题：每一行都跳回它自己的地方。
    expect(
      await screen.findByRole("link", { name: /问过的问题/ }),
    ).toHaveAttribute("href", "/chat/ses_1");
    expect(screen.getByRole("link", { name: /导出一份报告/ })).toHaveAttribute(
      "href",
      "/work/task_1",
    );
    expect(screen.getByRole("link", { name: /产品手册/ })).toHaveAttribute(
      "href",
      "/knowledge/kb_1",
    );
  });

  it("shows a session that was never spoken in without inventing a name", async () => {
    vi.mocked(listProjects).mockResolvedValue({
      projects: [project("prj_1", "季度复盘")],
    });
    vi.mocked(getProjectItems).mockResolvedValue({
      project_id: "prj_1",
      items: [
        {
          kind: "code",
          item_id: "ses_code_1",
          title: null,
          ordered_at: "2026-08-20T10:00:00Z",
        },
      ],
    });

    renderProjects("/projects/prj_1");

    // 会话由第一句指令命名；没说过话的会话就是没有名字。拿 id 编一个出来会让
    // 读者以为那串字符是他起的。
    expect(await screen.findByText("还没有名字")).toBeInTheDocument();
    expect(screen.queryByText(/ses_code_1/)).not.toBeInTheDocument();
  });

  it("renames a project in the row it is already in", async () => {
    const user = userEvent.setup();
    vi.mocked(listProjects).mockResolvedValue({
      projects: [project("prj_1", "旧名字")],
    });
    vi.mocked(renameProject).mockResolvedValue(project("prj_1", "新名字"));

    renderProjects();
    await user.click(
      await screen.findByRole("button", { name: "重命名项目 旧名字" }),
    );
    const field = screen.getByLabelText("项目名字");
    await user.clear(field);
    await user.type(field, "新名字{Enter}");

    await waitFor(() => {
      expect(vi.mocked(renameProject).mock.calls[0]?.slice(1)).toEqual([
        "prj_1",
        "新名字",
      ]);
    });
  });

  it("archives without deleting, and offers the way back", async () => {
    const user = userEvent.setup();
    vi.mocked(listProjects).mockResolvedValue({
      projects: [project("prj_1", "季度复盘", "2026-08-20T12:00:00Z")],
    });
    vi.mocked(setProjectArchived).mockResolvedValue(
      project("prj_1", "季度复盘"),
    );

    renderProjects();

    // 归档是可逆的，所以这一行给的是「取消归档」，不是第二个删除。
    const restore = await screen.findByRole("button", {
      name: "取消归档 季度复盘",
    });
    await user.click(restore);
    await waitFor(() => {
      expect(vi.mocked(setProjectArchived).mock.calls[0]?.slice(1)).toEqual([
        "prj_1",
        false,
      ]);
    });
    expect(deleteProject).not.toHaveBeenCalled();
  });

  it("creates a project and opens it", async () => {
    const user = userEvent.setup();
    vi.mocked(createProject).mockResolvedValue(
      project("prj_new", "新的一件事"),
    );

    renderProjects();
    await user.click(await screen.findByRole("button", { name: "新建项目" }));
    const field = screen.getByLabelText("项目名字");
    await user.type(field, "新的一件事{Enter}");

    await waitFor(() => {
      expect(vi.mocked(createProject).mock.calls[0]?.[1]).toBe("新的一件事");
    });
    const sidebar = screen.getByRole("complementary", { name: "项目列表" });
    expect(
      within(sidebar).queryByLabelText("项目名字"),
    ).not.toBeInTheDocument();
  });
});
