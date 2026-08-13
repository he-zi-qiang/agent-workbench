import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  listKnowledgeBaseDocuments,
  listKnowledgeBases,
  searchKnowledge,
  uploadDocument,
} from "../../api/client";
import type {
  DocumentVersion,
  KnowledgeBaseView,
  KnowledgeDocumentView,
  SearchResponse,
} from "../../api/types";
import { IdentityProvider } from "../../app/IdentityContext";
import { KnowledgePage } from "./KnowledgePage";

vi.mock("../../api/client", async () => {
  const actual = await vi.importActual("../../api/client");
  return {
    ...actual,
    listKnowledgeBaseDocuments: vi.fn(),
    listKnowledgeBases: vi.fn(),
    searchKnowledge: vi.fn(),
    uploadDocument: vi.fn(),
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

function documentVersion(): DocumentVersion {
  return {
    schema_version: 1,
    version_id: "ver_1",
    document_id: "doc_1",
    source_revision: 1,
    artifact_id: "art_1",
    content_sha256: "0".repeat(64),
  };
}

function searchResponse(overrides: Partial<SearchResponse> = {}): SearchResponse {
  return {
    hits: [
      {
        chunk_id: "chunk_1",
        document_id: "doc_1",
        document_version: "ver_1",
        text: "索引流水线由文档 Worker 异步执行。",
      },
    ],
    citations: [],
    retriever: "hybrid+rerank",
    ...overrides,
  };
}

function chooseFile(container: HTMLElement) {
  const input = container.querySelector<HTMLInputElement>('input[type="file"]');
  if (input === null) throw new Error("upload input missing");
  fireEvent.change(input, {
    target: {
      files: [new File(["# 简历"], "resume.md", { type: "text/markdown" })],
    },
  });
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
      within(row).getByText(/文件内容无法解析，请换成可读的 PDF、Word 或 Markdown 重新上传。/),
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

describe("KnowledgePage 上传授权", () => {
  beforeEach(() => {
    localStorage.clear();
    vi.clearAllMocks();
    vi.mocked(listKnowledgeBases).mockResolvedValue({
      knowledge_bases: [knowledgeBase()],
    });
    vi.mocked(listKnowledgeBaseDocuments).mockResolvedValue({ documents: [] });
    vi.mocked(uploadDocument).mockResolvedValue(documentVersion());
  });

  it("把填写的 principal 交给上传，逗号、全角逗号和空格都算分隔", async () => {
    const view = renderPage();
    expect(await screen.findByText("添加文档")).toBeInTheDocument();

    const grantField = screen.getByLabelText("同时授权给（可选）");
    fireEvent.change(grantField, {
      target: { value: " u_alice, u_bob，u_carol u_dora, " },
    });
    chooseFile(view.container);
    fireEvent.click(screen.getByRole("button", { name: /上传并开始索引/ }));

    // 数组是整体比对的，所以这条同时盯着尾逗号：空项要是混进去就红了。服务端的
    // principal id 不接受空串，带着它去 /complete 换回的是 422，而那时文件的
    // 字节已经传完了。
    await waitFor(() =>
      expect(uploadDocument).toHaveBeenCalledWith(
        expect.any(Object),
        expect.objectContaining({
          grantedPrincipals: ["u_alice", "u_bob", "u_carol", "u_dora"],
        }),
      ),
    );
    // 名单是写给刚上传那一份文档的，留在框里下一份会默默继承。
    await waitFor(() => expect(grantField).toHaveValue(""));
  });

  it("没填的时候送出空名单", async () => {
    // 对照组。上一条只证明「填了会送到」；少了这条，一个把整串原样塞进去、
    // 或者压根不看输入框的实现照样能让上一条过。
    const view = renderPage();
    expect(await screen.findByText("添加文档")).toBeInTheDocument();

    chooseFile(view.container);
    fireEvent.click(screen.getByRole("button", { name: /上传并开始索引/ }));

    await waitFor(() =>
      expect(uploadDocument).toHaveBeenCalledWith(
        expect.any(Object),
        expect.objectContaining({ grantedPrincipals: [] }),
      ),
    );
  });
});

describe("KnowledgePage 检索调试", () => {
  beforeEach(() => {
    localStorage.clear();
    vi.clearAllMocks();
    vi.mocked(listKnowledgeBases).mockResolvedValue({
      knowledge_bases: [knowledgeBase()],
    });
    vi.mocked(listKnowledgeBaseDocuments).mockResolvedValue({ documents: [] });
  });

  async function runSearch() {
    expect(await screen.findByText("添加文档")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("检索测试问题"), {
      target: { value: "索引是怎么跑的" },
    });
    fireEvent.click(screen.getByRole("button", { name: /测试检索/ }));
  }

  it("写出应答的是哪条检索栈", async () => {
    vi.mocked(searchKnowledge).mockResolvedValue(searchResponse());

    renderPage();
    await runSearch();

    expect(
      await screen.findByText("由 hybrid+rerank 检索栈应答"),
    ).toBeInTheDocument();
  });

  it("换一条检索栈，显示的就跟着换", async () => {
    // 对照组。只断言一次 hybrid+rerank 的话，把这几个字写死在页面上也能过；
    // 这条要求这行字真的来自响应。
    vi.mocked(searchKnowledge).mockResolvedValue(
      searchResponse({ retriever: "dense" }),
    );

    renderPage();
    await runSearch();

    expect(await screen.findByText("由 dense 检索栈应答")).toBeInTheDocument();
    expect(screen.queryByText(/rerank/)).toBeNull();
  });

  it("一条都没命中时也说明是哪条检索栈应答", async () => {
    vi.mocked(searchKnowledge).mockResolvedValue(
      searchResponse({ hits: [], retriever: "dense" }),
    );

    renderPage();
    await runSearch();

    expect(await screen.findByText("由 dense 检索栈应答")).toBeInTheDocument();
    expect(screen.getByText("本次没有返回可读的匹配片段。")).toBeInTheDocument();
  });
});
