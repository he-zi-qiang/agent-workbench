/**
 * 设置面板。
 *
 * 钉的是这次改动实际改掉的那几件事，而不是每一格里的内容——那些各有各的测试
 * （`UsagePage.test.tsx` 管用量怎么读，`SystemPage.test.tsx` 管健康检查怎么说）。
 * 这里管的是**这个框把它们摆在了一起**之后不能走样的部分：
 *
 * 一是**打开时落在「本地身份」**。左下角那颗头像此前打开的就是身份编辑框，一次
 * 改版不该让一个用惯了的按钮换掉它的后果。
 * 二是**四类都换得过去**，而且换过去之后上一类的内容不再留在屏幕上。
 * 三是**主题即点即生效**，不需要再按一次「保存」——这个框里只有身份那一类有草稿。
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { IdentityProvider, useIdentity } from "./IdentityContext";
import { SettingsDialog } from "./SettingsDialog";
import { ThemeProvider } from "./ThemeContext";

vi.mock("../api/client", () => ({
  getUsage: vi.fn().mockResolvedValue({
    window: "30d",
    since: "2026-07-30T00:00:00Z",
    until: "2026-08-29T00:00:00Z",
    by_mode: {},
    by_model: {},
    delegated: {
      tokens: {
        input_tokens: 0,
        output_tokens: 0,
        cache_read_tokens: 0,
        cache_write_tokens: 0,
      },
      cost_micro_usd: 0,
      runs: 0,
    },
    runs_in_flight: 0,
    unpriced_profiles: [],
  }),
  checkHealth: vi.fn().mockResolvedValue({ ok: true }),
  // 这个 mock 工厂没有 `importActual` 展开，所以它没列到的每一个导出在这里都是
  // `undefined`。「模型密钥」那一类一渲染就会去调它们，于是漏掉一个不是让那一类
  // 出错，是让这个文件里七条测试一起红。
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
  // 同一条理由的下一个受害者：「运行状态」那一类现在还渲染能力清单（ADR-102），
  // 它调 `getDeploymentCapabilities`，而 `capabilityErrorMessage` 用 `ApiError`
  // 做 instanceof——两个都得在这里列出来，否则这个文件整片红。
  getDeploymentCapabilities: vi.fn().mockResolvedValue({ capabilities: [] }),
  ApiError: class ApiError extends Error {
    constructor(
      public status: number,
      message: string,
    ) {
      super(message);
    }
  },
}));

/** 这个框自己不带触发器——它读的是 context 里那个开关。 */
function Opener() {
  const { setEditorOpen } = useIdentity();
  return (
    <button onClick={() => setEditorOpen(true)} type="button">
      打开设置
    </button>
  );
}

async function open(user: ReturnType<typeof userEvent.setup>) {
  render(
    <ThemeProvider>
      <IdentityProvider>
        <QueryClientProvider
          client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
        >
          <Opener />
          <SettingsDialog />
        </QueryClientProvider>
      </IdentityProvider>
    </ThemeProvider>,
  );
  await user.click(screen.getByRole("button", { name: "打开设置" }));
  return within(screen.getByRole("dialog", { name: "设置" }));
}

beforeEach(() => {
  localStorage.clear();
  document.documentElement.removeAttribute("data-theme");
});

describe("打开时落在哪一类", () => {
  it("本地身份——那颗头像此前打开的就是它", async () => {
    const user = userEvent.setup();
    const dialog = await open(user);

    expect(
      dialog.getByRole("button", { name: /本地身份/ }),
    ).toHaveAttribute("aria-current", "true");
    expect(dialog.getByLabelText("Tenant")).toBeInTheDocument();
  });
});

describe("换一类", () => {
  it("换过去之后，上一类的内容不再留在屏幕上", async () => {
    const user = userEvent.setup();
    const dialog = await open(user);

    await user.click(dialog.getByRole("button", { name: /外观/ }));

    expect(dialog.queryByLabelText("Tenant")).toBeNull();
    expect(dialog.getByRole("radio", { name: "浅色" })).toBeInTheDocument();
  });

  it("用量那一格里是真的报表，不是一句占位", async () => {
    const user = userEvent.setup();
    const dialog = await open(user);

    await user.click(dialog.getByRole("button", { name: /用量/ }));

    expect(await dialog.findByRole("button", { name: "7 天" })).toBeInTheDocument();
  });

  it("运行状态那一格里是真的健康检查", async () => {
    const user = userEvent.setup();
    const dialog = await open(user);

    await user.click(dialog.getByRole("button", { name: /运行状态/ }));

    expect(
      await dialog.findByRole("button", { name: /重新检查/ }),
    ).toBeInTheDocument();
  });
});

describe("主题", () => {
  it("即点即生效，不需要再按一次保存", async () => {
    const user = userEvent.setup();
    const dialog = await open(user);

    await user.click(dialog.getByRole("button", { name: /外观/ }));
    await user.click(dialog.getByRole("radio", { name: "深色" }));

    // 写到 `<html>` 上，不是等某个「保存」。这一格里没有草稿，所以也没有一颗
    // 按钮为它负责——这正是「应用身份」贴在那三个输入框下面、而不在对话框页脚
    // 的原因。
    await waitFor(() => {
      expect(document.documentElement).toHaveAttribute("data-theme", "dark");
    });
  });

  it("「跟随系统」不写 data-theme——那一档要让 CSS 自己按系统偏好解析", async () => {
    const user = userEvent.setup();
    const dialog = await open(user);

    await user.click(dialog.getByRole("button", { name: /外观/ }));
    await user.click(dialog.getByRole("radio", { name: "深色" }));
    await waitFor(() => {
      expect(document.documentElement).toHaveAttribute("data-theme", "dark");
    });

    await user.click(dialog.getByRole("radio", { name: "跟随系统" }));

    await waitFor(() => {
      expect(document.documentElement).not.toHaveAttribute("data-theme");
    });
  });
});
