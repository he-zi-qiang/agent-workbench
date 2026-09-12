import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { PrincipalIdentity } from "../../api/types";
import { BrowserFrame } from "./BrowserFrame";

// 这块面现在带身份头去取帧（ADR-0115 装配验证时发现它裸 fetch 一秒一次 401）。
const IDENTITY: PrincipalIdentity = {
  tenantId: "tenant_local",
  principalId: "user_local",
  scopes: [],
};

// 这块面只跟一个端点说话，而且说的是「给我一张图」。替掉 fetch 比替掉一个
// 客户端模块更贴近它真实的样子——它没有走 api/client，因为返回的是二进制而
// 不是 JSON。
const fetchMock = vi.fn();
// Held here rather than reached for as `URL.revokeObjectURL` in the assertion:
// reading a method off an object hands `expect` an unbound function, which is
// what `@typescript-eslint/unbound-method` objects to and is a real hazard for
// any method that uses `this`. Naming the mock says what is meant anyway.
const createObjectURL = vi.fn<() => string>();
const revokeObjectURL = vi.fn<(url: string) => void>();

beforeEach(() => {
  vi.stubGlobal("fetch", fetchMock);
  // jsdom 不带这两个。`createObjectURL` 返回一个可辨认的串，好让断言能确认
  // 图真的挂上去了；`revokeObjectURL` 被记着，因为「一秒一张、不撤销就是一
  // 分钟六十个 blob」是这个组件里一条真实的约束。
  createObjectURL.mockReturnValue("blob:frame-1");
  vi.stubGlobal("URL", { ...URL, createObjectURL, revokeObjectURL });
});

afterEach(() => {
  vi.unstubAllGlobals();
  fetchMock.mockReset();
  createObjectURL.mockReset();
  revokeObjectURL.mockReset();
});

describe("BrowserFrame（ADR-0113 §3.6）", () => {
  // 这一组三条是同一个决定的三面：503、204、200 必须画成三种不同的东西。
  // 把前两种合成一个空框，读者就分不清该去启动一个进程还是该等模型动手。

  it("服务没在跑时，给出把它跑起来的命令", async () => {
    fetchMock.mockResolvedValue({ ok: false, status: 503 });
    render(<BrowserFrame identity={IDENTITY} />);
    await waitFor(() => {
      expect(screen.getByText(/没有在这套部署里应答/)).toBeTruthy();
    });
    expect(screen.getByText(/dev\.sh browser-server/)).toBeTruthy();
  });

  it("在跑但还没打开过页面时，说的是「等一个动作」而不是错误", async () => {
    fetchMock.mockResolvedValue({ ok: true, status: 204 });
    render(<BrowserFrame identity={IDENTITY} />);
    await waitFor(() => {
      expect(screen.getByText(/还没打开过页面/)).toBeTruthy();
    });
    // 而且不能顺带把「没在跑」那句也说出来——那正是这条测试要防的合并。
    expect(screen.queryByText(/没有在这套部署里应答/)).toBeNull();
  });

  it("有帧时把它画出来，并说明这里点不动", async () => {
    fetchMock.mockResolvedValue({
      ok: true,
      status: 200,
      blob: () => Promise.resolve(new Blob([new Uint8Array([0xff, 0xd8])])),
    });
    render(<BrowserFrame identity={IDENTITY} />);
    await waitFor(() => {
      expect(screen.getByAltText("浏览器当前画面")).toBeTruthy();
    });
    expect(
      screen.getByAltText("浏览器当前画面").getAttribute("src"),
    ).toBe("blob:frame-1");
    // ADR-0113 §4：只读是写在面上的，不是靠读者猜的。
    expect(screen.getByText(/点不动它/)).toBeTruthy();
    // 帧那条路由先认身份头，和其他每一条一样（ADR-044）；不带头的那一版在
    // Compose 栈上一秒一次 401，面板永远说「没在跑」。
    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect((init.headers as Record<string, string>)["x-principal-id"]).toBe(
      "user_local",
    );
  });

  it("取不到和没在跑，对读者是同一件事", async () => {
    fetchMock.mockRejectedValue(new TypeError("network"));
    render(<BrowserFrame identity={IDENTITY} />);
    await waitFor(() => {
      expect(screen.getByText(/没有在这套部署里应答/)).toBeTruthy();
    });
  });

  it("卸载时撤销最后一张，不把 blob 留在文档上", async () => {
    fetchMock.mockResolvedValue({
      ok: true,
      status: 200,
      blob: () => Promise.resolve(new Blob([new Uint8Array([0xff, 0xd8])])),
    });
    const view = render(<BrowserFrame identity={IDENTITY} />);
    await waitFor(() => {
      expect(screen.getByAltText("浏览器当前画面")).toBeTruthy();
    });
    view.unmount();
    expect(revokeObjectURL).toHaveBeenCalledWith("blob:frame-1");
  });
});
