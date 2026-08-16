import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { describe, expect, it } from "vitest";
import { AppShell } from "./AppShell";
import { IdentityProvider } from "./IdentityContext";

describe("AppShell mobile navigation", () => {
  it("makes every auxiliary project page reachable from More", async () => {
    const user = userEvent.setup();
    render(
      <IdentityProvider>
        <MemoryRouter initialEntries={["/chat"]}>
          <Routes>
            <Route element={<AppShell />}>
              <Route element={<p>Chat page</p>} path="/chat" />
              <Route element={<p>Evaluation page</p>} path="/evaluation" />
            </Route>
          </Routes>
        </MemoryRouter>
      </IdentityProvider>,
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
      </IdentityProvider>,
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
