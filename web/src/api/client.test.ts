import { afterEach, describe, expect, it, vi } from "vitest";
import {
  apiRequest,
  askChat,
  createTask,
  declaredMediaType,
  downloadArtifact,
  identityHeaders,
  uploadDocument,
} from "./client";
import type { ArtifactDownloadTarget, PrincipalIdentity } from "./types";

const identity: PrincipalIdentity = {
  tenantId: "tenant_a",
  principalId: "principal_a",
  scopes: [],
};

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("identity headers", () => {
  it("omits an empty scopes header", () => {
    expect(identityHeaders(identity)).toEqual({
      "x-tenant-id": "tenant_a",
      "x-principal-id": "principal_a",
    });
  });

  it("serializes non-empty scopes", () => {
    expect(identityHeaders({ ...identity, scopes: ["artifact:export", "admin"] })).toEqual({
      "x-tenant-id": "tenant_a",
      "x-principal-id": "principal_a",
      "x-principal-scopes": "artifact:export,admin",
    });
  });
});

describe("apiRequest", () => {
  it("sends the development identity on protected requests", async () => {
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValue(new Response(JSON.stringify({ ok: true }), { status: 200 }));

    await apiRequest(identity, "/v1/example");

    const init = fetchMock.mock.calls[0]?.[1];
    expect(new Headers(init?.headers).get("x-tenant-id")).toBe("tenant_a");
    expect(new Headers(init?.headers).get("x-principal-id")).toBe("principal_a");
    expect(new Headers(init?.headers).has("x-principal-scopes")).toBe(false);
  });

  it("sends an explicit direct source without a knowledge base", async () => {
    const response = {
      answer: "Direct answer",
      citations: [],
      withheld: false,
      grounded: false,
      run_id: "run_1",
      turn_id: "turn_1",
    };
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValue(new Response(JSON.stringify(response), { status: 200 }));

    await askChat(
      identity,
      "ses_1",
      {
        question: "hello",
        answerMode: "direct",
        knowledgeBaseId: null,
      },
      "chat:direct",
    );

    const init = fetchMock.mock.calls[0]?.[1];
    if (typeof init?.body !== "string") throw new Error("request body was not JSON");
    expect(JSON.parse(init.body)).toMatchObject({
      answer_mode: "direct",
      knowledge_base_id: null,
      question: "hello",
    });
  });

  it("lets a task retry reuse the same idempotency key", async () => {
    const response = {
      task_id: "task_1",
      status: "queued",
      status_detail: null,
      created_at: "2026-08-02T00:00:00Z",
      updated_at: "2026-08-02T00:00:00Z",
    };
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockImplementation(() =>
        Promise.resolve(new Response(JSON.stringify(response), { status: 201 })),
      );

    const input = { objective: "research", maxRevisions: 2, wantsReport: false };
    await createTask(identity, input, "task:stable-attempt");
    await createTask(identity, input, "task:stable-attempt");

    for (const [, init] of fetchMock.mock.calls) {
      expect(new Headers(init?.headers).get("idempotency-key")).toBe(
        "task:stable-attempt",
      );
    }
  });
});

describe("downloadArtifact", () => {
  it("uses the RFC 5987 response filename instead of the artifact id", async () => {
    const click = vi
      .spyOn(HTMLAnchorElement.prototype, "click")
      .mockImplementation(() => undefined);
    const createObjectURL = vi.fn(() => "blob:download");
    const revokeObjectURL = vi.fn();
    vi.stubGlobal("URL", { createObjectURL, revokeObjectURL });
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(new Blob(["word bytes"]), {
        status: 200,
        headers: {
          "content-disposition":
            "attachment; filename=\"download\"; filename*=UTF-8''%E6%8A%A5%E5%91%8A.docx",
          "content-type":
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        },
      }),
    );

    await downloadArtifact(identity, "art_opaque_1");

    expect(click).toHaveBeenCalledOnce();
    expect((click.mock.instances[0] as HTMLAnchorElement).download).toBe("报告.docx");
    expect(createObjectURL).toHaveBeenCalledOnce();
    expect(revokeObjectURL).toHaveBeenCalledWith("blob:download");
  });

  it("falls back to the validated ArtifactRef filename when the header is absent", async () => {
    const click = vi
      .spyOn(HTMLAnchorElement.prototype, "click")
      .mockImplementation(() => undefined);
    vi.stubGlobal("URL", {
      createObjectURL: vi.fn(() => "blob:download"),
      revokeObjectURL: vi.fn(),
    });
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(new Blob(["word bytes"]), { status: 200 }),
    );
    const artifact: ArtifactDownloadTarget = {
      artifact_id: "art_opaque_2",
      filename: "mcp-result.docx",
    };

    await downloadArtifact(identity, artifact);

    expect((click.mock.instances[0] as HTMLAnchorElement).download).toBe(
      "mcp-result.docx",
    );
  });

  it("never turns an artifact id into a fallback filename", async () => {
    const click = vi
      .spyOn(HTMLAnchorElement.prototype, "click")
      .mockImplementation(() => undefined);
    vi.stubGlobal("URL", {
      createObjectURL: vi.fn(() => "blob:download"),
      revokeObjectURL: vi.fn(),
    });
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(new Blob(["bytes"]), { status: 200 }),
    );

    await downloadArtifact(identity, "art_secret_storage_key");

    expect((click.mock.instances[0] as HTMLAnchorElement).download).toBe("artifact");
  });
});

describe("declaredMediaType", () => {
  // The failure this exists for: a browser that has nothing to say about a .md
  // hands back an empty string, and the ingestion parser reads neither "" nor
  // application/octet-stream.
  it("names a Markdown file the browser could not identify", () => {
    expect(declaredMediaType({ name: "notes.md", type: "" })).toBe("text/markdown");
  });

  it("names a Markdown file the browser called opaque bytes", () => {
    expect(declaredMediaType({ name: "notes.md", type: "application/octet-stream" })).toBe(
      "text/markdown",
    );
  });

  it("reads the extension case-insensitively", () => {
    expect(declaredMediaType({ name: "NOTES.MD", type: "" })).toBe("text/markdown");
  });

  it("keeps a browser type the server can already read", () => {
    // text/plain parses; replacing it with text/markdown would be this client
    // overriding a real observation with a guess from the name.
    expect(declaredMediaType({ name: "notes.md", type: "text/plain" })).toBe("text/plain");
  });

  it("ignores a charset parameter when matching the allow-list", () => {
    expect(declaredMediaType({ name: "notes.md", type: "text/plain; charset=utf-8" })).toBe(
      "text/plain",
    );
  });

  it("names a PDF the browser could not identify", () => {
    expect(declaredMediaType({ name: "report.pdf", type: "" })).toBe("application/pdf");
  });

  it("keeps the browser's own PDF type", () => {
    expect(declaredMediaType({ name: "report.pdf", type: "application/pdf" })).toBe(
      "application/pdf",
    );
  });

  // The control. An implementation that answered "text/markdown" whenever the
  // browser was unhelpful would pass every case above and fail here -- and it
  // would be exactly the guess the parser's allow-list exists to refuse.
  it("declares nothing beyond opaque bytes when the name says nothing", () => {
    expect(declaredMediaType({ name: "data.bin", type: "" })).toBe(
      "application/octet-stream",
    );
  });
});

describe("uploadDocument", () => {
  it("declares .md as Markdown in both the intent and the transfer", async () => {
    // jsdom's Crypto has getRandomValues and randomUUID but no subtle, and
    // uploadDocument hashes the file before it sends anything. The digest is
    // stubbed rather than computed because nothing here asserts on it -- what
    // is under test is the declaration, not the checksum.
    vi.stubGlobal("crypto", {
      ...globalThis.crypto,
      subtle: { digest: () => Promise.resolve(new Uint8Array([0x01]).buffer) },
    });

    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation((input) => {
      const url = typeof input === "string" ? input : (input as Request).url;
      if (url.endsWith("/content")) {
        return Promise.resolve(
          new Response(
            JSON.stringify({ artifact_id: "art_1", size_bytes: 6, sha256: "abc" }),
            { status: 201 },
          ),
        );
      }
      if (url === "/v1/uploads") {
        return Promise.resolve(
          new Response(
            JSON.stringify({
              upload_id: "upl_1",
              content_path: "/v1/uploads/upl_1/content",
            }),
            { status: 201 },
          ),
        );
      }
      return Promise.resolve(
        new Response(JSON.stringify({ version_id: "ver_1" }), { status: 201 }),
      );
    });

    await uploadDocument(identity, {
      // What a browser actually hands back for a Markdown file it cannot place.
      file: new File(["# 标题"], "notes.md", { type: "" }),
      documentId: "doc_1",
      knowledgeBaseId: "kb_1",
      grantedPrincipals: ["principal_a"],
    });

    const intentInit = fetchMock.mock.calls[0]?.[1];
    if (typeof intentInit?.body !== "string") throw new Error("request body was not JSON");
    const intentBody = JSON.parse(intentInit.body) as { media_type: string };
    expect(intentBody.media_type).toBe("text/markdown");
    // Both declarations come from one value on purpose: the server reads the
    // intent's type, so a divergence here would be invisible until ingestion.
    expect(new Headers(fetchMock.mock.calls[1]?.[1]?.headers).get("content-type")).toBe(
      intentBody.media_type,
    );
  });
});
