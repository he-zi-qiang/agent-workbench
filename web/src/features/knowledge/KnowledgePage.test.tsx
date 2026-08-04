import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  listKnowledgeBaseDocuments,
  listKnowledgeBases,
} from "../../api/client";
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

describe("KnowledgePage selection", () => {
  beforeEach(() => {
    localStorage.clear();
    vi.mocked(listKnowledgeBases).mockResolvedValue({
      knowledge_bases: [
        {
          knowledge_base_id: "kb_resume",
          name: "校招资料",
          description: "项目与面试资料",
          document_count: 0,
          ready_document_count: 0,
          processing_document_count: 0,
          created_at: "2026-08-01T00:00:00Z",
          updated_at: "2026-08-02T00:00:00Z",
        },
      ],
    });
    vi.mocked(listKnowledgeBaseDocuments).mockResolvedValue({ documents: [] });
  });

  it("falls back to the first readable knowledge base for a stale URL", async () => {
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    render(
      <QueryClientProvider client={queryClient}>
        <IdentityProvider>
          <MemoryRouter initialEntries={["/knowledge?kb=kb_deleted"]}>
            <KnowledgePage />
          </MemoryRouter>
        </IdentityProvider>
      </QueryClientProvider>,
    );

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
