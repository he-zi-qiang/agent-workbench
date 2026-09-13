import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { PrincipalIdentity } from "../../api/types";
import { BrowserFrame, relativeToProject } from "./BrowserFrame";

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
      headers: { get: (): string | null => null },
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
    expect(screen.getByText(/点一下画面就能操作它/)).toBeTruthy();
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
      headers: { get: (): string | null => null },
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


describe("BrowserFrame 可以操作（ADR-0117，按住就是按住见 ADR-0120）", () => {
  const frameResponse = {
    ok: true,
    status: 200,
    headers: { get: (): string | null => null },
    blob: () => Promise.resolve(new Blob([new Uint8Array([0xff, 0xd8])])),
  };

  /** 每一次送进浏览器的请求体里的动作，按送出的顺序摊平。 */
  function sentActions(): unknown[] {
    return fetchMock.mock.calls
      .filter(([url]) => String(url).endsWith("/v1/browser/input"))
      .flatMap(([, init]) => {
        const body = JSON.parse((init as RequestInit).body as string) as {
          actions: unknown[];
        };
        return body.actions;
      });
  }

  function answering(
    input: { ok: boolean; status: number; body: unknown },
    delayMs = 0,
  ) {
    fetchMock.mockImplementation((url: string) => {
      if (!url.endsWith("/v1/browser/input")) {
        return Promise.resolve(frameResponse);
      }
      const reply = {
        ok: input.ok,
        status: input.status,
        headers: { get: (): string | null => "application/json" },
        json: () => Promise.resolve(input.body),
        text: () => Promise.resolve(JSON.stringify(input.body)),
      };
      return delayMs === 0
        ? Promise.resolve(reply)
        : new Promise((resolve) => setTimeout(() => resolve(reply), delayMs));
    });
  }

  it("按下再松开画面，送进去的是视口坐标上的一次按下和一次松开", async () => {
    answering({ ok: true, status: 200, body: { done: [], turns_in_flight: 0 } });
    render(<BrowserFrame identity={IDENTITY} />);
    const image = await waitFor(() => screen.getByAltText("浏览器当前画面"));
    const stage = screen.getByRole("application");

    // jsdom 里图没有尺寸，换算退回到浏览器那一侧的视口大小（1280×800）——
    // 比例 1，所以按在 (40, 30) 就是视口的 (40, 30)。一次点击在页面里就是一次
    // 按下加一次松开，浏览器自己会合成 click。
    fireEvent.mouseDown(stage, { button: 0, clientX: 40, clientY: 30 });
    fireEvent.mouseUp(stage, { button: 0, clientX: 40, clientY: 30 });
    expect(image).toBeTruthy();

    await waitFor(() => {
      expect(sentActions()).toEqual([
        { kind: "mouse_down", x: 40, y: 30 },
        { kind: "mouse_up", x: 40, y: 30 },
      ]);
    });
    const [, init] = fetchMock.mock.calls.find(([url]) =>
      String(url).endsWith("/v1/browser/input"),
    ) as [string, RequestInit];
    expect(init.method).toBe("POST");
  });

  it("按住方向键：送一次按下、按住期间的重复不送、松开送一次抬起；Ctrl 组合不送", async () => {
    // 这是用户那句「人还是无法控制在浏览器中的项目预览」的根：第一版每个键送一次
    // `key`，浏览器那一侧按下和抬起一口气做完，马里奥每一帧读一次 `keys.right`，
    // 两者之间一帧都没跑——按住方向键，角色一步也不走（ADR-0120）。
    answering({ ok: true, status: 200, body: { done: [], turns_in_flight: 0 } });
    render(<BrowserFrame identity={IDENTITY} />);
    await waitFor(() => screen.getByAltText("浏览器当前画面"));
    const stage = screen.getByRole("application");

    fireEvent.keyDown(stage, { key: "ArrowRight" });
    fireEvent.keyDown(stage, { key: "ArrowRight", repeat: true });
    fireEvent.keyDown(stage, { key: "ArrowRight", repeat: true });
    fireEvent.keyDown(stage, { key: "r", ctrlKey: true });
    fireEvent.keyUp(stage, { key: "ArrowRight" });

    await waitFor(() => {
      expect(sentActions()).toEqual([
        { kind: "key_down", text: "ArrowRight" },
        { kind: "key_up", text: "ArrowRight" },
      ]);
    });
  });

  it("上一次还没回来时按下的键不会被丢掉，按顺序送到", async () => {
    // 第一版在上一次没回来时直接丢掉新的输入。对轻点无所谓，对抬起是灾难：丢掉
    // 的若是 `key_up`，浏览器那边这个键就一直按着。
    answering({ ok: true, status: 200, body: { done: [], turns_in_flight: 0 } }, 30);
    render(<BrowserFrame identity={IDENTITY} />);
    await waitFor(() => screen.getByAltText("浏览器当前画面"));
    const stage = screen.getByRole("application");

    fireEvent.keyDown(stage, { key: " " });
    fireEvent.keyUp(stage, { key: " " });
    fireEvent.keyDown(stage, { key: "ArrowLeft" });
    fireEvent.keyUp(stage, { key: "ArrowLeft" });

    await waitFor(() => {
      expect(sentActions()).toEqual([
        { kind: "key_down", text: "Space" },
        { kind: "key_up", text: "Space" },
        { kind: "key_down", text: "ArrowLeft" },
        { kind: "key_up", text: "ArrowLeft" },
      ]);
    });
  });

  it("按住的时候焦点移走了，把还按着的键和鼠标松开", async () => {
    answering({ ok: true, status: 200, body: { done: [], turns_in_flight: 0 } });
    render(<BrowserFrame identity={IDENTITY} />);
    await waitFor(() => screen.getByAltText("浏览器当前画面"));
    const stage = screen.getByRole("application");

    fireEvent.keyDown(stage, { key: "ArrowRight" });
    fireEvent.mouseDown(stage, { button: 0, clientX: 7, clientY: 9 });
    // 抬起发生在这块面之外，它听不到。
    fireEvent.blur(stage);

    await waitFor(() => {
      expect(sentActions()).toEqual([
        { kind: "key_down", text: "ArrowRight" },
        { kind: "mouse_down", x: 7, y: 9 },
        { kind: "key_up", text: "ArrowRight" },
        { kind: "mouse_up", x: 7, y: 9 },
      ]);
    });
  });

  it("模型这一轮也在动这个页面时，输入照样送进去，只是说一声", async () => {
    // ADR-0119：这条测试的上上一版断言的是 409 和「等这一轮结束再点」。一个
    // 「写完页面再去浏览器里验」的回合要跑几分钟，而人想按方向键的正是那几
    // 分钟——所以那条规则每次该放行的时候都在拒绝。
    answering({
      ok: true,
      status: 200,
      body: { done: ["action 0 (key_down) ok"], turns_in_flight: 1 },
    });
    render(<BrowserFrame identity={IDENTITY} refusalMs={50} />);
    await waitFor(() => screen.getByAltText("浏览器当前画面"));
    const stage = screen.getByRole("application");

    fireEvent.keyDown(stage, { key: "ArrowRight" });

    await waitFor(() => {
      expect(screen.getByRole("status").textContent).toMatch(/送进去了/);
    });
    expect(sentActions()).toEqual([{ kind: "key_down", text: "ArrowRight" }]);
    // 说完自己退下去，和别的那几句一样。
    await waitFor(() => {
      expect(screen.getByRole("status").textContent).not.toMatch(/送进去了/);
    });
  });

  it("画面底下写着这一页在那台机器上的路径", async () => {
    // 读者问这块面的第一个问题是「这是哪个文件」。地址跟着帧一起来（ADR-0119），
    // 百分号编码在这一层解开——`windows测试` 是这件事被报上来的那个目录。
    fetchMock.mockImplementation(() =>
      Promise.resolve({
        ok: true,
        status: 200,
        headers: {
          get: (name: string): string | null =>
            name === "X-Browser-Url"
              ? "file:///projects/windows%E6%B5%8B%E8%AF%95/mario.html"
              : null,
        },
        blob: () => Promise.resolve(new Blob([new Uint8Array([0xff, 0xd8])])),
      }),
    );
    render(<BrowserFrame identity={IDENTITY} />);

    await waitFor(() => {
      expect(screen.getByText("/projects/windows测试/mario.html")).toBeTruthy();
    });
  });

  it("地址落在这段会话的项目目录里时，可以回到文件夹里打开它", async () => {
    // 文件夹和浏览器是一件事的两面（ADR-0120）：一头是右栏里 `.html` 的「在浏览器
    // 中打开」，这是另一头。
    fetchMock.mockImplementation(() =>
      Promise.resolve({
        ok: true,
        status: 200,
        headers: {
          get: (name: string): string | null =>
            name === "X-Browser-Url"
              ? "file:///projects/windows%E6%B5%8B%E8%AF%95/game/mario.html"
              : null,
        },
        blob: () => Promise.resolve(new Blob([new Uint8Array([0xff, 0xd8])])),
      }),
    );
    const onRevealFile = vi.fn<(path: string) => void>();
    render(
      <BrowserFrame
        identity={IDENTITY}
        onRevealFile={onRevealFile}
        projectRoot="/projects/windows测试"
      />,
    );

    fireEvent.click(await screen.findByRole("button", { name: "在文件夹中打开" }));

    expect(onRevealFile).toHaveBeenCalledWith("game/mario.html");
  });
});

describe("relativeToProject", () => {
  it("只认项目目录之下的文件，不认前缀相同的另一个目录，也不认根本身", () => {
    expect(relativeToProject("/projects/demo/a/b.html", "/projects/demo")).toBe(
      "a/b.html",
    );
    expect(relativeToProject("/projects/demo2/b.html", "/projects/demo")).toBeNull();
    expect(relativeToProject("/projects/demo/", "/projects/demo")).toBeNull();
    expect(relativeToProject("https://example.com/", "/projects/demo")).toBeNull();
    expect(relativeToProject("/projects/demo/a.html", null)).toBeNull();
  });
});
