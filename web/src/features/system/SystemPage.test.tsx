import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, checkHealth, getDeploymentCapabilities } from "../../api/client";
import type { DeploymentCapability } from "../../api/types";
import { IdentityProvider } from "../../app/IdentityContext";
import { SystemPage } from "./SystemPage";

vi.mock("../../api/client", async () => {
  const actual = await vi.importActual("../../api/client");
  return {
    ...actual,
    checkHealth: vi.fn(),
    getDeploymentCapabilities: vi.fn(),
  };
});

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
  },
  {
    id: "chat.knowledge_base",
    title: "知识库问答（RAG）",
    tier: "core",
    state: "absent",
    reason: "no embedding runtime is installed",
    remedy: "需要装了 embedding extra 的镜像。",
    detail: [],
  },
  {
    id: "task.worker",
    title: "任务 Worker",
    tier: "core",
    state: "unknown",
    reason: "本部署没有 Worker 上报通道。",
    remedy: "docker compose --profile demo ps",
    detail: [],
  },
  {
    id: "task.mcp_tools",
    title: "任务可用的 MCP 工具",
    tier: "optional",
    state: "available",
    reason: "",
    remedy: "",
    detail: ["word_render_document"],
  },
];

describe("SystemPage", () => {
  beforeEach(() => {
    localStorage.clear();
    vi.mocked(checkHealth).mockReset();
    vi.mocked(getDeploymentCapabilities).mockReset();
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
    expect(screen.getByText("缺失")).toBeInTheDocument();
    expect(screen.getAllByText("可用")).toHaveLength(2);
  });

  it("能调用的 MCP 工具按名字列出来", async () => {
    renderPage();

    expect(await screen.findByText("word_render_document")).toBeInTheDocument();
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
