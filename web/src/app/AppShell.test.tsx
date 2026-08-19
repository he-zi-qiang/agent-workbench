import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it } from "vitest";
import { AppShell } from "./AppShell";
import { IdentityProvider } from "./IdentityContext";
import { THEME_STORAGE_KEY, ThemeProvider } from "./ThemeContext";

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

    await user.click(screen.getByRole("button", { name: "更多" }));
    const dialog = screen.getByRole("dialog", { name: "更多页面" });
    const more = within(dialog);
    expect(dialog).toBeInTheDocument();
    expect(more.getByRole("link", { name: "效果评测" })).toBeInTheDocument();
    // Gone with ADR-048. Asserted against the whole document rather than the
    // sheet: an entry added back to `NAVIGATION` but missing from the More
    // filter renders in the desktop rail and nowhere else, and a sheet-only
    // assertion would call that deleted.
    expect(screen.queryByRole("link", { name: "待我确认" })).not.toBeInTheDocument();
    expect(more.getByRole("link", { name: "运行状态" })).toBeInTheDocument();
    // The page this filter was derived for. It used to name /evaluation and
    // /system literally, so 计算机 reached the desktop rail and no mobile
    // surface at all -- the exact shape the comment above describes.
    expect(more.getByRole("link", { name: "计算机" })).toBeInTheDocument();
    expect(
      more.getByRole("button", { name: "本地环境与身份" }),
    ).toBeInTheDocument();

    await user.click(more.getByRole("link", { name: "效果评测" }));
    expect(screen.getByText("Evaluation page")).toBeInTheDocument();
    expect(screen.queryByRole("dialog", { name: "更多页面" })).not.toBeInTheDocument();
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

  it("marks 工作台 as current on the task half too", () => {
    mounted("/work");

    // One rail entry stands for two routes now. Its `to` is `/chat`, so the
    // default link matching leaves it dark the moment somebody opens the 任务
    // tab -- a rail that disagrees with the page it is showing.
    const rail = within(screen.getByRole("navigation", { name: "主导航" }));
    expect(rail.getByRole("link", { name: "工作台" })).toHaveAttribute(
      "aria-current",
      "page",
    );
    expect(rail.getByRole("link", { name: "Code" })).not.toHaveAttribute(
      "aria-current",
    );
  });

  it("groups 工作台 and Code above the rail's dividing line", () => {
    mounted("/chat");

    // The two primary flows are one group, and the line belongs under them
    // rather than between them. It used to be pinned to `index === 1`, which
    // drew it above Code and separated the pair it was meant to join.
    const rail = within(screen.getByRole("navigation", { name: "主导航" }));
    const wrapperOf = (name: string) =>
      rail.getByRole("link", { name }).parentElement;

    expect(wrapperOf("工作台")).not.toHaveClass("aw-nav-divider");
    expect(wrapperOf("Code")).not.toHaveClass("aw-nav-divider");
    // The first entry that is not a primary flow is where the group ends.
    expect(wrapperOf("知识库")).toHaveClass("aw-nav-divider");
  });

  it("offers two top-level flows, not three", () => {
    mounted("/chat");

    // The control for the item above: with 工作台 covering two routes, a rail
    // that still listed Chat and Work separately would satisfy the current
    // marking and contradict the tab strip.
    const rail = within(screen.getByRole("navigation", { name: "主导航" }));
    expect(rail.queryByRole("link", { name: "Chat" })).not.toBeInTheDocument();
    expect(rail.queryByRole("link", { name: "Work" })).not.toBeInTheDocument();
    expect(rail.getByRole("link", { name: "工作台" })).toBeInTheDocument();
    expect(rail.getByRole("link", { name: "Code" })).toBeInTheDocument();
  });
});

describe("AppShell theme control", () => {
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

  it("starts on 跟随系统 and writes no data-theme for it", () => {
    mounted();

    const rail = within(screen.getByRole("navigation", { name: "主导航" }));
    expect(rail.getByRole("button", { name: "跟随系统" })).toBeInTheDocument();
    // 跟随系统这一档的做法是**不写属性**，把决定权留给 CSS 的
    // `color-scheme: light dark`。写一个 data-theme="system" 也能让按钮显示对，
    // 但 tokens.css 那两条覆盖规则选的是 light/dark，属性会变成一个没人读的字符串
    // ——而没人读的状态迟早会和真正生效的那个分叉。
    expect(document.documentElement).not.toHaveAttribute("data-theme");
  });

  it("cycles system → light → dark → system, and the attribute follows", async () => {
    const user = userEvent.setup();
    mounted();

    const rail = within(screen.getByRole("navigation", { name: "主导航" }));
    await user.click(rail.getByRole("button", { name: "跟随系统" }));
    expect(document.documentElement).toHaveAttribute("data-theme", "light");

    await user.click(rail.getByRole("button", { name: "浅色" }));
    expect(document.documentElement).toHaveAttribute("data-theme", "dark");

    // 回到 system 时属性要被**移除**，不是留一个旧值。留着的话，用户选回
    // 「跟随系统」之后系统再切深浅，界面不会跟着动。
    await user.click(rail.getByRole("button", { name: "深色" }));
    expect(document.documentElement).not.toHaveAttribute("data-theme");
  });

  it("survives a reload", async () => {
    const user = userEvent.setup();
    const first = mounted();

    await user.click(
      within(screen.getByRole("navigation", { name: "主导航" })).getByRole("button", {
        name: "跟随系统",
      }),
    );
    first.unmount();
    document.documentElement.removeAttribute("data-theme");

    mounted();
    expect(
      within(screen.getByRole("navigation", { name: "主导航" })).getByRole("button", {
        name: "浅色",
      }),
    ).toBeInTheDocument();
    expect(document.documentElement).toHaveAttribute("data-theme", "light");
  });

  it("ignores a stored value that is no longer one of the three", () => {
    // 手写进 localStorage 的旧值/脏值。落回第一档，而不是把它当成属性写到
    // <html> 上——后者会得到一个 CSS 里没有对应规则的主题，界面按浅色渲染而
    // 按钮显示着那个不存在的名字。
    localStorage.setItem(THEME_STORAGE_KEY, JSON.stringify("solarized"));
    mounted();

    expect(
      within(screen.getByRole("navigation", { name: "主导航" })).getByRole("button", {
        name: "跟随系统",
      }),
    ).toBeInTheDocument();
    expect(document.documentElement).not.toHaveAttribute("data-theme");
  });

  it("reaches the theme from mobile, where the rail is hidden", async () => {
    const user = userEvent.setup();
    mounted();

    await user.click(screen.getByRole("button", { name: "更多" }));
    const more = within(screen.getByRole("dialog", { name: "更多页面" }));
    await user.click(more.getByRole("button", { name: "主题：跟随系统" }));

    expect(document.documentElement).toHaveAttribute("data-theme", "light");
    // 面板不关：连点三下看三档，比每点一次都要重新打开「更多」合理。
    expect(screen.getByRole("dialog", { name: "更多页面" })).toBeInTheDocument();
    expect(more.getByRole("button", { name: "主题：浅色" })).toBeInTheDocument();
  });
});
