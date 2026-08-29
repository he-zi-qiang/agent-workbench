import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { ComputerPage } from "./ComputerPage";

/**
 * This page is a hand copy of `domain/computer.py` and `computer_mcp/gate.py`,
 * and its own docblock says so: if those change and this does not, this page
 * is wrong.
 *
 * **These tests cannot check that.** They run in a browser environment with no
 * access to the Python they mirror, so what they pin is narrower and worth
 * naming: that the claims this batch decided the page makes are the ones it
 * still makes, and that the specific claim which went stale -- a `click` tier
 * that could "移动指针", copied from an `_ALLOWED` entry no tool could ever
 * reach -- does not come back (ADR-091 §2.4).
 *
 * The page reads no endpoint, so there is nothing to mock and no provider to
 * wrap it in. That is a property of the page, not a shortcut here.
 */
describe("ComputerPage", () => {
  afterEach(cleanup);

  it("states both halves of what activation changed", () => {
    render(<ComputerPage />);

    // The widening, said plainly rather than buried: check 3 answers a weaker
    // question than it did, and a page that only listed the new capability
    // would be describing a permission model this project no longer has.
    expect(
      screen.getByText(/第 3 道从「人选了这扇窗」变成「模型在人批准的集合里选了一扇」/),
    ).toBeInTheDocument();

    // And the narrowing that makes it acceptable.
    expect(
      screen.getByText(/此刻最前面的那个也在名单里吗？/),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/把屏幕从正在被使用的那扇窗抢回来，不是/),
    ).toBeInTheDocument();
  });

  it("says the refusal does not name the window it refused for", () => {
    // Read off the container rather than with `getByText`: the sentence is
    // split across a `<strong>`, and JSX line wrapping puts whitespace inside
    // it, so no single text node holds it and no exact string matches it.
    const { container } = render(<ComputerPage />);

    expect(container.textContent).toContain("不说最前面的是谁");
    expect(container.textContent).toContain("把每一次被拒的激活变成");
    expect(container.textContent).toContain("此刻这个人在用什么");
  });

  it("says activation never starts an application", () => {
    render(<ComputerPage />);

    expect(screen.getByText(/从不启动应用/)).toBeInTheDocument();
    expect(screen.getByText(/known-gaps F-29/)).toBeInTheDocument();
  });

  it("does not claim a tier that can move the cursor or drag", () => {
    const { container } = render(<ComputerPage />);
    const prose = container.textContent ?? "";

    // The regression this whole batch is about, in the direction this file can
    // actually see it: a permission claimed in prose that nothing performs.
    expect(prose).not.toContain("移动指针");
    expect(prose).not.toContain("拖拽");
    // And the row it was removed from still describes what `click` does have.
    expect(screen.getByText(/可以左键单击、滚动，不能打字/)).toBeInTheDocument();
  });

  it("still refuses to show a session's grants, having no endpoint for them", () => {
    render(<ComputerPage />);

    expect(
      screen.getByText("这一页说明机制，不监控运行中的会话。"),
    ).toBeInTheDocument();
  });
});
