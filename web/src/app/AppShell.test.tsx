import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import {
  Link,
  MemoryRouter,
  Route,
  Routes,
  useLocation,
  useNavigate,
} from "react-router-dom";
import { beforeEach, describe, expect, it } from "vitest";
import { AppShell } from "./AppShell";
import { IdentityProvider } from "./IdentityContext";
import { ThemeProvider } from "./ThemeContext";
import {
  useWorkspaceSidebar,
  WorkspaceSidebarPortal,
} from "./WorkspaceSidebar";

beforeEach(() => {
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
      </ThemeProvider>,
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
      </ThemeProvider>,
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
    expect(settings.getByLabelText("Tenant")).toHaveFocus();
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
      </ThemeProvider>,
    );
  }

  it("marks 任务 as its own current primary flow", () => {
    mounted("/work");

    const rail = within(screen.getByRole("navigation", { name: "主导航" }));
    // 当前那一项不是链接：它已经在自己的页面上了，所以那一行的工作是「这一组
    // 展开不展开」，渲染成一个 disclosure 按钮。其余几项仍然是链接。
    const here = rail.getByRole("button", { name: "Tasks" });
    expect(here).toHaveAttribute("aria-current", "page");
    expect(here).toHaveAttribute("aria-expanded", "true");
    expect(rail.getByRole("link", { name: "Chat" })).not.toHaveAttribute(
      "aria-current",
    );
    expect(rail.getByRole("link", { name: "Code" })).not.toHaveAttribute(
      "aria-current",
    );
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

  it("offers Chat, Tasks and Code as three top-level flows", () => {
    mounted("/chat");

    const rail = within(screen.getByRole("navigation", { name: "主导航" }));
    // 挂载在 /chat，所以 Chat 是那个 disclosure 按钮，另外两项是链接。
    expect(rail.getByRole("button", { name: "Chat" })).toBeInTheDocument();
    expect(rail.getByRole("link", { name: "Tasks" })).toBeInTheDocument();
    expect(rail.getByRole("link", { name: "Code" })).toBeInTheDocument();
    // 三个工作区一起改成英文，所以旧的中文名不该还留在栏里：一栏两套名字，
    // 读屏念一套、搜索命中另一套。
    expect(rail.queryByRole("link", { name: "对话" })).not.toBeInTheDocument();
    expect(rail.queryByRole("link", { name: "编码" })).not.toBeInTheDocument();
  });

  it("nests the active feature's real work items in the one shell sidebar", async () => {
    const user = userEvent.setup();
    render(
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
      </ThemeProvider>,
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
      </ThemeProvider>,
    );

    const disclosure = screen.getByRole("button", { name: "Chat" });
    await user.click(disclosure);
    expect(disclosure).toHaveAttribute("aria-expanded", "false");

    const host = document.getElementById("workspace-sidebar-context");
    expect(host).not.toBeNull();
    expect(host?.querySelector('[aria-label="最近对话"]')).not.toBeNull();
  });

  it.each([
    ["/chat/session-42", "Tasks", "Chat"],
    ["/work/task-42", "Code", "Tasks"],
    ["/code/session-42", "Chat", "Code"],
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
    ["/workflow", "Tasks", "/work"],
    ["/code-review", "Code", "/code"],
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
    await user.click(rail.getByRole("link", { name: "Tasks" }));
    expect(rail.getByRole("link", { name: "Chat" })).toHaveAttribute(
      "href",
      "/chat",
    );
  });

  it("rejects repeated POP entries owned by the previous identity", async () => {
    const user = userEvent.setup();
    mounted("/chat/session-a");

    const rail = within(screen.getByRole("navigation", { name: "主导航" }));
    await user.click(rail.getByRole("link", { name: "Tasks" }));
    await user.click(screen.getByRole("link", { name: "打开 A 任务" }));
    expect(screen.getByRole("status", { name: "当前路径" })).toHaveTextContent(
      "/work/task-a",
    );

    await user.click(screen.getByRole("button", { name: /环境与身份/ }));
    const dialog = within(
      screen.getByRole("dialog", { name: "设置" }),
    );
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
    await user.click(rail.getByRole("link", { name: "Tasks" }));
    expect(rail.getByRole("link", { name: "Chat" })).toHaveAttribute(
      "href",
      "/chat",
    );
  });
});

describe("AppShell quick switcher", () => {
  function mounted(at = "/chat") {
    return render(
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
      </ThemeProvider>,
    );
  }

  it("opens from the keyboard and goes to an exact Workbench destination", async () => {
    const user = userEvent.setup();
    mounted();

    fireEvent.keyDown(window, { key: "k", ctrlKey: true });
    const dialog = screen.getByRole("dialog", { name: "快速跳转" });
    const search = within(dialog).getByRole("combobox", { name: "搜索页面" });

    // 输入中文仍然要能找到它：label 现在是 Tasks，中文靠 keywords 里的别名兜。
    await user.type(search, "任务");
    expect(within(dialog).getAllByRole("option")).toHaveLength(1);
    expect(
      within(dialog).getByRole("option", { name: /Tasks/ }),
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
      </ThemeProvider>,
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
