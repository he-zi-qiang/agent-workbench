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
    expect(await screen.findByText("请先选择知识库，才能上传")).toBeInTheDocument();
    expect(uploadDocument).not.toHaveBeenCalled();

    view.rerender(<Harness knowledgeBaseId="kb_resume" />);
    expect(await screen.findByText("已存入知识库（会一直保留）")).toBeInTheDocument();
    await waitFor(() => expect(uploadDocument).toHaveBeenCalledTimes(1));
    expect(listKnowledgeBaseDocuments).toHaveBeenCalledWith(
      IDENTITY,
      "kb_resume",
    );
  });

  it("does not present an uploaded file as something it can take back", async () => {
    // The misreading this component used to invite: a paperclip and a "移除"
    // button say per-message attachment, while the file has in fact been
    // uploaded into a shared knowledge base and cannot be deleted from
    // anywhere in this system.
    const view = render(<Harness knowledgeBaseId="kb_resume" />);
    const input = view.container.querySelector<HTMLInputElement>('input[type="file"]');
    if (input === null) throw new Error("attachment input missing");

    fireEvent.change(input, {
      target: {
        files: [new File(["# resume"], "resume.md", { type: "text/markdown" })],
      },
    });

    // Asserted from the moment the bytes have left, not only once indexing
    // finishes: the file is in the knowledge base for good either way, and
    // that is exactly the window in which the old copy was most misleading.
    expect(
      await screen.findByText(/只是不再列出它，不会删除已上传的文档/),
    ).toBeInTheDocument();
    // The control says what it does, rather than "移除".
    expect(
      screen.getByRole("button", {
        name: "从这个列表中移除 resume.md（文件仍在知识库中）",
      }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "移除 resume.md" })).toBeNull();
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
