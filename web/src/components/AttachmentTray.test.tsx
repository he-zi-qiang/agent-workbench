import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactElement } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  listKnowledgeBaseDocuments,
  listKnowledgeBases,
  uploadDocument,
} from "../api/client";
import type {
  DocumentVersion,
  KnowledgeBaseView,
  KnowledgeDocumentStatus,
  PrincipalIdentity,
} from "../api/types";
import {
  AttachmentButton,
  AttachmentTray,
  useKnowledgeAttachments,
} from "./AttachmentTray";

vi.mock("../api/client", () => ({
  listKnowledgeBaseDocuments: vi.fn(),
  listKnowledgeBases: vi.fn(),
  uploadDocument: vi.fn(),
}));

const IDENTITY: PrincipalIdentity = {
  tenantId: "tenant_a",
  principalId: "alice",
  scopes: [],
};

describe("knowledge attachments", () => {
  beforeEach(() => {
    vi.mocked(uploadDocument).mockReset();
    vi.mocked(listKnowledgeBaseDocuments).mockReset();
    vi.mocked(listKnowledgeBases).mockReset();
    vi.mocked(listKnowledgeBases).mockResolvedValue({
      knowledge_bases: [knowledgeBase("kb_resume"), knowledgeBase("kb_other")],
    });
    vi.mocked(uploadDocument).mockResolvedValue({
      schema_version: 1,
      version_id: "ver_1",
      document_id: "doc_1",
      source_revision: 1,
      artifact_id: "artifact_1",
      content_sha256: "a".repeat(64),
    });
    vi.mocked(listKnowledgeBaseDocuments).mockImplementation(() =>
      Promise.resolve({ documents: uploadedDocuments("ready") }),
    );
  });

  it("waits for a knowledge base, then uploads and waits for a real ready status", async () => {
    const view = renderTray(null);
    chooseFile(view.container);
    expect(await screen.findByText("请先选择知识库，才能上传")).toBeInTheDocument();
    expect(uploadDocument).not.toHaveBeenCalled();

    view.show("kb_resume");
    expect(await screen.findByText("已存入知识库（会一直保留）")).toBeInTheDocument();
    await waitFor(() => expect(uploadDocument).toHaveBeenCalledTimes(1));
    expect(listKnowledgeBaseDocuments).toHaveBeenCalledWith(IDENTITY, "kb_resume");
  });

  it("does not present an uploaded file as something it can take back", async () => {
    // The misreading this component used to invite: a paperclip and a "移除"
    // button say per-message attachment, while the file has in fact been
    // uploaded into a shared knowledge base and cannot be deleted from
    // anywhere in this system.
    const view = renderTray("kb_resume");
    chooseFile(view.container);

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

  it("reports a refused document instead of indexing it forever", async () => {
    // The document is in the knowledge base and ingestion has given up on it.
    // Every status that is not `ready` used to be read as "still working", so
    // this chip span at 正在建立索引 for as long as the page stayed open -- and
    // the composer it blocks would never unblock either.
    vi.mocked(listKnowledgeBaseDocuments).mockImplementation(() =>
      Promise.resolve({
        documents: uploadedDocuments("failed", "invalid_tool_input"),
      }),
    );
    const view = renderTray("kb_resume");
    chooseFile(view.container);

    expect(
      await screen.findByText("文件内容无法解析，换一个可读的文件再试"),
    ).toBeInTheDocument();
    expect(screen.queryByText("正在建立索引，完成后才能检索到")).toBeNull();
    // The control that already exists is still offered, and this is only
    // asserting that a refused chip is not left without one. It is worth being
    // exact about what it can do here, because it is less than it looks: the
    // upload path returns the existing version unchanged when the content hash
    // matches (`adapters/persistence/documents.py`), and a retry sends the same
    // File, so `source_revision` does not move and the refusal that is keyed to
    // it still stands. The chip's own sentence is the working instruction --
    // change the file -- and that is a different file, not this one again.
    expect(
      screen.getByRole("button", { name: "重试 resume.md" }),
    ).toBeInTheDocument();
    // Failed, and in the knowledge base regardless -- so the × must still say
    // what it can actually do.
    expect(
      screen.getByRole("button", {
        name: "从这个列表中移除 resume.md（文件仍在知识库中）",
      }),
    ).toBeInTheDocument();
  });

  it("names an unknown refusal code rather than inventing a cause for it", async () => {
    vi.mocked(listKnowledgeBaseDocuments).mockImplementation(() =>
      Promise.resolve({ documents: uploadedDocuments("failed", "policy_denied") }),
    );
    const view = renderTray("kb_resume");
    chooseFile(view.container);

    expect(
      await screen.findByText("索引失败（policy_denied），可以重试一次"),
    ).toBeInTheDocument();
  });

  it("aborts an upload still on the wire when the knowledge base changes", async () => {
    // The failure this closes is silent: the tray is emptied by the switch, the
    // promise keeps the *old* id, and the file finishes landing in a knowledge
    // base the reader has already navigated away from.
    const pending = neverResolvingUpload();
    const view = renderTray("kb_resume");
    chooseFile(view.container);
    await waitFor(() => expect(uploadDocument).toHaveBeenCalledTimes(1));
    expect(vi.mocked(uploadDocument).mock.calls[0]?.[1].knowledgeBaseId).toBe(
      "kb_resume",
    );
    expect(pending.signal()?.aborted).toBe(false);

    view.show("kb_other");

    await waitFor(() => expect(pending.signal()?.aborted).toBe(true));
    expect(
      await screen.findByText("已切换知识库，这次上传已取消"),
    ).toBeInTheDocument();
  });

  it("does not call off the upload that choosing a base just started", async () => {
    // The first selection is a change of knowledge base like any other, so the
    // abort fires on it too -- and the file queued while no base was chosen is
    // uploaded on that very change. What keeps the two apart today is only that
    // the queued upload starts from a deferred callback, after the abort has
    // already run against an empty set. Nothing states that dependency where
    // either side can see it, and every other test here would stay green if it
    // broke: their upload double ignores the signal it is handed, so a file
    // aborted the instant it left still reaches 已存入知识库 on screen. The
    // failure would be a chip reading 已切换知识库，这次上传已取消 about the base
    // the reader had just picked.
    const pending = neverResolvingUpload();
    const view = renderTray(null);
    chooseFile(view.container);

    view.show("kb_resume");

    await waitFor(() => expect(uploadDocument).toHaveBeenCalledTimes(1));
    // Past the point where an abort raised by the same change would have landed.
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(pending.signal()?.aborted).toBe(false);
  });

  it("aborts an upload still on the wire when the tray is cleared", async () => {
    // The route both pages actually take when the reader switches sources.
    const pending = neverResolvingUpload();
    const view = renderTray("kb_resume");
    chooseFile(view.container);
    await waitFor(() => expect(uploadDocument).toHaveBeenCalledTimes(1));

    fireEvent.click(screen.getByRole("button", { name: "清空" }));

    await waitFor(() => expect(pending.signal()?.aborted).toBe(true));
  });

  it("refuses to offer an upload into a read-only knowledge base", async () => {
    // `can_write` is already on the wire and already read by the knowledge base
    // page. Without it here, the reader picks a file, watches it upload, and is
    // told no by the server at the end of it.
    vi.mocked(listKnowledgeBases).mockResolvedValue({
      knowledge_bases: [knowledgeBase("kb_shared", { can_write: false })],
    });
    const view = renderTray("kb_shared");

    const button = await screen.findByRole("button", {
      name: "无法上传文件：这个知识库对你是只读的，不能上传文件",
    });
    expect(button).toBeDisabled();

    chooseFile(view.container);
    await waitFor(() => expect(screen.queryByText("resume.md")).toBeNull());
    expect(uploadDocument).not.toHaveBeenCalled();
  });
});

function knowledgeBase(
  id: string,
  overrides: Partial<KnowledgeBaseView> = {},
): KnowledgeBaseView {
  return {
    knowledge_base_id: id,
    name: id,
    description: null,
    can_write: true,
    document_count: 0,
    ready_document_count: 0,
    processing_document_count: 0,
    failed_document_count: 0,
    created_at: "2026-08-02T00:00:00Z",
    updated_at: "2026-08-02T00:00:01Z",
    ...overrides,
  };
}

/** The list endpoint's answer about whatever the last upload created. */
function uploadedDocuments(
  status: KnowledgeDocumentStatus,
  failureCode: string | null = null,
) {
  const input = vi.mocked(uploadDocument).mock.calls[0]?.[1];
  if (input === undefined) return [];
  return [
    {
      document_id: input.documentId,
      filename: input.file.name,
      media_type: input.file.type,
      size_bytes: input.file.size,
      source_revision: 1,
      last_applied_revision: status === "ready" ? 1 : 0,
      status,
      failure_code: failureCode,
      created_at: "2026-08-02T00:00:00Z",
      updated_at: "2026-08-02T00:00:01Z",
    },
  ];
}

/**
 * An upload that never finishes on its own, so the only way out is the abort.
 *
 * It rejects when the signal fires, because that is what `fetch` does; a mock
 * that ignored the signal would let a hook that never passes one still pass.
 * The rejection *value* is deliberately not the point -- the hook reads the
 * signal, not what came back with the rejection.
 */
function neverResolvingUpload() {
  let captured: AbortSignal | undefined;
  vi.mocked(uploadDocument).mockImplementation((_identity, _input, signal) => {
    captured = signal;
    return new Promise<DocumentVersion>((_resolve, reject) => {
      signal?.addEventListener("abort", () =>
        reject(new DOMException(String(signal.reason), "AbortError")),
      );
    });
  });
  return { signal: () => captured };
}

function chooseFile(container: HTMLElement, name = "resume.md") {
  const input = container.querySelector<HTMLInputElement>('input[type="file"]');
  if (input === null) throw new Error("attachment input missing");
  fireEvent.change(input, {
    target: { files: [new File(["# resume"], name, { type: "text/markdown" })] },
  });
}

function renderTray(knowledgeBaseId: string | null) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  const ui = (id: string | null): ReactElement => (
    <QueryClientProvider client={queryClient}>
      <Harness knowledgeBaseId={id} />
    </QueryClientProvider>
  );
  const view = render(ui(knowledgeBaseId));
  return { ...view, show: (id: string | null) => view.rerender(ui(id)) };
}

function Harness({ knowledgeBaseId }: { knowledgeBaseId: string | null }) {
  const attachments = useKnowledgeAttachments(IDENTITY, knowledgeBaseId);
  return (
    <div>
      <AttachmentButton
        disabled={attachments.readOnlyReason !== null}
        {...(attachments.readOnlyReason === null
          ? {}
          : { disabledReason: attachments.readOnlyReason })}
        onFiles={attachments.addFiles}
      />
      {/* Stands in for the `clear()` both pages call before they switch. */}
      <button onClick={attachments.clear} type="button">
        清空
      </button>
      <AttachmentTray
        items={attachments.items}
        onRemove={attachments.remove}
        onRetry={attachments.retry}
      />
    </div>
  );
}
