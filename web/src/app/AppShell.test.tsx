import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import userEvent from "@testing-library/user-event";
import {
  Link,
  MemoryRouter,
  Route,
  Routes,
  useLocation,
  useNavigate,
} from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { AppShell } from "./AppShell";
import { IdentityProvider } from "./IdentityContext";
import { ThemeProvider } from "./ThemeContext";
import {
  useWorkspaceSidebar,
  WorkspaceSidebarPortal,
} from "./WorkspaceSidebar";

// 设置面板默认落在「模型密钥」（2026-09-04 评审），那一类用 react-query 读
// key 的状态，所以每一处渲染都得有 QueryClient——它此前落在「本地身份」时不用。
const queryClient = new QueryClient({
  defaultOptions: { queries: { retry: false } },
});

vi.mock("../api/client", () => ({
  getProviderKey: vi.fn().mockResolvedValue({
    active: false,
    stored: false,
    fingerprint: null,
    path: "~/.config/agent-workbench/key",
    restart_required: false,
    restart_hint: "",
  }),
  storeProviderKey: vi.fn(),
  clearProviderKey: vi.fn(),
  // 侧栏的「待处理」每 15 秒问一次；默认没有人在等。
  listApprovals: vi.fn().mockResolvedValue({ approvals: [], cursor: null }),
}));

beforeEach(() => {
  queryClient.clear();
  localStorage.removeItem("aw.identity.v5");
  localStorage.removeItem("agent-workbench:workspace-sidebar-collapsed-v2");
});

function ChatContextProbe() {
  const sidebar = useWorkspaceSidebar();
  return (
    <>
      <button onClick={sidebar.open} type="button">
        打开对话列表
      </button>
      <WorkspaceSidebarPortal>
        <aside aria-label="最近对话">
          <button
            className="aw-chat-sessions-close"
            onClick={sidebar.close}
            type="button"
          >
            关闭对话列表
          </button>
          <a href="#session-a">会话 A</a>
        </aside>
      </WorkspaceSidebarPortal>
      <p>Chat page</p>
    </>
  );
}

function PathProbe({ chatLink = false }: { chatLink?: boolean }) {
  const location = useLocation();
  const navigate = useNavigate();
  return (
    <>
      <output aria-label="当前路径">{location.pathname}</output>
      {chatLink ? <Link to="/chat/session-b">打开 B 会话</Link> : null}
      <button onClick={() => void navigate(-1)} type="button">
        浏览器后退
      </button>
    </>
  );
}

function SystemProbe() {
  return (
    <>
      <p>System page</p>
      <PathProbe />
    </>
  );
}

describe("AppShell mobile navigation", () => {
  it("makes every auxiliary project page reachable from More", async () => {
    const user = userEvent.setup();
    render(
      <QueryClientProvider client={queryClient}>
      <ThemeProvider>
        <IdentityProvider>
          <MemoryRouter initialEntries={["/chat"]}>
            <Routes>
              <Route element={<AppShell />}>
                <Route element={<p>Chat page</p>} path="/chat" />
                <Route element={<p>Evaluation page</p>} path="/evaluation" />
              </Route>
            </Routes>
          </MemoryRouter>
        </IdentityProvider>
      </ThemeProvider>
      </QueryClientProvider>,
    );

    await user.click(
      within(screen.getByRole("navigation", { name: "移动端导航" })).getByRole(
        "button",
        { name: "更多" },
      ),
    );
    const dialog = screen.getByRole("dialog", { name: "更多页面" });
    const more = within(dialog);
    expect(dialog).toBeInTheDocument();
    expect(more.getByRole("link", { name: "效果评测" })).toBeInTheDocument();
    // Gone with ADR-048. Asserted against the whole document rather than the
    // sheet: an entry added back to `NAVIGATION` but missing from the More
    // filter renders in the desktop rail and nowhere else, and a sheet-only
    // assertion would call that deleted.
    expect(
      screen.queryByRole("link", { name: "待我确认" }),
    ).not.toBeInTheDocument();
    expect(more.getByRole("link", { name: "运行状态" })).toBeInTheDocument();
    // The page this filter was derived for. It used to name /evaluation and
    // /system literally, so 计算机 reached the desktop rail and no mobile
    // surface at all -- the exact shape the comment above describes.
    expect(more.getByRole("link", { name: "计算机" })).toBeInTheDocument();
    expect(
      more.getByRole("button", { name: "设置" }),
    ).toBeInTheDocument();

    await user.click(more.getByRole("link", { name: "效果评测" }));
    expect(screen.getByText("Evaluation page")).toBeInTheDocument();
    expect(
      screen.queryByRole("dialog", { name: "更多页面" }),
    ).not.toBeInTheDocument();
  });

  it("keeps focus inside More and restores it on close", async () => {
    const user = userEvent.setup();
    render(
      <QueryClientProvider client={queryClient}>
      <ThemeProvider>
        <IdentityProvider>
          <MemoryRouter initialEntries={["/chat"]}>
            <Routes>
              <Route element={<AppShell />}>
                <Route element={<p>Chat page</p>} path="/chat" />
              </Route>
            </Routes>
          </MemoryRouter>
        </IdentityProvider>
      </ThemeProvider>
      </QueryClientProvider>,
    );

    const railElement = screen.getByRole("navigation", { name: "主导航" });
    const rail = within(railElement);
    const trigger = rail.getByRole("button", { name: "更多" });
    await user.click(trigger);
    const dialog = screen.getByRole("dialog", { name: "更多页面" });
    const more = within(dialog);
    expect(more.getByRole("link", { name: "知识库" })).toHaveFocus();

    more.getByRole("button", { name: "设置" }).focus();
    await user.tab();
    expect(more.getByRole("button", { name: "关闭更多页面" })).toHaveFocus();

    await user.keyboard("{Escape}");
    await waitFor(() => expect(trigger).toHaveFocus());

    await user.click(trigger);
    await user.click(
      within(screen.getByRole("dialog", { name: "更多页面" })).getByRole(
        "button",
        { name: "设置" },
      ),
    );
    const settings = within(screen.getByRole("dialog", { name: "设置" }));
    // 焦点落在打开时那一类的第一个输入框上——现在是「模型密钥」的 key 框，
    // 不再是身份那一类的 Tenant。
    await waitFor(() =>
      expect(settings.getByLabelText("新的 API key")).toHaveFocus(),
    );
    // 关闭走那颗 X（和 Escape），不走一颗页脚的「取消」。分类式的设置面板里，
    // 页脚那个位置看起来在为**整个面板**负责，而它只能取消当前这一类里的草稿
    // ——另外三类根本没有待保存的东西。
    await user.click(settings.getByRole("button", { name: "关闭" }));
    await waitFor(() => expect(trigger).toHaveFocus());
  });
});

describe("AppShell rail", () => {
  function mounted(at: string) {
    return render(
      <QueryClientProvider client={queryClient}>
      <ThemeProvider>
        <IdentityProvider>
          <MemoryRouter initialEntries={[at]}>
            <Routes>
              <Route element={<AppShell />}>
                <Route
                  element={
                    <>
                      <p>Chat page</p>
                      <PathProbe chatLink />
                    </>
                  }
                  path="/chat/:sessionId?"
                />
                <Route
                  element={
                    <>
                      <p>Work page</p>
                      <PathProbe />
                      <Link to="/work/task-a">打开 A 任务</Link>
                    </>
                  }
                  path="/work/:taskId?"
                />
                <Route
                  element={
                    <>
                      <p>Code page</p>
                      <PathProbe />
                    </>
                  }
                  path="/code/:sessionId?"
                />
                <Route element={<SystemProbe />} path="/system" />
                <Route
                  element={
                    <>
                      <p>Fallback page</p>
                      <PathProbe />
                    </>
                  }
                  path="*"
                />
              </Route>
            </Routes>
          </MemoryRouter>
        </IdentityProvider>
      </ThemeProvider>
      </QueryClientProvider>,
    );
  }

  it("marks 任务 as its own current primary flow", () => {
    mounted("/work");

    const rail = within(screen.getByRole("navigation", { name: "主导航" }));
    // 当前那一项仍然是链接，只多一个 aria-current。上一版把它渲染成一个
    // disclosure 按钮（「这一组展开不展开」），同一行两种点击含义——2026-09-04
    // 的评审把它列为第一条要改的。折叠现在是列表区自己头上那颗箭头。
    const here = rail.getByRole("link", { name: "任务" });
    expect(here).toHaveAttribute("aria-current", "page");
    expect(rail.getByRole("link", { name: "对话" })).not.toHaveAttribute(
      "aria-current",
    );
    expect(rail.getByRole("link", { name: "编码" })).not.toHaveAttribute(
      "aria-current",
    );
    const fold = rail.getByRole("button", { name: "收起最近任务" });
    expect(fold).toHaveAttribute("aria-expanded", "true");
    expect(fold).toHaveAttribute("aria-controls", "workspace-sidebar-context");
  });

  it("主导航在八个页面上是同一组链接，顺序和位置不随内容变", () => {
    const { unmount } = mounted("/chat");
    const order = () =>
      within(screen.getByRole("navigation", { name: "主导航" }))
        .getAllByRole("link")
        .map((link) => link.getAttribute("aria-label"))
        .filter((name) => name !== "Agent Workbench");
    const onChat = order();
    expect(onChat).toEqual(["对话", "任务", "编码", "知识库"]);
    // Chat 的会话列表长在这四个链接**后面**，不夹在它们中间。
    const rail = screen.getByRole("navigation", { name: "主导航" });
    const slot = rail.querySelector("#workspace-sidebar-context");
    expect(slot).not.toBeNull();
    const lastLink = within(rail).getByRole("link", { name: "知识库" });
    expect(
      lastLink.compareDocumentPosition(slot as Node) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).not.toBe(0);
    unmount();

    // 辅助页没有列表可挂，四个链接照旧，只是不再有列表区——也不再收窄。
    mounted("/system");
    expect(order()).toEqual(onChat);
    expect(
      document.querySelector("#workspace-sidebar-context"),
    ).toBeNull();
    expect(screen.queryByRole("button", { name: /最近/ })).toBeNull();
  });

  it("将低频工具收进更多，保持主导航安静", async () => {
    const user = userEvent.setup();
    mounted("/chat");

    const rail = within(screen.getByRole("navigation", { name: "主导航" }));
    expect(rail.getByRole("link", { name: "知识库" })).toBeInTheDocument();
    expect(
      rail.queryByRole("link", { name: "效果评测" }),
    ).not.toBeInTheDocument();
    expect(
      rail.queryByRole("link", { name: "运行状态" }),
    ).not.toBeInTheDocument();

    await user.click(rail.getByRole("button", { name: "更多" }));
    const more = within(screen.getByRole("dialog", { name: "更多页面" }));
    expect(more.getByRole("link", { name: "效果评测" })).toBeInTheDocument();
    expect(more.getByRole("link", { name: "运行状态" })).toBeInTheDocument();
  });

  it("offers 对话、任务、编码 as three top-level flows", () => {
    mounted("/chat");

    const rail = within(screen.getByRole("navigation", { name: "主导航" }));
    // 三项都是链接，挂载在 /chat 所以 Chat 带 aria-current。
    expect(rail.getByRole("link", { name: "对话" })).toHaveAttribute(
      "aria-current",
      "page",
    );
    expect(rail.getByRole("link", { name: "任务" })).toBeInTheDocument();
    expect(rail.getByRole("link", { name: "编码" })).toBeInTheDocument();
    // 无障碍名只有中文。英文名画在旁边，但不进名字——一栏两套名字，读屏念
    // 一套、搜索命中另一套。
    expect(rail.queryByRole("link", { name: "Chat" })).not.toBeInTheDocument();
    expect(rail.queryByRole("link", { name: "Code" })).not.toBeInTheDocument();
    expect(rail.getByRole("link", { name: "对话" })).toHaveTextContent("Chat");
  });

  it("nests the active feature's real work items in the one shell sidebar", async () => {
    const user = userEvent.setup();
    render(
      <QueryClientProvider client={queryClient}>
      <ThemeProvider>
        <IdentityProvider>
          <MemoryRouter initialEntries={["/chat"]}>
            <Routes>
              <Route element={<AppShell />}>
                <Route element={<ChatContextProbe />} path="/chat" />
              </Route>
            </Routes>
          </MemoryRouter>
        </IdentityProvider>
      </ThemeProvider>
      </QueryClientProvider>,
    );

    const railElement = screen.getByRole("navigation", { name: "主导航" });
    const rail = within(railElement);
    expect(
      rail.getByRole("complementary", { name: "最近对话" }),
    ).toContainElement(rail.getByRole("link", { name: "会话 A" }));

    const trigger = screen.getByRole("button", { name: "打开对话列表" });
    await user.click(trigger);
    expect(screen.getByRole("dialog", { name: "主导航" })).toBe(railElement);
    expect(
      screen.getByText("Chat page").closest(".aw-app-content"),
    ).toHaveAttribute("inert");
    expect(document.querySelector(".aw-mobile-nav")).toHaveAttribute("inert");
    await waitFor(() =>
      expect(rail.getByRole("button", { name: "关闭对话列表" })).toHaveFocus(),
    );
    await user.keyboard("{Escape}");
    await waitFor(() => expect(trigger).toHaveFocus());
    expect(screen.getByRole("navigation", { name: "主导航" })).toBe(
      railElement,
    );
    expect(
      screen.getByText("Chat page").closest(".aw-app-content"),
    ).not.toHaveAttribute("inert");
  });

  it("keeps a collapsed workspace context mounted for the mobile drawer", async () => {
    const user = userEvent.setup();
    render(
      <QueryClientProvider client={queryClient}>
      <ThemeProvider>
        <IdentityProvider>
          <MemoryRouter initialEntries={["/chat"]}>
            <Routes>
              <Route element={<AppShell />}>
                <Route element={<ChatContextProbe />} path="/chat" />
              </Route>
            </Routes>
          </MemoryRouter>
        </IdentityProvider>
      </ThemeProvider>
      </QueryClientProvider>,
    );

    // 折叠是列表区头上那颗箭头，不是导航项。收起之后名字跟着换，读屏念到的
    // 就是下一次点击会发生的事。
    const fold = screen.getByRole("button", { name: "收起最近对话" });
    await user.click(fold);
    expect(fold).toHaveAttribute("aria-expanded", "false");
    expect(fold).toHaveAccessibleName("展开最近对话");
    // 限定在主导航里：底部那条移动端栏也有一个叫 Chat 的链接。
    expect(
      within(screen.getByRole("navigation", { name: "主导航" })).getByRole(
        "link",
        { name: "对话" },
      ),
    ).toHaveAttribute("aria-current", "page");

    const host = document.getElementById("workspace-sidebar-context");
    expect(host).not.toBeNull();
    expect(host?.querySelector('[aria-label="最近对话"]')).not.toBeNull();
  });

  it.each([
    ["/chat/session-42", "任务", "对话"],
    ["/work/task-42", "编码", "任务"],
    ["/code/session-42", "对话", "编码"],
  ])("returns from %s to the last open item", async (start, away, back) => {
    const user = userEvent.setup();
    mounted(start);

    const rail = within(screen.getByRole("navigation", { name: "主导航" }));
    await user.click(rail.getByRole("link", { name: away }));

    expect(rail.getByRole("link", { name: back })).toHaveAttribute(
      "href",
      start,
    );
    await user.click(rail.getByRole("link", { name: back }));
    expect(screen.getByRole("status", { name: "当前路径" })).toHaveTextContent(
      start,
    );
  });

  it.each([
    ["/workflow", "任务", "/work"],
    ["/code-review", "编码", "/code"],
  ])("does not treat the lookalike path %s as a flow", (start, label, root) => {
    mounted(start);

    const rail = within(screen.getByRole("navigation", { name: "主导航" }));
    expect(rail.getByRole("link", { name: label })).not.toHaveAttribute(
      "aria-current",
    );
    expect(rail.getByRole("link", { name: label })).toHaveAttribute(
      "href",
      root,
    );
  });

  it("keeps primary history isolated when identities change", async () => {
    const user = userEvent.setup();
    mounted("/chat/session-a");

    const switchPrincipal = async (principal: string) => {
      await user.click(screen.getByRole("button", { name: /环境与身份/ }));
      const dialog = within(
        screen.getByRole("dialog", { name: "设置" }),
      );
      await user.click(dialog.getByRole("button", { name: /本地身份/ }));
      const principalField = dialog.getByLabelText("Principal");
      await user.clear(principalField);
      await user.type(principalField, principal);
      await user.click(dialog.getByRole("button", { name: "应用身份" }));
    };

    await switchPrincipal("user_b");
    await waitFor(() =>
      expect(
        screen.getByRole("status", { name: "当前路径" }),
      ).toHaveTextContent("/chat"),
    );
    await user.click(screen.getByRole("link", { name: "打开 B 会话" }));
    expect(screen.getByRole("status", { name: "当前路径" })).toHaveTextContent(
      "/chat/session-b",
    );

    await switchPrincipal("user_local");
    await waitFor(() =>
      expect(
        screen.getByRole("status", { name: "当前路径" }),
      ).toHaveTextContent("/chat/session-a"),
    );
    await switchPrincipal("user_b");
    await waitFor(() =>
      expect(
        screen.getByRole("status", { name: "当前路径" }),
      ).toHaveTextContent("/chat/session-b"),
    );
  });

  it("does not adopt an old primary URL after switching identity on a utility page", async () => {
    const user = userEvent.setup();
    mounted("/chat/session-a");

    const rail = within(screen.getByRole("navigation", { name: "主导航" }));
    await user.click(rail.getByRole("button", { name: "更多" }));
    await user.click(
      within(screen.getByRole("dialog", { name: "更多页面" })).getByRole(
        "link",
        { name: "运行状态" },
      ),
    );
    expect(screen.getByText("System page")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /环境与身份/ }));
    const dialog = within(
      screen.getByRole("dialog", { name: "设置" }),
    );
    await user.click(dialog.getByRole("button", { name: /本地身份/ }));
    const principalField = dialog.getByLabelText("Principal");
    await user.clear(principalField);
    await user.type(principalField, "user_b");
    await user.click(dialog.getByRole("button", { name: "应用身份" }));

    await user.click(screen.getByRole("button", { name: "浏览器后退" }));
    await waitFor(() =>
      expect(
        screen.getByRole("status", { name: "当前路径" }),
      ).toHaveTextContent("/chat"),
    );
    // Chat 此刻是当前工作区，那一行是折叠开关而不是链接，所以记忆改从「离开
    // 再回来」这个方向验：切到 Tasks 之后 Chat 重新变成链接，它的 href 就是
    // 这份记忆本身——要是旧身份的 session-a 还留在里面，这里会看得见。
    await user.click(rail.getByRole("link", { name: "任务" }));
    expect(rail.getByRole("link", { name: "对话" })).toHaveAttribute(
      "href",
      "/chat",
    );
  });

  it("rejects repeated POP entries owned by the previous identity", async () => {
    const user = userEvent.setup();
    mounted("/chat/session-a");

    const rail = within(screen.getByRole("navigation", { name: "主导航" }));
    await user.click(rail.getByRole("link", { name: "任务" }));
    await user.click(screen.getByRole("link", { name: "打开 A 任务" }));
    expect(screen.getByRole("status", { name: "当前路径" })).toHaveTextContent(
      "/work/task-a",
    );

    await user.click(screen.getByRole("button", { name: /环境与身份/ }));
    const dialog = within(
      screen.getByRole("dialog", { name: "设置" }),
    );
    await user.click(dialog.getByRole("button", { name: /本地身份/ }));
    const principalField = dialog.getByLabelText("Principal");
    await user.clear(principalField);
    await user.type(principalField, "user_b");
    await user.click(dialog.getByRole("button", { name: "应用身份" }));
    await waitFor(() =>
      expect(
        screen.getByRole("status", { name: "当前路径" }),
      ).toHaveTextContent("/work"),
    );

    await user.click(screen.getByRole("button", { name: "浏览器后退" }));
    await waitFor(() =>
      expect(
        screen.getByRole("status", { name: "当前路径" }),
      ).toHaveTextContent("/work"),
    );
    await user.click(screen.getByRole("button", { name: "浏览器后退" }));
    await waitFor(() =>
      expect(
        screen.getByRole("status", { name: "当前路径" }),
      ).toHaveTextContent("/chat"),
    );
    // 同上：当前那一行是折叠开关，记忆从「离开再回来」这个方向验。
    await user.click(rail.getByRole("link", { name: "任务" }));
    expect(rail.getByRole("link", { name: "对话" })).toHaveAttribute(
      "href",
      "/chat",
    );
  });
});

describe("AppShell 待处理", () => {
  function mounted(at = "/system") {
    return render(
      <QueryClientProvider client={queryClient}>
      <ThemeProvider>
        <IdentityProvider>
          <MemoryRouter initialEntries={[at]}>
            <Routes>
              <Route element={<AppShell />}>
                <Route element={<p>System page</p>} path="/system" />
                <Route element={<PathProbe />} path="/work/:taskId" />
              </Route>
            </Routes>
          </MemoryRouter>
        </IdentityProvider>
      </ThemeProvider>
      </QueryClientProvider>,
    );
  }

  it("从任何一页都看得见等你批准的任务，点进去就是那个任务", async () => {
    const { listApprovals } = await import("../api/client");
    vi.mocked(listApprovals).mockResolvedValue({
      approvals: [
        {
          approval_id: "apr_1",
          task_id: "task_waiting",
          status: "pending",
          decision_version: 1,
          decided_at: null,
          created_at: "2026-09-05T01:00:00Z",
        },
      ],
      cursor: null,
    });
    const user = userEvent.setup();
    mounted("/system");

    const rail = within(screen.getByRole("navigation", { name: "主导航" }));
    const entry = await rail.findByRole("button", { name: "待处理，1 项" });
    expect(entry).toHaveTextContent("1");
    // 只问 pending 的：一个把已决定的也列出来的队列，会让「我已经答过了」
    // 和「它没了」分不开。
    expect(vi.mocked(listApprovals).mock.calls[0]?.[1]).toMatchObject({
      statuses: ["pending"],
    });

    await user.click(entry);
    const dialog = within(screen.getByRole("dialog", { name: "待处理" }));
    const link = dialog.getByRole("link", { name: /task_waiting/ });
    expect(link).toHaveAttribute("href", "/work/task_waiting");
    // 范围写在脸上：Code 的命令审批不在这里。
    expect(dialog.getByText(/只列任务的审批/)).toBeInTheDocument();
    await user.click(link);
    expect(screen.getByRole("status", { name: "当前路径" })).toHaveTextContent(
      "/work/task_waiting",
    );
    expect(screen.queryByRole("dialog", { name: "待处理" })).toBeNull();
  });

  it("没人在等的时候入口还在，只是没有数字", async () => {
    // 上一条给这个 mock 排了一条审批；mock 跨用例活着，这里明确清空。
    const { listApprovals } = await import("../api/client");
    vi.mocked(listApprovals).mockResolvedValue({ approvals: [], cursor: null });
    const user = userEvent.setup();
    mounted("/system");

    const rail = within(screen.getByRole("navigation", { name: "主导航" }));
    const entry = await rail.findByRole("button", { name: "待处理" });
    expect(entry.querySelector(".aw-rail-badge")).toBeNull();
    await user.click(entry);
    expect(
      within(screen.getByRole("dialog", { name: "待处理" })).getByText(
        "没有等你批准的任务。",
      ),
    ).toBeInTheDocument();
    await user.keyboard("{Escape}");
    await waitFor(() => expect(entry).toHaveFocus());
  });
});

describe("AppShell quick switcher", () => {
  function mounted(at = "/chat") {
    return render(
      <QueryClientProvider client={queryClient}>
      <ThemeProvider>
        <IdentityProvider>
          <MemoryRouter initialEntries={[at]}>
            <Routes>
              <Route element={<AppShell />}>
                <Route element={<p>Chat page</p>} path="/chat" />
                <Route element={<p>Work page</p>} path="/work" />
                <Route element={<p>Code page</p>} path="/code" />
              </Route>
            </Routes>
          </MemoryRouter>
        </IdentityProvider>
      </ThemeProvider>
      </QueryClientProvider>,
    );
  }

  it("opens from the keyboard and goes to an exact Workbench destination", async () => {
    const user = userEvent.setup();
    mounted();

    fireEvent.keyDown(window, { key: "k", ctrlKey: true });
    const dialog = screen.getByRole("dialog", { name: "快速跳转" });
    const search = within(dialog).getByRole("combobox", { name: "搜索页面" });

    // 中文是 label 本身；英文名靠 keywords 兜——输入 tasks 也要能到同一处。
    await user.type(search, "任务");
    expect(within(dialog).getAllByRole("option")).toHaveLength(1);
    expect(
      within(dialog).getByRole("option", { name: /任务/ }),
    ).toBeInTheDocument();

    await user.keyboard("{Enter}");
    expect(screen.getByText("Work page")).toBeInTheDocument();
    expect(
      screen.queryByRole("dialog", { name: "快速跳转" }),
    ).not.toBeInTheDocument();
  });

  it("gives mobile auxiliary pages a purpose before opening the switcher", async () => {
    const user = userEvent.setup();
    mounted();

    const moreTrigger = within(
      screen.getByRole("navigation", { name: "移动端导航" }),
    ).getByRole("button", { name: "更多" });
    await user.click(moreTrigger);
    const more = within(screen.getByRole("dialog", { name: "更多页面" }));
    expect(more.getByText("了解屏幕控制的安全边界")).toBeInTheDocument();
    expect(more.getByText("检查 API、数据库与本地身份")).toBeInTheDocument();

    await user.click(more.getByRole("button", { name: /快速跳转/ }));
    expect(
      screen.queryByRole("dialog", { name: "更多页面" }),
    ).not.toBeInTheDocument();
    expect(
      screen.getByRole("dialog", { name: "快速跳转" }),
    ).toBeInTheDocument();

    await user.keyboard("{Escape}");
    await waitFor(() => expect(moreTrigger).toHaveFocus());
  });

  it("closes with Escape without changing the page", async () => {
    const user = userEvent.setup();
    mounted("/code");

    fireEvent.keyDown(window, { key: "k", ctrlKey: true });
    expect(
      screen.getByRole("dialog", { name: "快速跳转" }),
    ).toBeInTheDocument();
    await user.keyboard("{Escape}");

    expect(
      screen.queryByRole("dialog", { name: "快速跳转" }),
    ).not.toBeInTheDocument();
    expect(screen.getByText("Code page")).toBeInTheDocument();
  });

  it("does not stack over a dialog that may contain an unsaved draft", async () => {
    const user = userEvent.setup();
    mounted();

    await user.click(screen.getByRole("button", { name: /环境与身份/ }));
    expect(
      screen.getByRole("dialog", { name: "设置" }),
    ).toBeInTheDocument();
    fireEvent.keyDown(window, { key: "k", metaKey: true });

    expect(
      screen.queryByRole("dialog", { name: "快速跳转" }),
    ).not.toBeInTheDocument();
  });
});

describe("AppShell 更多面板里没有第二个主题开关", () => {
  function mounted() {
    return render(
      <QueryClientProvider client={queryClient}>
      <ThemeProvider>
        <IdentityProvider>
          <MemoryRouter initialEntries={["/chat"]}>
            <Routes>
              <Route element={<AppShell />}>
                <Route element={<p>Chat page</p>} path="/chat" />
              </Route>
            </Routes>
          </MemoryRouter>
        </IdentityProvider>
      </ThemeProvider>
      </QueryClientProvider>,
    );
  }

  beforeEach(() => {
    localStorage.clear();
    document.documentElement.removeAttribute("data-theme");
  });

  it("主题只在设置的「外观」里改，更多面板不再单列一行", async () => {
    const user = userEvent.setup();
    mounted();

    // 此前这里有一颗「主题：跟随系统」循环三档的按钮，而设置面板的「外观」已经
    // 把三档并排画出来了——同一个开关两个入口、两种形状，读成的是重复。
    const rail = within(screen.getByRole("navigation", { name: "主导航" }));
    await user.click(rail.getByRole("button", { name: "更多" }));
    const more = within(screen.getByRole("dialog", { name: "更多页面" }));
    expect(more.queryByRole("button", { name: /^主题/ })).toBeNull();

    await user.click(more.getByRole("button", { name: "设置" }));
    const settings = within(screen.getByRole("dialog", { name: "设置" }));
    await user.click(settings.getByRole("button", { name: /外观/ }));
    await user.click(settings.getByRole("radio", { name: "深色" }));
    expect(document.documentElement).toHaveAttribute("data-theme", "dark");
  });

  it("窄屏也能到主题：更多 → 设置 → 外观", async () => {
    const user = userEvent.setup();
    mounted();

    await user.click(
      within(screen.getByRole("navigation", { name: "移动端导航" })).getByRole(
        "button",
        { name: "更多" },
      ),
    );
    const more = within(screen.getByRole("dialog", { name: "更多页面" }));
    await user.click(more.getByRole("button", { name: "设置" }));
    const settings = within(screen.getByRole("dialog", { name: "设置" }));
    await user.click(settings.getByRole("button", { name: /外观/ }));
    expect(settings.getByRole("radio", { name: "浅色" })).toBeInTheDocument();
  });
});
