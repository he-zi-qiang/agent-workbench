import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  ApiError,
  checkHealth,
  getDeploymentCapabilities,
  setDeploymentSwitch,
} from "../../api/client";
import type { DeploymentCapability, DeploymentSwitch } from "../../api/types";
import { IdentityProvider } from "../../app/IdentityContext";
import { SystemPage } from "./SystemPage";

vi.mock("../../api/client", async () => {
  const actual = await vi.importActual("../../api/client");
  return {
    ...actual,
    checkHealth: vi.fn(),
    getDeploymentCapabilities: vi.fn(),
    setDeploymentSwitch: vi.fn(),
  };
});

/** 一个还没人拨过的开关：这次关着，下次照启动环境走，什么也不欠。 */
const UNDECIDED: DeploymentSwitch = {
  id: "multi_agent.delegation_enabled",
  stored: null,
  active: false,
  restart_required: false,
  restart_hint: "",
  held: "",
  overridden: false,
  needs_model: false,
  blocked: "",
};

/** 这台部署缺了三样东西，三样各缺得不一样——这正是这一页要能说清的。 */
const CAPABILITIES: DeploymentCapability[] = [
  {
    id: "chat.direct",
    title: "直接对话",
    tier: "core",
    state: "available",
    reason: "",
    remedy: "",
    detail: [],
    provision: "key",
    switch: null,
  },
  {
    id: "chat.knowledge_base",
    title: "知识库问答（RAG）",
    tier: "core",
    state: "absent",
    reason: "no embedding runtime is installed",
    remedy: "需要装了 embedding extra 的镜像。",
    detail: [],
    provision: "install",
    switch: null,
  },
  {
    id: "task.worker",
    title: "任务 Worker",
    tier: "core",
    state: "unknown",
    reason: "本部署没有 Worker 上报通道。",
    remedy: "docker compose --profile demo ps",
    detail: [],
    provision: "none",
    switch: null,
  },
  {
    id: "task.mcp_tools",
    title: "任务可用的 MCP 工具",
    tier: "optional",
    state: "available",
    reason: "",
    remedy: "",
    detail: ["word_render_document"],
    provision: "install",
    switch: null,
  },
  {
    id: "task.delegation",
    title: "子代理委派",
    tier: "optional",
    state: "absent",
    reason: "multi_agent.delegation_enabled 为假。",
    remedy: "打开这一行的开关，然后重启 API 与 Worker。",
    detail: [],
    provision: "switch",
    switch: UNDECIDED,
  },
];

function withSwitch(patch: Partial<DeploymentSwitch>): DeploymentCapability[] {
  return CAPABILITIES.map((row) =>
    row.id === "task.delegation" ? { ...row, switch: { ...UNDECIDED, ...patch } } : row,
  );
}

describe("SystemPage", () => {
  beforeEach(() => {
    localStorage.clear();
    vi.mocked(checkHealth).mockReset();
    vi.mocked(getDeploymentCapabilities).mockReset();
    vi.mocked(setDeploymentSwitch).mockReset();
    vi.mocked(getDeploymentCapabilities).mockResolvedValue({
      capabilities: CAPABILITIES,
    });
    vi.mocked(checkHealth).mockImplementation((path) =>
      Promise.resolve(
        path === "/health/live"
          ? { ok: true, status: "live" }
          : { ok: true, status: "ready" },
      ),
    );
  });

  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  it("translates the two probes narrowly and leaves worker status unknown", async () => {
    const { container } = renderPage();

    expect(await screen.findByText("可响应")).toBeInTheDocument();
    expect(screen.getByText("已就绪")).toBeInTheDocument();
    expect(screen.getByText("状态未知")).toBeInTheDocument();
    // 这句话里不再有「模型」，也不再说「现有公开接口无法验证这些状态」——从
    // ADR-102 起有一个接口能验证其中一半，而一条已经不成立的免责声明比没有更糟。
    expect(
      screen.getByText(/数据库已就绪不代表 Qdrant 与两个 Worker 都正常/),
    ).toHaveTextContent("从这里看不出来");
    // The fold is gone. It held two sections that both restated something
    // already on the page -- the identity block duplicates the dialog the
    // button below opens, and "why are some states unknown" restated the
    // warning directly above it in three bullets.
    expect(container.querySelector("details")).toBeNull();
    expect(
      screen.getByRole("button", { name: "编辑本地身份" }),
    ).toBeInTheDocument();
  });

  it("每一处缺失都带着它的原因和补法，而不是一个红点", async () => {
    renderPage();

    expect(await screen.findByText("知识库问答（RAG）")).toBeInTheDocument();
    expect(
      screen.getByText("no embedding runtime is installed"),
    ).toBeInTheDocument();
    expect(
      screen.getByText("要补上它：需要装了 embedding extra 的镜像。"),
    ).toBeInTheDocument();
  });

  it("Worker 是「未知」，不是「缺失」", async () => {
    renderPage();

    // 三个状态各出现一次：可用、缺失、未知。把未知画成缺失，就是替另一个进程
    // 作了它没作过的证。
    expect(await screen.findByText("任务 Worker")).toBeInTheDocument();
    expect(screen.getByText("未知")).toBeInTheDocument();
    // 两处缺失：知识库（核心）和委派（附加）。
    expect(screen.getAllByText("缺失")).toHaveLength(2);
    expect(screen.getAllByText("可用")).toHaveLength(2);
  });

  it("能调用的 MCP 工具按名字列出来", async () => {
    renderPage();

    expect(await screen.findByText("word_render_document")).toBeInTheDocument();
  });

  it("安装型的零件标「需要安装」，开关型的零件带一个三位开关", async () => {
    renderPage();

    expect(await screen.findByText("需要安装")).toBeInTheDocument();
    const group = screen.getByRole("radiogroup", { name: "子代理委派：下次启动" });
    expect(group).toBeInTheDocument();
    // 三个位置：「不指定」是一个真实的状态，不是「关」的别名。
    expect(screen.getByRole("radio", { name: "不指定" })).toHaveAttribute(
      "aria-checked",
      "true",
    );
    expect(screen.getByText("这次启动：关 · 下次启动：按启动环境与配置")).toBeInTheDocument();
    // 可用的 MCP 那一行也是安装型，但它可用，所以不标——标了只会让人去找不存在的活。
    expect(screen.getAllByText("需要安装")).toHaveLength(1);
  });

  it("拨一下开关：写的是下次启动，页面用服务端回的整份清单替换自己", async () => {
    const user = userEvent.setup();
    vi.mocked(setDeploymentSwitch).mockResolvedValue({
      capabilities: withSwitch({
        stored: true,
        restart_required: true,
        restart_hint: "重启 agent-api 与 agent-task-worker 后这个选择才会生效。",
      }),
    });
    renderPage();

    await user.click(await screen.findByRole("radio", { name: "打开" }));

    expect(setDeploymentSwitch).toHaveBeenCalledWith(
      expect.anything(),
      "multi_agent.delegation_enabled",
      true,
    );
    await waitFor(() =>
      expect(screen.getByRole("radio", { name: "打开" })).toHaveAttribute(
        "aria-checked",
        "true",
      ),
    );
    // 两个问题，两个答案：下次开，这次还是关，并且说清要重启。
    expect(screen.getByText("这次启动：关 · 下次启动：开")).toBeInTheDocument();
    expect(screen.getByText(/重启 agent-api/)).toBeInTheDocument();
    // 没有再去 GET：服务端回的就是整份清单。
    expect(getDeploymentCapabilities).toHaveBeenCalledTimes(1);
  });

  it("「不指定」是收回选择，走的是 null", async () => {
    const user = userEvent.setup();
    vi.mocked(getDeploymentCapabilities).mockResolvedValue({
      capabilities: withSwitch({ stored: false }),
    });
    vi.mocked(setDeploymentSwitch).mockResolvedValue({
      capabilities: CAPABILITIES,
    });
    renderPage();

    await user.click(await screen.findByRole("radio", { name: "不指定" }));

    expect(setDeploymentSwitch).toHaveBeenCalledWith(
      expect.anything(),
      "multi_agent.delegation_enabled",
      null,
    );
  });

  it("被环境变量压过的开关说出来，而不是装作能改", async () => {
    vi.mocked(getDeploymentCapabilities).mockResolvedValue({
      capabilities: withSwitch({ stored: true, active: false, overridden: true }),
    });
    renderPage();

    expect(await screen.findByText(/压过了这里的选择/)).toBeInTheDocument();
    expect(screen.getByText("这次启动：关 · 下次启动：开")).toBeInTheDocument();
  });

  it("被搁置的开关把搁置的原因写在行上", async () => {
    vi.mocked(getDeploymentCapabilities).mockResolvedValue({
      capabilities: withSwitch({
        stored: true,
        held: "这次启动没有可用的 Provider Key，所以这个开关被搁置。",
      }),
    });
    renderPage();

    expect(await screen.findByText(/被搁置/)).toBeInTheDocument();
  });

  it("「重新检查」也重读能力清单，因为按它的人往往刚重启过 API", async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByText("子代理委派");
    expect(getDeploymentCapabilities).toHaveBeenCalledTimes(1);

    await user.click(screen.getByRole("button", { name: "重新检查" }));

    await waitFor(() => expect(getDeploymentCapabilities).toHaveBeenCalledTimes(2));
  });

  it("旧 API 的 404 说的是「这个 API 比控制台旧」，不是读取失败", async () => {
    vi.mocked(getDeploymentCapabilities).mockRejectedValue(
      new ApiError(404, "Not Found"),
    );

    renderPage();

    expect(
      await screen.findByText(/这个 API 进程还没有能力清单接口/),
    ).toBeInTheDocument();
  });
});

function renderPage() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <IdentityProvider>
        <SystemPage />
      </IdentityProvider>
    </QueryClientProvider>,
  );
}
