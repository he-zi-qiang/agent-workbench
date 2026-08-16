import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { checkHealth } from "../../api/client";
import { IdentityProvider } from "../../app/IdentityContext";
import { SystemPage } from "./SystemPage";

vi.mock("../../api/client", async () => {
  const actual = await vi.importActual("../../api/client");
  return {
    ...actual,
    checkHealth: vi.fn(),
  };
});

describe("SystemPage", () => {
  beforeEach(() => {
    localStorage.clear();
    vi.mocked(checkHealth).mockReset();
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
    expect(
      screen.getByText(/数据库已就绪不代表模型、Qdrant、Task Worker/),
    ).toHaveTextContent("现有公开接口无法验证这些状态");
    // The fold is gone. It held two sections that both restated something
    // already on the page -- the identity block duplicates the dialog the
    // button below opens, and "why are some states unknown" restated the
    // warning directly above it in three bullets.
    expect(container.querySelector("details")).toBeNull();
    expect(
      screen.getByRole("button", { name: "编辑本地身份" }),
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
