import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { IdentityProvider } from "../../app/IdentityContext";
import { ProviderKeyPanel } from "./ProviderKeyPanel";

const getProviderKey = vi.hoisted(() => vi.fn());
const storeProviderKey = vi.hoisted(() => vi.fn());
const clearProviderKey = vi.hoisted(() => vi.fn());

vi.mock("../../api/client", () => ({
  getProviderKey,
  storeProviderKey,
  clearProviderKey,
}));

/** 服务端遮好的状态。控制台从不拿到比这更多的东西。 */
const NOTHING = {
  active: false,
  stored: false,
  fingerprint: null,
  path: "~/.config/agent-workbench/key",
  restart_required: false,
  restart_hint: "",
};

function show() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
    <IdentityProvider>
      <QueryClientProvider client={client}>
        <ProviderKeyPanel />
      </QueryClientProvider>
    </IdentityProvider>,
  );
}

describe("模型密钥", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    getProviderKey.mockResolvedValue(NOTHING);
    storeProviderKey.mockResolvedValue({ ...NOTHING, stored: true });
    clearProviderKey.mockResolvedValue(NOTHING);
  });

  it("没有 key 时说出模型调用不可用，而不是只显示一个空框", async () => {
    show();
    expect(await screen.findByText(/模型调用不可用/)).toBeInTheDocument();
  });

  it("保存后把「已存下」和「正在用」分开说，并要求重启", async () => {
    getProviderKey.mockResolvedValue({
      ...NOTHING,
      stored: true,
      fingerprint: "…1234",
      restart_required: true,
      restart_hint: "重启 agent-api 与 agent-task-worker 后这把 key 才会生效。",
    });
    show();
    // 两个问题，两个答案：存下了，但这个进程没有在用它。
    expect(await screen.findByText("…1234")).toBeInTheDocument();
    expect(screen.getByText(/重启 agent-api/)).toBeInTheDocument();
    expect(screen.getByText(/模型调用不可用/)).toBeInTheDocument();
  });

  it("输入框是 password 型，且从不被已存的 key 填回", async () => {
    getProviderKey.mockResolvedValue({
      ...NOTHING,
      stored: true,
      active: true,
      fingerprint: "…1234",
    });
    show();
    // 两格都显示指纹（正在用的和已存下的是同一把），所以这里是 All 而不是单数。
    await screen.findAllByText("…1234");
    const input = screen.getByLabelText("新的 API key");
    // 服务端没有返回明文的方法，所以这里也没有可回填的东西——空框是对的。
    expect(input).toHaveValue("");
    expect(input).toHaveAttribute("type", "password");
  });

  it("空输入时保存键是禁用的", async () => {
    show();
    await screen.findByText(/模型调用不可用/);
    expect(screen.getByRole("button", { name: /保存/ })).toBeDisabled();
  });

  it("保存后清空输入框，不把刚提交的 key 留在屏幕上", async () => {
    const user = userEvent.setup();
    show();
    await screen.findByText(/模型调用不可用/);
    const input = screen.getByLabelText("新的 API key");
    await user.type(input, "example-not-a-credential-0001");
    await user.click(screen.getByRole("button", { name: /保存/ }));

    await waitFor(() =>
      expect(storeProviderKey).toHaveBeenCalledWith(
        expect.anything(),
        "example-not-a-credential-0001",
      ),
    );
    await waitFor(() => expect(input).toHaveValue(""));
  });

  it("没有已存的 key 时，删除键是禁用的", async () => {
    show();
    await screen.findByText(/模型调用不可用/);
    expect(
      screen.getByRole("button", { name: /删除已存的 key/ }),
    ).toBeDisabled();
  });

  it("读不到状态时说出来，而不是显示一个看起来正常的空面板", async () => {
    getProviderKey.mockRejectedValue(new Error("boom"));
    show();
    expect(await screen.findByRole("alert")).toHaveTextContent("boom");
  });
});
