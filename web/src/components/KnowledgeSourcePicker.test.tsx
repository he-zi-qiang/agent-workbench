import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { listKnowledgeBases } from "../api/client";
import type { PrincipalIdentity } from "../api/types";
import { KnowledgeSourcePicker } from "./KnowledgeSourcePicker";

vi.mock("../api/client", () => ({
  listKnowledgeBases: vi.fn(),
}));

const IDENTITY: PrincipalIdentity = {
  tenantId: "tenant_a",
  principalId: "alice",
  scopes: ["knowledge:read"],
};

describe("KnowledgeSourcePicker", () => {
  beforeEach(() => {
    vi.mocked(listKnowledgeBases).mockResolvedValue({
      knowledge_bases: [
        {
          knowledge_base_id: "kb_resume",
          name: "校招资料",
          description: null,
          can_write: true,
          document_count: 3,
          ready_document_count: 2,
          processing_document_count: 1,
          failed_document_count: 0,
          created_at: "2026-08-01T00:00:00Z",
          updated_at: "2026-08-02T00:00:00Z",
        },
      ],
    });
  });

  it("offers free conversation and real knowledge bases as peer choices", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter>
          <KnowledgeSourcePicker
            identity={IDENTITY}
            onChange={onChange}
            value={null}
          />
        </MemoryRouter>
      </QueryClientProvider>,
    );

    const select = screen.getByLabelText("回答资料");
    expect(screen.getByRole("option", { name: "不使用知识库 · 自由回答" })).toBeInTheDocument();
    expect(
      await screen.findByRole("option", { name: "校招资料 · 2/3 可用" }),
    ).toBeInTheDocument();

    await user.selectOptions(select, "kb_resume");
    expect(onChange).toHaveBeenCalledWith(
      expect.objectContaining({ knowledge_base_id: "kb_resume", name: "校招资料" }),
    );
  });
});
