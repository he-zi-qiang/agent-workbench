import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { BrowserFrame } from "./BrowserFrame";

// 这块面只跟一个端点说话，而且说的是「给我一张图」。替掉 fetch 比替掉一个
// 客户端模块更贴近它真实的样子——它没有走 api/client，因为返回的是二进制而
// 不是 JSON。
const fetchMock = vi.fn();

beforeEach(() => {
  vi.stubGlobal("fetch", fetchMock);
  // jsdom 不带这两个。`createObjectURL` 返回一个可辨认的串，好让断言能确认
  // 图真的挂上去了；`revokeObjectURL` 被记着，因为「一秒一张、不撤销就是一
  // 分钟六十个 blob」是这个组件里一条真实的约束。
  vi.stubGlobal("URL", {
    ...URL,
    createObjectURL: vi.fn(() => "blob:frame-1"),
    revokeObjectURL: vi.fn(),
  });
});

afterEach(() => {
  vi.unstubAllGlobals();
  fetchMock.mockReset();
});

describe("BrowserFrame（ADR-0112 §3.6）", () => {
  // 这一组三条是同一个决定的三面：503、204、200 必须画成三种不同的东西。
  // 把前两种合成一个空框，读者就分不清该去启动一个进程还是该等模型动手。

  it("服务没在跑时，给出把它跑起来的命令", async () => {
    fetchMock.mockResolvedValue({ ok: false, status: 503 });
    render(<BrowserFrame />);
    await waitFor(() => {
      expect(screen.getByText(/没有在这套部署里应答/)).toBeTruthy();
    });
    expect(screen.getByText(/dev\.sh browser-server/)).toBeTruthy();
  });

  it("在跑但还没打开过页面时，说的是「等一个动作」而不是错误", async () => {
    fetchMock.mockResolvedValue({ ok: true, status: 204 });
    render(<BrowserFrame />);
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
      blob: async () => new Blob([new Uint8Array([0xff, 0xd8])]),
    });
    render(<BrowserFrame />);
    await waitFor(() => {
      expect(screen.getByAltText("浏览器当前画面")).toBeTruthy();
    });
    expect(
      screen.getByAltText("浏览器当前画面").getAttribute("src"),
    ).toBe("blob:frame-1");
    // ADR-0112 §4：只读是写在面上的，不是靠读者猜的。
    expect(screen.getByText(/点不动它/)).toBeTruthy();
  });

  it("取不到和没在跑，对读者是同一件事", async () => {
    fetchMock.mockRejectedValue(new TypeError("network"));
    render(<BrowserFrame />);
    await waitFor(() => {
      expect(screen.getByText(/没有在这套部署里应答/)).toBeTruthy();
    });
  });

  it("卸载时撤销最后一张，不把 blob 留在文档上", async () => {
    fetchMock.mockResolvedValue({
      ok: true,
      status: 200,
      blob: async () => new Blob([new Uint8Array([0xff, 0xd8])]),
    });
    const view = render(<BrowserFrame />);
    await waitFor(() => {
      expect(screen.getByAltText("浏览器当前画面")).toBeTruthy();
    });
    view.unmount();
    expect(URL.revokeObjectURL).toHaveBeenCalledWith("blob:frame-1");
  });
});
