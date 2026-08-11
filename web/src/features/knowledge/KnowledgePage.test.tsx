import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  listKnowledgeBaseDocuments,
  listKnowledgeBases,
} from "../../api/client";
import type {
  KnowledgeBaseView,
  KnowledgeDocumentView,
} from "../../api/types";
import { IdentityProvider } from "../../app/IdentityContext";
import { KnowledgePage } from "./KnowledgePage";

vi.mock("../../api/client", async () => {
  const actual = await vi.importActual("../../api/client");
  return {
    ...actual,
    listKnowledgeBaseDocuments: vi.fn(),
    listKnowledgeBases: vi.fn(),
  };
});

function knowledgeBase(
  overrides: Partial<KnowledgeBaseView> = {},
): KnowledgeBaseView {
  return {
    knowledge_base_id: "kb_resume",
    name: "校招资料",
    description: "项目与面试资料",
    can_write: true,
    document_count: 0,
    ready_document_count: 0,
    processing_document_count: 0,
    failed_document_count: 0,
    created_at: "2026-08-01T00:00:00Z",
    updated_at: "2026-08-02T00:00:00Z",
    ...overrides,
  };
}

function document(
  overrides: Partial<KnowledgeDocumentView> = {},
): KnowledgeDocumentView {
  return {
    document_id: "doc_1",
    filename: "handbook.pdf",
    media_type: "application/pdf",
    size_bytes: 2048,
    source_revision: 1,
    last_applied_revision: 0,
    status: "processing",
    failure_code: null,
    created_at: "2026-08-02T00:00:00Z",
    updated_at: "2026-08-02T00:00:01Z",
    ...overrides,
  };
}

function renderPage(entry = "/knowledge") {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <IdentityProvider>
        <MemoryRouter initialEntries={[entry]}>
          <KnowledgePage />
        </MemoryRouter>
      </IdentityProvider>
    </QueryClientProvider>,
  );
}

describe("KnowledgePage selection", () => {
  beforeEach(() => {
    localStorage.clear();
    vi.mocked(listKnowledgeBases).mockResolvedValue({
      knowledge_bases: [knowledgeBase()],
    });
    vi.mocked(listKnowledgeBaseDocuments).mockResolvedValue({ documents: [] });
  });

  it("falls back to the first readable knowledge base for a stale URL", async () => {
    renderPage("/knowledge?kb=kb_deleted");

    expect(await screen.findByRole("heading", { name: "校招资料" })).toBeInTheDocument();
    await waitFor(() =>
      expect(listKnowledgeBaseDocuments).toHaveBeenCalledWith(
        expect.any(Object),
        "kb_resume",
        expect.any(AbortSignal),
      ),
    );
  });
});

describe("KnowledgePage 写权限", () => {
  beforeEach(() => {
    localStorage.clear();
    vi.mocked(listKnowledgeBaseDocuments).mockResolvedValue({ documents: [] });
  });

  it("对只读知识库不显示上传入口，并说明为什么", async () => {
    vi.mocked(listKnowledgeBases).mockResolvedValue({
      knowledge_bases: [knowledgeBase({ can_write: false })],
    });

    const view = renderPage();

    expect(await screen.findByText("只读知识库")).toBeInTheDocument();
    // 入口整块消失，不是渲染成禁用按钮：这里要挡住的是「把整份文件传完才吃
    // 404」，一个还能选中文件的控件挡不住那件事。
    expect(screen.queryByRole("button", { name: /上传并开始索引/ })).toBeNull();
    expect(view.container.querySelector('input[type="file"]')).toBeNull();
  });

  it("对自己的知识库照常显示上传入口", async () => {
    // 对照组。少了它，上面那条在「上传入口从来就没渲染过」时同样会通过。
    vi.mocked(listKnowledgeBases).mockResolvedValue({
      knowledge_bases: [knowledgeBase({ can_write: true })],
    });

    const view = renderPage();

    expect(await screen.findByText("添加文档")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /上传并开始索引/ }),
    ).toBeInTheDocument();
    expect(view.container.querySelector('input[type="file"]')).not.toBeNull();
    expect(screen.queryByText("只读知识库")).toBeNull();
  });
});

describe("KnowledgePage 摄取失败", () => {
  beforeEach(() => {
    localStorage.clear();
  });

  it("解析失败的文档显示索引失败而不是正在索引", async () => {
    vi.mocked(listKnowledgeBases).mockResolvedValue({
      knowledge_bases: [
        knowledgeBase({ document_count: 1, failed_document_count: 1 }),
      ],
    });
    vi.mocked(listKnowledgeBaseDocuments).mockResolvedValue({
      documents: [
        document({ status: "failed", failure_code: "invalid_tool_input" }),
      ],
    });

    renderPage();

    // 在文档行里找，不在整页里找：概览那一栏也有「索引失败」四个字，而它在
    // 文档列表还没回来时就已经渲染了——用整页断言会在列表还是 loading 的时候
    // 就通过，正好放过这条测试要盯的那块界面。
    const row = await screen.findByRole("article");
    expect(within(row).getByText("索引失败")).toBeInTheDocument();
    expect(
      within(row).getByText(/文件内容无法解析，请换成可读的 PDF 或 Markdown 重新上传。/),
    ).toBeInTheDocument();
    expect(screen.queryByText("正在索引")).toBeNull();
  });

  it("还在处理的文档仍然显示正在索引", async () => {
    // 对照组：上面那条断言的是「不再是正在索引」，所以必须证明「正在索引」
    // 这句话本身还活着，否则删掉它也能让上面通过。
    vi.mocked(listKnowledgeBases).mockResolvedValue({
      knowledge_bases: [
        knowledgeBase({ document_count: 1, processing_document_count: 1 }),
      ],
    });
    vi.mocked(listKnowledgeBaseDocuments).mockResolvedValue({
      documents: [document({ status: "processing" })],
    });

    renderPage();

    const row = await screen.findByRole("article");
    expect(within(row).getByText("正在索引")).toBeInTheDocument();
    expect(within(row).queryByText("索引失败")).toBeNull();
  });
});
