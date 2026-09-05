import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { getComputerSession } from "../../api/client";
import { IdentityProvider } from "../../app/IdentityContext";
import { ComputerPage } from "./ComputerPage";

vi.mock("../../api/client", async () => {
  const actual = await vi.importActual("../../api/client");
  return { ...actual, getComputerSession: vi.fn() };
});

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
 * **That last sentence used to be "the page reads no endpoint, so there is
 * nothing to mock and no provider to wrap it in -- a property of the page, not
 * a shortcut here".** It stopped being true with ADR-095, which built the read
 * path this page had spent its whole life saying did not exist. It is quoted
 * rather than deleted because it is the third time a claim on this page
 * outlived the thing it described, and the page's own docblock is about exactly
 * that.
 *
 * So there is a provider now, and one call to mock. What did not change is the
 * reason the page was careful: a plausible allowlist is read as the real one.
 * The tests below pin the three states that keeps -- unreachable, reachable and
 * empty, reachable and populated -- as three different things.
 */
/** The shape the route answers with when that server is not running. */
const UNREACHABLE = {
  reachable: false as const,
  host_platform: "darwin" as const,
  session: null,
  detail: "屏幕控制服务器没有在这台机器上应答。",
};

function draw() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <IdentityProvider>
        <ComputerPage />
      </IdentityProvider>
    </QueryClientProvider>,
  );
}

describe("ComputerPage", () => {
  afterEach(cleanup);

  beforeEach(() => {
    vi.mocked(getComputerSession).mockReset();
    // The ordinary machine: that server is not started by any ordinary path,
    // so this is what the rule-reading tests below should see behind them.
    vi.mocked(getComputerSession).mockResolvedValue(UNREACHABLE);
  });

  it("states both halves of what activation changed", () => {
    draw();

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
    const { container } = draw();

    expect(container.textContent).toContain("不说最前面的是谁");
    expect(container.textContent).toContain("把每一次被拒的激活变成");
    expect(container.textContent).toContain("此刻这个人在用什么");
  });

  it("says activation never starts an application", () => {
    draw();

    // Two nodes carry this phrase since the F-30 notice went in: the claim
    // itself, and the notice referring back to it. Assert the claim's own
    // element rather than uniqueness, which is a fact about the page's
    // wording rather than about what it promises.
    expect(
      screen.getByText("从不启动应用", { selector: "strong" }),
    ).toBeInTheDocument();
    expect(screen.getByText(/known-gaps F-29/)).toBeInTheDocument();
  });

  it("states the four conditions activation actually requires", () => {
    // The page promises to mirror the gate. Activation works as of ADR-092,
    // but only because the server is packaged a particular way -- and a page
    // that described the two checks without saying so would leave a reader
    // thinking any deployment gets this.
    const { container } = draw();

    expect(
      screen.getByText(/这个能力要求服务器自己是一个签名的 \.app（ADR-092）。/),
    ).toBeInTheDocument();
    const prose = container.textContent ?? "";
    expect(prose).toContain("主线程活着的 run loop");
    // The measured evidence, including the shape of every failure.
    expect(prose).toContain("15/15");
    expect(prose).toContain("失败");
  });

  it("does not claim a tier that can move the cursor or drag", () => {
    const { container } = draw();
    const prose = container.textContent ?? "";

    // The regression this whole batch is about, in the direction this file can
    // actually see it: a permission claimed in prose that nothing performs.
    expect(prose).not.toContain("移动指针");
    expect(prose).not.toContain("拖拽");
    // And the row it was removed from still describes what `click` does have.
    expect(screen.getByText(/可以左键单击、滚动，不能打字/)).toBeInTheDocument();
  });

  it("says the machine is not running that server rather than showing nothing", async () => {
    // The ordinary answer on an ordinary machine, and the state this page spent
    // its whole life not being able to distinguish from any other. Not an error
    // and not styled as one: nothing is wrong, that server simply is not up.
    draw();

    expect(
      await screen.findByText(/屏幕控制服务器没有在跑/),
    ).toBeInTheDocument();
    expect(screen.getByText(/scripts\/dev\.sh computer-server/)).toBeInTheDocument();
  });

  it("names the Windows launcher when the API itself runs on Windows", async () => {
    // The page used to tell everyone to run `scripts/dev.sh computer-server`,
    // which exists only on macOS. The launcher is chosen by where the API
    // process runs, never by the browser's OS (2026-09-04 review).
    vi.mocked(getComputerSession).mockResolvedValue({
      ...UNREACHABLE,
      host_platform: "win32",
    });
    draw();

    expect(await screen.findByText(/scripts\\computer\.cmd/)).toBeInTheDocument();
    expect(screen.queryByText(/scripts\/dev\.sh computer-server/)).toBeNull();
  });

  it("sends a containerised API's reader to the host, with both launchers", async () => {
    // A Linux API is the Compose stack: the server cannot run inside it
    // (ADR-0108), and which host is outside is not something this process
    // can see -- so it names both host commands rather than guessing one.
    vi.mocked(getComputerSession).mockResolvedValue({
      ...UNREACHABLE,
      host_platform: "linux",
    });
    const { container } = draw();

    await screen.findByText(/屏幕控制服务器没有在跑/);
    const prose = container.textContent ?? "";
    expect(prose).toContain("宿主机");
    expect(prose).toContain("scripts\\computer.cmd");
    expect(prose).toContain("scripts/dev.sh computer-server");
  });

  it("offers a recheck right where the not-running answer is", async () => {
    // 「未启动 → 对应系统的启动方式 → 重新检查」是首屏要回答的三件事；第三件
    // 此前只有页面顶上一颗按钮都没有——只能等 4 秒轮询。
    draw();

    expect(
      await screen.findByRole("button", { name: "重新检查" }),
    ).toBeInTheDocument();
  });

  it("folds the rules under 工程说明 so the first screen is the machine's state", async () => {
    draw();

    await screen.findByText(/屏幕控制服务器没有在跑/);
    const fold = screen.getByText("工程说明").closest("details");
    expect(fold).not.toBeNull();
    expect(fold).not.toHaveAttribute("open");
    // Folded, not removed: the four checks are still on the page.
    expect(fold?.textContent).toContain("门禁四道检查");
  });

  it("tells an empty allowlist apart from a server that is not answering", async () => {
    // The distinction this whole page has been careful about since it was
    // written. Before ADR-095 it could not be drawn at all, so the page drew
    // neither; drawing them the same way now would be the same mistake with a
    // route behind it.
    vi.mocked(getComputerSession).mockResolvedValue({
      reachable: true,
      detail: "",
      host_platform: "darwin",
      session: {
        service: "agent-workbench-computer",
        scope: "process",
        granted: [],
        frontmost: { bundle_id: "com.apple.Notes", name: "Notes", granted: false },
        actions: [],
      },
    });
    draw();

    expect(await screen.findByText(/还没有任何应用被批准/)).toBeInTheDocument();
    expect(screen.queryByText(/屏幕控制服务器没有在跑/)).toBeNull();
  });

  it("names the frontmost window the model was refused without being told", async () => {
    // ADR-095 in one assertion, on the page that used to be the argument for
    // showing nothing. The reader is sitting in front of this window; the model
    // that was just refused was not told which one it is.
    vi.mocked(getComputerSession).mockResolvedValue({
      reachable: true,
      detail: "",
      host_platform: "darwin",
      session: {
        service: "agent-workbench-computer",
        scope: "process",
        granted: [
          { bundle_id: "com.apple.Notes", name: "Notes", tier: "full" },
        ],
        frontmost: { bundle_id: "com.apple.mail", name: "Mail", granted: false },
        actions: [
          {
            at: "2026-08-29T11:20:18+00:00",
            action: "left_click",
            application: { bundle_id: "com.apple.mail", name: "Mail" },
            allowed: false,
            reason: "The frontmost application is not in this session's approved list.",
            detail: "",
          },
        ],
      },
    });
    draw();

    // 两处：前台那一栏，和被拒的那一行。两处都该有——面板说的是同一扇窗
    // 停住了任务。
    expect(await screen.findAllByText("Mail")).toHaveLength(2);
    expect(screen.getByText(/任务会停在这里等你/)).toBeInTheDocument();
    // And the refusal the model read, quoted rather than rewritten -- the two
    // readers see the same sentence, which is what lets the panel explain why
    // something stopped.
    expect(
      screen.getByText(/not in this session's approved list/),
    ).toBeInTheDocument();
  });

  it("calls the allowlist the process's and not the session's", async () => {
    // One word, load bearing: grants hang on the process, not on an MCP session
    // (known-gap F-19). A panel captioned "this session" would be the first
    // place somebody read a session-scoped grant into existence.
    vi.mocked(getComputerSession).mockResolvedValue({
      reachable: true,
      detail: "",
      host_platform: "darwin",
      session: {
        service: "agent-workbench-computer",
        scope: "process",
        granted: [],
        frontmost: { bundle_id: "com.apple.Notes", name: "Notes", granted: true },
        actions: [],
      },
    });
    const { container } = draw();

    await screen.findByText(/正在应答/);
    const prose = container.textContent ?? "";
    expect(prose).toContain("批准挂在这个");
    expect(prose).toContain("进程一关就清空");
  });
});
