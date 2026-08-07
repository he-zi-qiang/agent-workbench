import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  listKnowledgeBaseDocuments,
  uploadDocument,
} from "../api/client";
import type { PrincipalIdentity } from "../api/types";
import {
  AttachmentButton,
  AttachmentTray,
  useKnowledgeAttachments,
} from "./AttachmentTray";

vi.mock("../api/client", () => ({
  listKnowledgeBaseDocuments: vi.fn(),
  uploadDocument: vi.fn(),
}));

const IDENTITY: PrincipalIdentity = {
  tenantId: "tenant_a",
  principalId: "alice",
  scopes: [],
};

describe("knowledge attachments", () => {
  beforeEach(() => {
    vi.mocked(uploadDocument).mockResolvedValue({
      schema_version: 1,
      version_id: "ver_1",
      document_id: "doc_1",
      source_revision: 1,
      artifact_id: "artifact_1",
      content_sha256: "a".repeat(64),
    });
    vi.mocked(listKnowledgeBaseDocuments).mockImplementation(() => {
      const input = vi.mocked(uploadDocument).mock.calls[0]?.[1];
      return Promise.resolve({
        documents: input === undefined
          ? []
          : [
              {
                document_id: input.documentId,
                filename: input.file.name,
                media_type: input.file.type,
                size_bytes: input.file.size,
                source_revision: 1,
                last_applied_revision: 1,
                status: "ready" as const,
                created_at: "2026-08-02T00:00:00Z",
                updated_at: "2026-08-02T00:00:01Z",
              },
            ],
      });
    });
  });

  it("waits for a knowledge base, then uploads and waits for a real ready status", async () => {
    const view = render(<Harness knowledgeBaseId={null} />);
    const input = view.container.querySelector<HTMLInputElement>('input[type="file"]');
    if (input === null) throw new Error("attachment input missing");
    const file = new File(["# resume"], "resume.md", { type: "text/markdown" });

    fireEvent.change(input, { target: { files: [file] } });
    expect(await screen.findByText("请选择知识库后上传")).toBeInTheDocument();
    expect(uploadDocument).not.toHaveBeenCalled();

    view.rerender(<Harness knowledgeBaseId="kb_resume" />);
    expect(await screen.findByText("已加入知识库，可以使用")).toBeInTheDocument();
    await waitFor(() => expect(uploadDocument).toHaveBeenCalledTimes(1));
    expect(listKnowledgeBaseDocuments).toHaveBeenCalledWith(
      IDENTITY,
      "kb_resume",
    );
  });
});

function Harness({ knowledgeBaseId }: { knowledgeBaseId: string | null }) {
  const attachments = useKnowledgeAttachments(IDENTITY, knowledgeBaseId);
  return (
    <div>
      <AttachmentButton onFiles={attachments.addFiles} />
      <AttachmentTray
        items={attachments.items}
        onRemove={attachments.remove}
        onRetry={attachments.retry}
      />
    </div>
  );
}
