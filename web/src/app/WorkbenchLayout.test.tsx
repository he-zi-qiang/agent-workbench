import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { describe, expect, it } from "vitest";
import { WorkbenchLayout } from "./WorkbenchLayout";

/**
 * Stand-ins for the two real pages. They are not mocked versions of Chat and
 * Work -- they are two routes under the layout, which is all this component
 * knows about either of them. Rendering the real pages here would test their
 * data fetching a second time and tell us nothing about the tab strip.
 */
function mounted(at: string) {
  return render(
    <MemoryRouter initialEntries={[at]}>
      <Routes>
        <Route element={<WorkbenchLayout />}>
          <Route element={<p>对话页</p>} path="/chat" />
          <Route element={<p>对话页 会话</p>} path="/chat/:sessionId" />
          <Route element={<p>任务页</p>} path="/work" />
          <Route element={<p>任务页 任务</p>} path="/work/:taskId" />
        </Route>
      </Routes>
    </MemoryRouter>,
  );
}

function tabs() {
  return within(screen.getByRole("navigation", { name: "工作台" }));
}

describe("WorkbenchLayout", () => {
  it("switches between the two flows in one page, and the URL follows", async () => {
    const user = userEvent.setup();
    mounted("/chat");

    expect(screen.getByText("对话页")).toBeInTheDocument();
    await user.click(tabs().getByRole("link", { name: "任务" }));

    // A button that only set state would swap the pane and leave the address
    // bar behind -- no bookmark, no back button, no copyable link.
    expect(screen.getByText("任务页")).toBeInTheDocument();
  });

  it("comes back to the session you were in, not to a blank composer", async () => {
    const user = userEvent.setup();
    mounted("/chat/ses_1");
    expect(screen.getByText("对话页 会话")).toBeInTheDocument();

    await user.click(tabs().getByRole("link", { name: "任务" }));
    await user.click(tabs().getByRole("link", { name: "对话" }));

    // The layout element does not unmount when the child route changes, which
    // is the only reason the remembered path survives the round trip.
    expect(screen.getByText("对话页 会话")).toBeInTheDocument();
  });

  it("marks the current tab as the page, not as a styled button", async () => {
    const user = userEvent.setup();
    mounted("/chat");

    expect(tabs().getByRole("link", { name: "对话" })).toHaveAttribute(
      "aria-current",
      "page",
    );
    expect(tabs().getByRole("link", { name: "任务" })).not.toHaveAttribute(
      "aria-current",
    );

    await user.click(tabs().getByRole("link", { name: "任务" }));
    expect(tabs().getByRole("link", { name: "任务" })).toHaveAttribute(
      "aria-current",
      "page",
    );
  });

  it("marks the tab on a deep URL, not only on the bare one", () => {
    mounted("/work/task_1");

    // Weaker than it looks, and left in because the weaker claim still holds:
    // currency is by prefix, so a task URL marks 任务. It is *not* evidence for
    // using `Link` over `NavLink` -- a sabotage that switched to href matching
    // kept this green, because the active tab's href is always the current
    // path. The comment that claimed otherwise is gone with it.
    expect(tabs().getByRole("link", { name: "任务" })).toHaveAttribute(
      "aria-current",
      "page",
    );
    expect(tabs().getByRole("link", { name: "对话" })).not.toHaveAttribute(
      "aria-current",
    );
  });
});
