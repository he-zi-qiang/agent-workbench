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
              <Route element={<p>Approvals page</p>} path="/approvals" />
            </Route>
          </Routes>
        </MemoryRouter>
      </IdentityProvider>,
    );

    await user.click(screen.getByRole("button", { name: "更多" }));
    const dialog = screen.getByRole("dialog", { name: "更多页面" });
    const more = within(dialog);
    expect(dialog).toBeInTheDocument();
    expect(more.getByRole("link", { name: "待我确认" })).toBeInTheDocument();
    expect(more.getByRole("link", { name: "效果评测" })).toBeInTheDocument();
    expect(more.getByRole("link", { name: "运行状态" })).toBeInTheDocument();
    expect(
      more.getByRole("button", { name: "本地环境与身份" }),
    ).toBeInTheDocument();

    await user.click(more.getByRole("link", { name: "待我确认" }));
    expect(screen.getByText("Approvals page")).toBeInTheDocument();
    expect(screen.queryByRole("dialog", { name: "更多页面" })).not.toBeInTheDocument();
  });
});
