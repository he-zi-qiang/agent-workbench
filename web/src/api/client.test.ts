import { afterEach, describe, expect, it, vi } from "vitest";
import {
  MAX_LAYOUT_BYTES,
  apiRequest,
  askChat,
  createTask,
  declaredMediaType,
  downloadArtifact,
  getChatSession,
  getDocumentPdf,
  identityHeaders,
  listChatSessions,
  renameChatSession,
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
      agent_invocation_count: 0,
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

  it("lists, resolves and renames Chat sessions through the owner-scoped endpoints", async () => {
    const listed = {
      sessions: [
        {
          session_id: "ses_chat_1",
          title: "旧名字",
          last_activity_at: "2026-08-20T10:00:00Z",
        },
      ],
    };
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(
        new Response(JSON.stringify(listed), { status: 200 }),
      )
      .mockResolvedValueOnce(
        new Response(JSON.stringify(listed.sessions[0]), { status: 200 }),
      )
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({ ...listed.sessions[0], title: "新名字" }),
          { status: 200 },
        ),
      );

    await listChatSessions(identity);
    await getChatSession(identity, "ses_chat_1");
    await renameChatSession(identity, "ses_chat_1", "新名字");

    expect(fetchMock.mock.calls[0]?.[0]).toBe("/v1/chat/sessions");
    expect(fetchMock.mock.calls[1]?.[0]).toBe("/v1/chat/sessions/ses_chat_1");
    expect(fetchMock.mock.calls[1]?.[1]?.method).toBe("GET");
    expect(fetchMock.mock.calls[2]?.[0]).toBe("/v1/chat/sessions/ses_chat_1");
    const renameInit = fetchMock.mock.calls[2]?.[1];
    expect(renameInit?.method).toBe("PATCH");
    expect(renameInit?.body).toBe(JSON.stringify({ title: "新名字" }));
  });
});

describe("what a failure with no detail says", () => {
  // 只有服务端一个字都没给的时候才走到这里——而从这里出去的句子会落到每个页面
  // 的 ErrorNotice 上。此前它是「请求失败（HTTP 409）」：一个读者做不了任何事的
  // 数字，配一句什么都没说的话。状态码恰恰是浏览器在这一刻唯一知道的东西，所以
  // 它被花在「这是哪一类失败、你能做什么」上。
  it.each([
    { status: 401, says: /没有权限/ },
    { status: 403, says: /没有权限/ },
    { status: 404, says: /已经没有这个东西/ },
    { status: 409, says: /状态变了/ },
    { status: 429, says: /太密/ },
    { status: 503, says: /服务端出错/ },
  ])("turns $status into something the reader can act on", async ({ status, says }) => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.resolve(new Response("", { status }))),
    );
    await expect(apiRequest(identity, "/v1/anything")).rejects.toThrow(says);
  });

  // 兜底那一档保留数字：说不出别的的时候，数字至少能被贴进 issue 里。
  it("keeps the number only where nothing better can be said", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.resolve(new Response("", { status: 418 }))),
    );
    await expect(apiRequest(identity, "/v1/anything")).rejects.toThrow(/418/);
  });

  // 服务端给了话就用服务端的，这条没变。
  it("prefers what the server said over any of them", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() =>
        Promise.resolve(
          new Response(JSON.stringify({ detail: "这个会话已经在跑一轮了" }), {
            status: 409,
            headers: { "content-type": "application/json" },
          }),
        ),
      ),
    );
    await expect(apiRequest(identity, "/v1/anything")).rejects.toThrow(
      "这个会话已经在跑一轮了",
    );
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

describe("getDocumentPdf", () => {
  it("asks as the principal, because a frame cannot", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(new Blob(["%PDF-1.7"]), {
        status: 200,
        headers: { "content-type": "application/pdf" },
      }),
    );

    const layout = await getDocumentPdf(identity, "art_opaque_1");

    expect(layout.available).toBe(true);
    const [path, init] = fetchMock.mock.calls[0] ?? [];
    expect(path).toBe("/v1/artifacts/art_opaque_1/pdf");
    // The reason this fetch exists at all: `<iframe src="/v1/...">` would issue
    // its own request with none of these, and render a 404 in the panel.
    expect(new Headers(init?.headers).get("x-tenant-id")).toBe("tenant_a");
    expect(new Headers(init?.headers).get("x-principal-id")).toBe("principal_a");
  });

  it("reports a missing converter as an answer rather than as a failure", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ detail: "layout conversion is unavailable" }), {
        status: 503,
        headers: { "content-type": "application/json" },
      }),
    );

    // Resolved, not thrown. A deployment without a converter is not broken and
    // the document is not lost, so this must not travel the path that carries
    // "something went wrong" -- the panel keeps its text preview and says why
    // there is no layout.
    await expect(getDocumentPdf(identity, "art_opaque_1")).resolves.toEqual({
      available: false,
      reason: "converter_unavailable",
    });
  });

  it("declines a build whose API has no such route", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ detail: "Not Found" }), { status: 404 }),
    );

    await expect(getDocumentPdf(identity, "art_opaque_1")).resolves.toEqual({
      available: false,
      reason: "unavailable",
    });
  });

  it("refuses bytes that are not a PDF instead of framing them", async () => {
    // A blob: URL inherits this page's origin, so a response body that came
    // back as HTML would run as this page if it were framed. The type is what
    // decides, so the type is checked before a URL exists.
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(new Blob(["<script>alert(1)</script>"]), {
        status: 200,
        headers: { "content-type": "text/html" },
      }),
    );

    await expect(getDocumentPdf(identity, "art_opaque_1")).resolves.toEqual({
      available: false,
      reason: "unavailable",
    });
  });

  it("refuses an oversized layout on the declared length, before reading it", async () => {
    // The ceiling used to be checked against `byteLength` alone -- a number
    // that exists because the whole body has already been allocated -- under a
    // comment promising this page would hold no more than MAX_LAYOUT_BYTES of
    // it. A 200 MiB conversion was held in full and then refused. The header is
    // where that promise can be kept, and it is the one the API actually sends:
    // the layout route returns a plain `Response`, so Starlette declares its
    // length.
    const read = vi.spyOn(Response.prototype, "arrayBuffer");
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      // Eight bytes, declaring far more. Which is the point: what is acted on
      // is the declaration, and a declaration that understates cannot buy more
      // bytes -- the body is delimited by it.
      new Response(new Uint8Array(8), {
        status: 200,
        headers: {
          "content-type": "application/pdf",
          "content-length": String(MAX_LAYOUT_BYTES + 1),
        },
      }),
    );

    await expect(getDocumentPdf(identity, "art_opaque_1")).resolves.toEqual({
      available: false,
      reason: "too_large",
    });
    expect(read).not.toHaveBeenCalled();
  });

  it("refuses a layout larger than this page will hold, declared or not", async () => {
    // The other half, and the reason the check after the read stayed: a
    // response that declares no length reaches the ceiling only once the bytes
    // are in hand. `Response` adds no `content-length` of its own, so this is
    // that case rather than the one above -- the body has to be read to find
    // out, and what the constant bounds here is what gets *kept*.
    const read = vi.spyOn(Response.prototype, "arrayBuffer");
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(new Uint8Array(MAX_LAYOUT_BYTES + 1), {
        status: 200,
        headers: { "content-type": "application/pdf" },
      }),
    );

    // The blob lives in the query cache for the session, so this is the ceiling
    // on what one artifact can pin there -- not a judgement about the document,
    // which downloads at any size.
    await expect(getDocumentPdf(identity, "art_opaque_1")).resolves.toEqual({
      available: false,
      reason: "too_large",
    });
    expect(read).toHaveBeenCalledOnce();
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

  // A .docx is the one upload where the browser usually *does* know the type,
  // and the long registered string is easy to get subtly wrong in a set. Both
  // directions are checked: the browser's own declaration survives, and a
  // browser that said nothing still gets a type the ingestion parser reads.
  it("keeps the type a browser reports for a Word document", () => {
    expect(
      declaredMediaType({
        name: "report.docx",
        type: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
      }),
    ).toBe("application/vnd.openxmlformats-officedocument.wordprocessingml.document");
  });

  it("names a Word document the browser could not identify", () => {
    expect(declaredMediaType({ name: "report.docx", type: "" })).toBe(
      "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    );
  });

  it("leaves a .doc alone, because this build cannot read the old format", () => {
    // The parser accepts the `application/msword` *alias* that some uploaders
    // attach to a .docx; it cannot read an actual binary .doc. Guessing a
    // readable type from a .doc extension would turn "we cannot read this"
    // into a document stuck at "indexing" forever.
    expect(declaredMediaType({ name: "report.doc", type: "" })).toBe(
      "application/octet-stream",
    );
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

  it("hands the caller's abort signal to the transfer, not only to the JSON calls", async () => {
    // The transfer is the leg the caller is actually buying. The intent and
    // `/complete` are single round trips that are over before anyone could
    // change their mind; the PUT is the document itself, and on a real file it
    // is where the seconds -- or minutes -- are spent. A signal threaded into
    // the two quick calls and dropped from the PUT cancels nothing anybody
    // could observe, and a caller's hook can be tested green against it,
    // because the hook only ever sees the promise. So it is asserted here,
    // against the request that leaves.
    vi.stubGlobal("crypto", {
      ...globalThis.crypto,
      subtle: { digest: () => Promise.resolve(new Uint8Array([0x01]).buffer) },
    });

    const controller = new AbortController();
    const signals: (AbortSignal | null | undefined)[] = [];
    let transferStarted = () => {};
    const transferring = new Promise<void>((resolve) => {
      transferStarted = resolve;
    });

    vi.spyOn(globalThis, "fetch").mockImplementation((input, init) => {
      const url = typeof input === "string" ? input : (input as Request).url;
      signals.push(init?.signal);
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
      if (url.endsWith("/content")) {
        transferStarted();
        return new Promise<Response>((_resolve, reject) => {
          // Rejects the way `fetch` does when the request is called off.
          init?.signal?.addEventListener("abort", () =>
            reject(new DOMException(String(init.signal?.reason), "AbortError")),
          );
          // A transfer handed no signal would otherwise hang here and this test
          // would report a timeout, which says nothing. Settling it lets the
          // assertion below name the actual defect.
          setTimeout(() => reject(new Error("the transfer was never called off")), 200);
        });
      }
      return Promise.resolve(
        new Response(JSON.stringify({ version_id: "ver_1" }), { status: 201 }),
      );
    });

    const pending = uploadDocument(
      identity,
      {
        file: new File(["# 标题"], "notes.md", { type: "" }),
        documentId: "doc_1",
        knowledgeBaseId: "kb_1",
        grantedPrincipals: ["principal_a"],
      },
      controller.signal,
    );
    await transferring;
    controller.abort("不再上传这个文件");

    await expect(pending).rejects.toThrow();
    expect(signals[0]).toBe(controller.signal);
    expect(signals[1]).toBe(controller.signal);
    // And `/complete` never went out. That request is the one that attaches the
    // document to a knowledge base, so an abort that beats it is the difference
    // between a wasted upload and a file in a base the reader has left.
    expect(signals).toHaveLength(2);
  });

  it("carries it into the request that attaches the document as well", async () => {
    // `/complete` is issued only after the transfer returns, so an abort raised
    // during a transfer that had already finished lands in the gap in front of
    // it. Without the signal on this request that abort is silent and the
    // document is attached anyway -- which is the whole failure, arrived at
    // through a narrower door.
    vi.stubGlobal("crypto", {
      ...globalThis.crypto,
      subtle: { digest: () => Promise.resolve(new Uint8Array([0x01]).buffer) },
    });

    const controller = new AbortController();
    const signals: (AbortSignal | null | undefined)[] = [];
    vi.spyOn(globalThis, "fetch").mockImplementation((input, init) => {
      const url = typeof input === "string" ? input : (input as Request).url;
      signals.push(init?.signal);
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
      if (url.endsWith("/content")) {
        return Promise.resolve(
          new Response(
            JSON.stringify({ artifact_id: "art_1", size_bytes: 6, sha256: "abc" }),
            { status: 201 },
          ),
        );
      }
      return Promise.resolve(
        new Response(JSON.stringify({ version_id: "ver_1" }), { status: 201 }),
      );
    });

    await uploadDocument(
      identity,
      {
        file: new File(["# 标题"], "notes.md", { type: "" }),
        documentId: "doc_1",
        knowledgeBaseId: "kb_1",
        grantedPrincipals: ["principal_a"],
      },
      controller.signal,
    );

    expect(signals).toHaveLength(3);
    expect(signals[2]).toBe(controller.signal);
  });
});
