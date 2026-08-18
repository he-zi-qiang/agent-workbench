import type {
  ApprovalDecision,
  ApprovalView,
  ArtifactDownloadTarget,
  AskResponse,
  CodeAskResponse,
  CodeSessionListResponse,
  CodeSessionView,
  CreateSessionResponse,
  CreateUploadResponse,
  DocumentPreview,
  DocumentVersion,
  EvaluationCurrentRunResponse,
  EvaluationReportsResponse,
  EvaluationRunView,
  EvaluationSuite,
  HealthResponse,
  CitedPassageView,
  HistoryResponse,
  KnowledgeBaseListResponse,
  KnowledgeBaseView,
  KnowledgeDocumentListResponse,
  PendingApprovalsResponse,
  PrincipalIdentity,
  SearchResponse,
  TaskGraphChoice,
  TaskIntent,
  TaskListResponse,
  TaskStatus,
  TaskTimelineResponse,
  TaskView,
  TriageResponse,
  UploadContentResponse,
  RunFileResponse,
  WorkspaceResponse,
} from "./types";

const WORD_DOCUMENT_MEDIA_TYPE =
  "application/vnd.openxmlformats-officedocument.wordprocessingml.document";

export class ApiError extends Error {
  readonly status: number;
  readonly detail: unknown;

  constructor(status: number, message: string, detail?: unknown) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }
}

interface RequestOptions {
  method?: "GET" | "POST" | "PUT" | "PATCH" | "DELETE";
  body?: unknown;
  headers?: Record<string, string>;
  signal?: AbortSignal;
}

function identityHeaders(identity: PrincipalIdentity): Record<string, string> {
  const headers: Record<string, string> = {
    "x-tenant-id": identity.tenantId,
    "x-principal-id": identity.principalId,
  };
  if (identity.scopes.length > 0) {
    headers["x-principal-scopes"] = identity.scopes.join(",");
  }
  return headers;
}

async function parseError(response: Response): Promise<ApiError> {
  let detail: unknown;
  try {
    detail = await response.json();
  } catch {
    detail = await response.text().catch(() => undefined);
  }
  const message =
    typeof detail === "object" && detail !== null && "detail" in detail
      ? String(detail.detail)
      : `请求失败（HTTP ${response.status}）`;
  return new ApiError(response.status, message, detail);
}

export async function apiRequest<T>(
  identity: PrincipalIdentity,
  path: string,
  options: RequestOptions = {},
): Promise<T> {
  const headers: Record<string, string> = {
    accept: "application/json",
    ...identityHeaders(identity),
    ...options.headers,
  };
  let body: BodyInit | undefined;
  if (options.body !== undefined) {
    headers["content-type"] = "application/json";
    body = JSON.stringify(options.body);
  }
  const init: RequestInit = {
    method: options.method ?? "GET",
    headers,
  };
  if (body !== undefined) init.body = body;
  if (options.signal !== undefined) init.signal = options.signal;
  const response = await fetch(path, init);
  if (!response.ok) throw await parseError(response);
  // A 204 has no body, and parsing one throws `SyntaxError: Unexpected end of
  // JSON input` from inside the success path -- where `response.ok` has already
  // promised the caller there is nothing to catch. This repository's delete
  // routes answer 200 with a body precisely so they never take this branch;
  // it is here for the endpoints that already answered 204 before anybody
  // looked (`cancelEvaluationRun` is one), and for the next one that does.
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

export function newIdempotencyKey(prefix: string): string {
  return `${prefix}:${crypto.randomUUID()}`;
}

export async function createChatSession(
  identity: PrincipalIdentity,
  title?: string,
): Promise<CreateSessionResponse> {
  return apiRequest(identity, "/v1/chat/sessions", {
    method: "POST",
    body: { title: title || null },
  });
}

export async function getChatHistory(
  identity: PrincipalIdentity,
  sessionId: string,
  signal?: AbortSignal,
): Promise<HistoryResponse> {
  return apiRequest(identity, `/v1/chat/sessions/${encodeURIComponent(sessionId)}/messages`, {
    ...(signal === undefined ? {} : { signal }),
  });
}

/**
 * The passage behind one citation of one answer.
 *
 * Addressed through the turn rather than by chunk id alone, and that is the
 * server's shape rather than this client's preference: a bare chunk id cannot
 * be turned into the knowledge base the index read needs.
 */
export async function getCitedPassage(
  identity: PrincipalIdentity,
  sessionId: string,
  turnId: string,
  chunkId: string,
): Promise<CitedPassageView> {
  return apiRequest(
    identity,
    `/v1/chat/sessions/${encodeURIComponent(sessionId)}/turns/${encodeURIComponent(turnId)}/citations/${encodeURIComponent(chunkId)}`,
  );
}

export async function askChat(
  identity: PrincipalIdentity,
  sessionId: string,
  input: {
    question: string;
    answerMode: "direct" | "rag";
    knowledgeBaseId: string | null;
    topK?: number;
  },
  idempotencyKey: string,
  signal?: AbortSignal,
): Promise<AskResponse> {
  return apiRequest(identity, `/v1/chat/sessions/${encodeURIComponent(sessionId)}/messages`, {
    method: "POST",
    headers: { "Idempotency-Key": idempotencyKey },
    body: {
      question: input.question,
      answer_mode: input.answerMode,
      knowledge_base_id: input.knowledgeBaseId,
      top_k: input.topK ?? 8,
    },
    ...(signal === undefined ? {} : { signal }),
  });
}

export async function createCodeSession(
  identity: PrincipalIdentity,
  title?: string,
): Promise<CreateSessionResponse> {
  return apiRequest(identity, "/v1/code/sessions", {
    method: "POST",
    body: { title: title || null },
  });
}

export async function getCodeHistory(
  identity: PrincipalIdentity,
  sessionId: string,
  signal?: AbortSignal,
): Promise<HistoryResponse> {
  return apiRequest(identity, `/v1/code/sessions/${encodeURIComponent(sessionId)}/messages`, {
    ...(signal === undefined ? {} : { signal }),
  });
}

export async function askCode(
  identity: PrincipalIdentity,
  sessionId: string,
  instruction: string,
  idempotencyKey: string,
  signal?: AbortSignal,
): Promise<CodeAskResponse> {
  return apiRequest(identity, `/v1/code/sessions/${encodeURIComponent(sessionId)}/messages`, {
    method: "POST",
    headers: { "Idempotency-Key": idempotencyKey },
    body: { instruction },
    ...(signal === undefined ? {} : { signal }),
  });
}

export async function getCodeWorkspace(
  identity: PrincipalIdentity,
  sessionId: string,
  signal?: AbortSignal,
): Promise<WorkspaceResponse> {
  return apiRequest(
    identity,
    `/v1/code/sessions/${encodeURIComponent(sessionId)}/workspace`,
    { ...(signal === undefined ? {} : { signal }) },
  );
}

export async function getCodeApprovals(
  identity: PrincipalIdentity,
  sessionId: string,
  signal?: AbortSignal,
): Promise<PendingApprovalsResponse> {
  return apiRequest(
    identity,
    `/v1/code/sessions/${encodeURIComponent(sessionId)}/approvals`,
    { ...(signal === undefined ? {} : { signal }) },
  );
}

export async function decideCodeApproval(
  identity: PrincipalIdentity,
  sessionId: string,
  approvalId: string,
  decision: ApprovalDecision,
): Promise<void> {
  await apiRequest(
    identity,
    `/v1/code/sessions/${encodeURIComponent(sessionId)}/approvals/${encodeURIComponent(approvalId)}`,
    { method: "POST", body: { decision } },
  );
}

export async function listKnowledgeBases(
  identity: PrincipalIdentity,
  signal?: AbortSignal,
): Promise<KnowledgeBaseListResponse> {
  return apiRequest(identity, "/v1/knowledge-bases", {
    ...(signal === undefined ? {} : { signal }),
  });
}

export async function createKnowledgeBase(
  identity: PrincipalIdentity,
  input: { name: string; description?: string },
): Promise<KnowledgeBaseView> {
  return apiRequest(identity, "/v1/knowledge-bases", {
    method: "POST",
    body: {
      name: input.name,
      description: input.description?.trim() || null,
    },
  });
}

export async function getKnowledgeBase(
  identity: PrincipalIdentity,
  knowledgeBaseId: string,
): Promise<KnowledgeBaseView> {
  return apiRequest(
    identity,
    `/v1/knowledge-bases/${encodeURIComponent(knowledgeBaseId)}`,
  );
}

export async function listKnowledgeBaseDocuments(
  identity: PrincipalIdentity,
  knowledgeBaseId: string,
  signal?: AbortSignal,
): Promise<KnowledgeDocumentListResponse> {
  return apiRequest(
    identity,
    `/v1/knowledge-bases/${encodeURIComponent(knowledgeBaseId)}/documents`,
    { ...(signal === undefined ? {} : { signal }) },
  );
}

export async function listTasks(
  identity: PrincipalIdentity,
  options: { statuses?: TaskStatus[]; cursor?: string; limit?: number } = {},
): Promise<TaskListResponse> {
  const params = new URLSearchParams({ limit: String(options.limit ?? 25) });
  options.statuses?.forEach((status) => params.append("status", status));
  if (options.cursor) params.set("cursor", options.cursor);
  return apiRequest(identity, `/v1/tasks?${params.toString()}`);
}

export async function getTask(
  identity: PrincipalIdentity,
  taskId: string,
): Promise<TaskView> {
  return apiRequest(identity, `/v1/tasks/${encodeURIComponent(taskId)}`);
}

export async function createTask(
  identity: PrincipalIdentity,
  input: {
    objective: string;
    maxRevisions: number;
    knowledgeBaseId?: string;
    wantsReport: boolean;
    graph?: TaskGraphChoice;
    intent?: TaskIntent;
  },
  idempotencyKey = newIdempotencyKey("task"),
): Promise<TaskView> {
  return apiRequest(identity, "/v1/tasks", {
    method: "POST",
    headers: { "Idempotency-Key": idempotencyKey },
    body: {
      objective: input.objective,
      max_revisions: input.maxRevisions,
      wants_report: input.wantsReport,
      ...(input.knowledgeBaseId
        ? { knowledge_base_id: input.knowledgeBaseId }
        : {}),
      // Only when chosen. Absent means the deployment's default -- the exact
      // bytes this client sent before the field existed.
      ...(input.graph === undefined ? {} : { graph: input.graph }),
      // Provenance for the timeline (ADR-036); absent claims nothing.
      ...(input.intent === undefined ? {} : { intent: input.intent }),
    },
  });
}

/**
 * Ask the server to propose this objective's shape before submitting.
 *
 * A failed or timed-out call is reported as `default` rather than thrown:
 * every failure carries the same instruction -- submit what you would have
 * submitted anyway -- and a create form that surfaced a triage error would
 * block the one action triage exists to smooth (ADR-036).
 */
export async function triageTask(
  identity: PrincipalIdentity,
  input: {
    objective: string;
    knowledgeBaseSelected: boolean;
    attachmentNames?: string[];
  },
  options: { signal?: AbortSignal } = {},
): Promise<TriageResponse> {
  try {
    return await apiRequest<TriageResponse>(identity, "/v1/tasks/triage", {
      method: "POST",
      body: {
        objective: input.objective,
        knowledge_base_selected: input.knowledgeBaseSelected,
        ...(input.attachmentNames === undefined || input.attachmentNames.length === 0
          ? {}
          : { attachment_names: input.attachmentNames }),
      },
      ...(options.signal === undefined ? {} : { signal: options.signal }),
    });
  } catch {
    return {
      status: "default",
      graph: null,
      wants_report: null,
      reason: null,
      question: null,
      options: [],
    };
  }
}

export async function cancelTask(
  identity: PrincipalIdentity,
  taskId: string,
  reason: string,
): Promise<TaskView> {
  return apiRequest(identity, `/v1/tasks/${encodeURIComponent(taskId)}/cancel`, {
    method: "POST",
    body: { reason },
  });
}

export async function getTaskTimeline(
  identity: PrincipalIdentity,
  taskId: string,
  cursor?: string,
): Promise<TaskTimelineResponse> {
  const params = new URLSearchParams({ limit: "200" });
  if (cursor) params.set("cursor", cursor);
  return apiRequest(
    identity,
    `/v1/tasks/${encodeURIComponent(taskId)}/timeline?${params.toString()}`,
  );
}

export async function getApproval(
  identity: PrincipalIdentity,
  approvalId: string,
): Promise<ApprovalView> {
  return apiRequest(identity, `/v1/approvals/${encodeURIComponent(approvalId)}`);
}

export async function decideApproval(
  identity: PrincipalIdentity,
  approval: ApprovalView,
  decision: "approved" | "rejected",
): Promise<ApprovalView> {
  return apiRequest(
    identity,
    `/v1/approvals/${encodeURIComponent(approval.approval_id)}/decisions`,
    {
      method: "POST",
      body: {
        decision,
        decision_version: approval.decision_version + 1,
      },
    },
  );
}

export async function searchKnowledge(
  identity: PrincipalIdentity,
  input: { query: string; knowledgeBaseId: string; topK?: number },
): Promise<SearchResponse> {
  return apiRequest(identity, "/v1/search", {
    method: "POST",
    body: {
      query: input.query,
      knowledge_base_id: input.knowledgeBaseId,
      top_k: input.topK ?? 8,
    },
  });
}

/** Every media type the ingestion parser reads from a declaration alone. */
const SERVER_READABLE_MEDIA_TYPES = new Set([
  "text/plain",
  "text/markdown",
  "text/x-markdown",
  "application/pdf",
  // Both spellings the ingestion parser accepts. The long one is the
  // registered type browsers send; the short alias turns up from some
  // uploaders, and this set has to match the server's or a file the parser
  // can read gets its declaration overwritten by the extension table below.
  "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
  "application/msword",
]);

/** The same extensions the upload controls already accept, mapped to a type. */
const MEDIA_TYPE_BY_EXTENSION: readonly (readonly [string, string])[] = [
  [".md", "text/markdown"],
  [".markdown", "text/markdown"],
  [".pdf", "application/pdf"],
  [".docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"],
  // `text/plain` is in the set above, so a browser that types a .txt correctly
  // never reaches this table. The entry is for the browsers that do not -- the
  // same ones that hand back `""` for a .md -- and it is the fourth of the four
  // families the ingestion parser reads.
  [".txt", "text/plain"],
];

/**
 * What to declare a file as when the browser's own guess is unusable.
 *
 * Browsers routinely hand back `""` or `application/octet-stream` for a `.md`,
 * and neither is a type the ingestion parser reads -- so the declaration the
 * browser produced is the one thing that makes an upload the UI advertises fail.
 * It fails late and quietly, too: the three upload calls all succeed, and the
 * document sits at "正在建立索引" forever because the async worker refuses it.
 *
 * Falling back to the extension is not this client inventing a fact about
 * bytes. The file name is already what decides whether a file is accepted at
 * all (`ACCEPTED_EXTENSIONS` in AttachmentTray, the `accept` attributes); only
 * the declaration sent to the server disagreed with it. A browser type the
 * parser can read always wins -- it is the more specific claim, and overriding
 * `text/plain` on a `.md` would discard information rather than add it.
 *
 * A name that says nothing still gets `application/octet-stream`, for the same
 * reason the CLI does (`apps/cli/upload.py`): the server decides what it can
 * parse, and guessing past the name would be asserting something we never read.
 */
export function declaredMediaType(file: { name: string; type: string }): string {
  const declared = file.type.split(";", 1)[0]?.trim().toLowerCase() ?? "";
  if (SERVER_READABLE_MEDIA_TYPES.has(declared)) return declared;

  const name = file.name.toLowerCase();
  const matched = MEDIA_TYPE_BY_EXTENSION.find(([extension]) => name.endsWith(extension));
  return matched?.[1] ?? "application/octet-stream";
}

/**
 * `signal` is threaded through all three legs, not just the first.
 *
 * The three requests are not equally cancellable and it is worth being exact
 * about which one the caller is buying. Aborting the intent or the PUT leaves
 * nothing attached to any knowledge base -- an unused upload id and, at worst,
 * an orphan blob. Only `/complete` puts the document into a knowledge base, so
 * an abort that lands before it is sent is the one that actually prevents the
 * write; one that lands after the request left the browser cannot un-write it.
 *
 * That residual race is a few milliseconds wide. The window this closes is the
 * PUT, which for a real document is seconds to minutes of upload still to go,
 * and which every abort reaches.
 */
export async function uploadDocument(
  identity: PrincipalIdentity,
  input: {
    file: File;
    documentId: string;
    knowledgeBaseId: string;
    grantedPrincipals: string[];
  },
  signal?: AbortSignal,
): Promise<DocumentVersion> {
  const declaredSha256 = await sha256(input.file);
  // Computed once and threaded through the transfer: the server reads the
  // intent's type rather than the PUT's header, but two call sites deriving it
  // separately is how a declaration and its bytes start disagreeing.
  const mediaType = declaredMediaType(input.file);
  const intent = await apiRequest<CreateUploadResponse>(identity, "/v1/uploads", {
    method: "POST",
    body: {
      declared_size_bytes: input.file.size,
      declared_sha256: declaredSha256,
      media_type: mediaType,
      filename: input.file.name,
    },
    ...(signal === undefined ? {} : { signal }),
  });

  const transferred = await uploadBytes(
    identity,
    intent.content_path,
    input.file,
    mediaType,
    signal,
  );
  return apiRequest(identity, `/v1/uploads/${encodeURIComponent(intent.upload_id)}/complete`, {
    method: "POST",
    body: {
      artifact_id: transferred.artifact_id,
      document_id: input.documentId,
      knowledge_base_id: input.knowledgeBaseId,
      granted_principals: input.grantedPrincipals,
    },
    ...(signal === undefined ? {} : { signal }),
  });
}

async function uploadBytes(
  identity: PrincipalIdentity,
  path: string,
  file: File,
  mediaType: string,
  signal?: AbortSignal,
): Promise<UploadContentResponse> {
  const init: RequestInit = {
    method: "PUT",
    headers: {
      ...identityHeaders(identity),
      "content-type": mediaType,
    },
    body: file,
  };
  if (signal !== undefined) init.signal = signal;
  const response = await fetch(path, init);
  if (!response.ok) throw await parseError(response);
  return (await response.json()) as UploadContentResponse;
}

async function sha256(file: File): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", await file.arrayBuffer());
  return Array.from(new Uint8Array(digest), (value) =>
    value.toString(16).padStart(2, "0"),
  ).join("");
}

export async function checkHealth(path: "/health/live" | "/health/ready") {
  const response = await fetch(path, { headers: { accept: "application/json" } });
  const payload = (await response.json()) as HealthResponse;
  return { ok: response.ok, status: payload.status };
}

export async function downloadArtifact(
  identity: PrincipalIdentity,
  target: string | ArtifactDownloadTarget,
): Promise<void> {
  const artifactId = typeof target === "string" ? target : target.artifact_id;
  const response = await fetch(`/v1/artifacts/${encodeURIComponent(artifactId)}`, {
    headers: identityHeaders(identity),
  });
  if (!response.ok) throw await parseError(response);
  saveBlob(
    await response.blob(),
    filenameFromContentDisposition(response.headers.get("content-disposition")) ??
      (typeof target === "string" ? null : safeDownloadFilename(target.filename)) ??
      defaultArtifactFilename(response.headers.get("content-type")),
  );
}

/**
 * Hand a blob to the browser as a file to keep.
 *
 * One copy of the anchor dance, because there are now two callers and the part
 * that is easy to get subtly wrong is the same in both: the object URL has to
 * be revoked, and forgetting it leaks the whole file for as long as the tab
 * lives -- which for a workspace of generated documents is not a rounding error.
 */
function saveBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  anchor.click();
  URL.revokeObjectURL(url);
}

function filenameFromContentDisposition(header: string | null): string | null {
  if (header === null) return null;

  const extended = /(?:^|;)\s*filename\*\s*=\s*UTF-8''([^;]*)/i.exec(header);
  if (extended?.[1] !== undefined) {
    try {
      const decoded = safeDownloadFilename(decodeURIComponent(extended[1].trim()));
      if (decoded !== null) return decoded;
    } catch {
      // A malformed extended parameter may still have a valid ASCII fallback.
    }
  }

  const basic =
    /(?:^|;)\s*filename\s*=\s*(?:"((?:\\.|[^"\\])*)"|([^;]*))/i.exec(header);
  const raw = basic?.[1]?.replace(/\\(.)/g, "$1") ?? basic?.[2];
  return safeDownloadFilename(raw);
}

function safeDownloadFilename(value: string | null | undefined): string | null {
  if (value === null || value === undefined) return null;
  const cleaned = value.trim();
  if (
    cleaned === "" ||
    cleaned === "." ||
    cleaned === ".." ||
    cleaned.length > 255 ||
    cleaned.includes("/") ||
    cleaned.includes("\\") ||
    Array.from(cleaned).some((character) => {
      const codePoint = character.codePointAt(0) ?? 0;
      return codePoint < 32 || codePoint === 127;
    })
  ) {
    return null;
  }
  return cleaned;
}

function defaultArtifactFilename(contentType: string | null): string {
  const mediaType = contentType?.split(";", 1)[0]?.trim().toLowerCase();
  return mediaType === WORD_DOCUMENT_MEDIA_TYPE ? "mcp-result.docx" : "artifact";
}

/**
 * An artifact's bytes as text, for showing a result instead of making the
 * reader download it to find out what the Task produced.
 *
 * Bounded on the client because this renders into the page: the artifact store
 * has its own ceiling, but "the server allowed it" is not the same question as
 * "a browser should paint it". Over the limit the caller shows the download
 * instead, which is what a large binary deserved anyway.
 */
export const MAX_PREVIEW_BYTES = 512 * 1024;

export async function getArtifactText(
  identity: PrincipalIdentity,
  artifactId: string,
): Promise<{ text: string; truncated: boolean }> {
  const response = await fetch(`/v1/artifacts/${encodeURIComponent(artifactId)}`, {
    headers: identityHeaders(identity),
  });
  if (!response.ok) throw await parseError(response);
  const blob = await response.blob();
  if (blob.size > MAX_PREVIEW_BYTES) {
    return { text: await blob.slice(0, MAX_PREVIEW_BYTES).text(), truncated: true };
  }
  return { text: await blob.text(), truncated: false };
}

/**
 * A Word document as text, extracted by the server.
 *
 * Server-side because a .docx is a zip of XML: doing it here means shipping a
 * zip reader and an XML parser to every page load to re-derive text the API can
 * already produce with the same library that wrote the file.
 */
export async function getDocumentPreview(
  identity: PrincipalIdentity,
  artifactId: string,
): Promise<DocumentPreview> {
  const response = await fetch(
    `/v1/artifacts/${encodeURIComponent(artifactId)}/preview`,
    { headers: { ...identityHeaders(identity), accept: "application/json" } },
  );
  if (!response.ok) throw await parseError(response);
  return (await response.json()) as DocumentPreview;
}

/**
 * What this page will hold in memory to show one page layout.
 *
 * The source .docx is already capped at 20 MiB by the preview route, but a
 * converted PDF is not bounded by that: a document whose weight is text
 * converts small, and one that is thirty full-page scans does not. The number
 * is a page's worth of held bytes rather than a judgement about documents --
 * the blob lives in the query cache for the session, so this is the ceiling on
 * what one artifact can pin there.
 *
 * Applied to the declared `content-length` first, and to the bytes only after.
 * The sentence above used to be a claim the code did not keep: the one check
 * read `byteLength`, which exists because the whole body had already been
 * allocated -- a 200 MiB conversion was held in full and *then* refused. The
 * header is where that promise can be kept, and trusting it is not credulity:
 * a length that understates cannot buy more bytes, because the body is
 * delimited by it. What the header cannot cover is a response that declares
 * nothing -- a chunked one, or a proxy that dropped it -- and for those the
 * check after the read is the whole of the bound. So: refused before arrival
 * when the length is declared, and refused before it is *kept* when it is not.
 */
export const MAX_LAYOUT_BYTES = 32 * 1024 * 1024;

/**
 * Why this deployment is not showing a layout, when it is not showing one.
 *
 * `converter_unavailable` is the case worth naming: converting .docx to PDF
 * needs an external program, and a deployment without one is correctly
 * configured for everything except this panel.
 */
export type DocumentLayoutDecline =
  | "converter_unavailable"
  | "too_large"
  | "unavailable";

/**
 * A layout view, or the reason there is none.
 *
 * The absence is a value rather than a thrown error, and that is the whole
 * design of this function. A deployment with no converter is not broken and the
 * document is not lost -- the text preview beside this is unaffected and the
 * file downloads unchanged -- so a decline must not travel the path that
 * carries "something went wrong". Thrown, it would arrive at the panel as an
 * exception indistinguishable from a real fault, and the panel would have to
 * turn it red or guess.
 */
export type DocumentLayout =
  | { available: true; blob: Blob }
  | { available: false; reason: DocumentLayoutDecline };

/**
 * A Word document as a page layout, for readers who need to see the document
 * rather than read it.
 *
 * Fetched into a blob rather than pointed at with `<iframe src="/v1/...">`,
 * because a frame issues its own request and carries no headers this code can
 * set -- the identity headers every other call here sends would be missing, and
 * the frame would show a 404. That is the same constraint the event stream has
 * (`apps/api/web.py`), reached from the other direction.
 *
 * The caller owns the blob URL and its lifetime: the URL has to outlive the
 * call for the frame to keep rendering, which is why this returns the blob and
 * not a URL -- `downloadArtifact` above can revoke immediately because a click
 * has already consumed it, and copying that here would revoke a frame's source
 * out from under it.
 */
export async function getDocumentPdf(
  identity: PrincipalIdentity,
  artifactId: string,
): Promise<DocumentLayout> {
  const response = await fetch(
    `/v1/artifacts/${encodeURIComponent(artifactId)}/pdf`,
    { headers: { ...identityHeaders(identity), accept: "application/pdf" } },
  );
  if (!response.ok) {
    // 503 is the converter; everything else -- a build whose API has no such
    // route at all, a document that would not convert, a refusal on size --
    // lands on the same fallback and differs only in the sentence shown.
    if (response.status === 503) {
      return { available: false, reason: "converter_unavailable" };
    }
    return {
      available: false,
      reason: response.status === 413 ? "too_large" : "unavailable",
    };
  }
  const declared = response.headers.get("content-type")?.split(";", 1)[0]?.trim();
  if (declared?.toLowerCase() !== "application/pdf") {
    // A blob: URL inherits this page's origin, so what the frame renders is
    // decided by the blob's type. Anything that is not a PDF is refused here
    // rather than framed and hoped about, and the type below is asserted rather
    // than inherited from the response for the same reason.
    return { available: false, reason: "unavailable" };
  }
  // An absent header parses to 0 and a malformed one to NaN, and neither is a
  // declaration of size, so both fall through to the check after the read. Only
  // a number the response actually stated is acted on here.
  const declaredLength = Number(response.headers.get("content-length"));
  if (Number.isFinite(declaredLength) && declaredLength > MAX_LAYOUT_BYTES) {
    // Cancelled rather than abandoned: the transfer this refusal exists to
    // avoid paying for is still in flight, and dropping the reference would
    // leave it arriving into a body nobody reads.
    void response.body?.cancel().catch(() => undefined);
    return { available: false, reason: "too_large" };
  }
  const bytes = await response.arrayBuffer();
  if (bytes.byteLength > MAX_LAYOUT_BYTES) {
    return { available: false, reason: "too_large" };
  }
  return { available: true, blob: new Blob([bytes], { type: "application/pdf" }) };
}

/**
 * An artifact's bytes as a blob, for a viewer on this page rather than a file
 * in the Downloads folder.
 *
 * Fetched here instead of pointing an `<img>`/`<iframe>` at the route for the
 * same reason `getDocumentPdf` fetches: an embedded element issues its own
 * request and carries none of the identity headers, so it would render a 404.
 * The caller owns the object URL and its lifetime -- this returns the blob,
 * not a URL, because a frame keeps reading its source after the call returns.
 */
export async function getArtifactBlob(
  identity: PrincipalIdentity,
  artifactId: string,
): Promise<Blob> {
  const response = await fetch(`/v1/artifacts/${encodeURIComponent(artifactId)}`, {
    headers: identityHeaders(identity),
  });
  if (!response.ok) throw await parseError(response);
  return response.blob();
}

export async function getArtifactJson<T>(
  identity: PrincipalIdentity,
  artifactId: string,
): Promise<T> {
  const response = await fetch(`/v1/artifacts/${encodeURIComponent(artifactId)}`, {
    headers: { ...identityHeaders(identity), accept: "application/json" },
  });
  if (!response.ok) throw await parseError(response);
  return (await response.json()) as T;
}

export { identityHeaders };

/**
 * One file out of a coding session's working set.
 *
 * Addressed by session and name rather than by artifact id, because that is the
 * only address a client has: the listing withholds the id on purpose, so that
 * nothing in a browser can name a version the session has already moved past.
 */
export async function downloadCodeWorkspaceFile(
  identity: PrincipalIdentity,
  sessionId: string,
  name: string,
): Promise<void> {
  const response = await fetchCodeWorkspaceFile(identity, sessionId, name);
  saveBlob(
    await response.blob(),
    filenameFromContentDisposition(response.headers.get("content-disposition")) ??
      safeDownloadFilename(name) ??
      defaultArtifactFilename(response.headers.get("content-type")),
  );
}

/**
 * The same file, as text to show rather than as a file to keep.
 *
 * Capped at the same size the artifact preview is capped at, and truncated
 * rather than refused: a reader looking at the head of a large generated file
 * is better served than one told the file is too big to look at.
 */
export async function getCodeWorkspaceFileText(
  identity: PrincipalIdentity,
  sessionId: string,
  name: string,
): Promise<{ text: string; truncated: boolean }> {
  const blob = await (await fetchCodeWorkspaceFile(identity, sessionId, name)).blob();
  if (blob.size > MAX_PREVIEW_BYTES) {
    return { text: await blob.slice(0, MAX_PREVIEW_BYTES).text(), truncated: true };
  }
  return { text: await blob.text(), truncated: false };
}

/**
 * The same file, as a blob for an in-page viewer (an image, a PDF frame).
 * The identity-header reasoning of `getArtifactBlob` applies unchanged; the
 * address stays session + name because that is the only address a client has.
 */
export async function getCodeWorkspaceFileBlob(
  identity: PrincipalIdentity,
  sessionId: string,
  name: string,
): Promise<Blob> {
  return (await fetchCodeWorkspaceFile(identity, sessionId, name)).blob();
}

function fetchCodeWorkspaceFile(
  identity: PrincipalIdentity,
  sessionId: string,
  name: string,
): Promise<Response> {
  // Each segment encoded separately. A workspace name cannot contain a slash --
  // the server's own type forbids it -- so encoding the whole tail as one piece
  // would only differ for names that are already impossible, while leaving a
  // reader unsure which rule is doing the work.
  return fetch(
    `/v1/code/sessions/${encodeURIComponent(sessionId)}/workspace/${encodeURIComponent(name)}`,
    { headers: identityHeaders(identity) },
  ).then(async (response) => {
    if (!response.ok) throw await parseError(response);
    return response;
  });
}

export async function listCodeSessions(
  identity: PrincipalIdentity,
  signal?: AbortSignal,
): Promise<CodeSessionListResponse> {
  return apiRequest(identity, "/v1/code/sessions", {
    ...(signal === undefined ? {} : { signal }),
  });
}

export async function renameCodeSession(
  identity: PrincipalIdentity,
  sessionId: string,
  title: string,
): Promise<CodeSessionView> {
  return apiRequest(identity, `/v1/code/sessions/${encodeURIComponent(sessionId)}`, {
    method: "PATCH",
    body: { title },
  });
}

/**
 * The three deletes.
 *
 * Written out rather than folded into one `deleteThing(kind, id)`: the three
 * are different enough that a shared helper would have to take the path apart
 * again, and each one's failure modes belong to its own call site -- a Task
 * refuses while it is still running, a session refuses while a turn is in
 * flight, and a chat session also has a browser-local row to forget.
 */
export async function deleteCodeSession(
  identity: PrincipalIdentity,
  sessionId: string,
): Promise<{ session_id: string }> {
  return apiRequest(identity, `/v1/code/sessions/${encodeURIComponent(sessionId)}`, {
    method: "DELETE",
  });
}

/**
 * Put one file into a coding session's workspace.
 *
 * A raw-body PUT, not a multipart form, because that is the seam this API puts
 * document bytes through -- `apiRequest` is the control plane and serialises
 * JSON, so this builds its own request rather than widening that one to carry
 * a Blob it would have to special-case.
 */
/**
 * Run one Python file out of a coding session's working set (ADR-065).
 *
 * A POST because it is not safe and not idempotent: it starts a container and
 * whatever the script writes lands in the workspace. No body -- the reader
 * supplies no script, only which of their own files runs, and that is in the
 * path already.
 *
 * No `Idempotency-Key` either, unlike a turn. A key buys a stable run id so a
 * retry is refused as busy rather than becoming a second turn; there is no
 * turn here to be second, and re-running a file is an ordinary thing to want
 * twice.
 */
export async function runCodeWorkspaceFile(
  identity: PrincipalIdentity,
  sessionId: string,
  name: string,
): Promise<RunFileResponse> {
  return apiRequest(
    identity,
    `/v1/code/sessions/${encodeURIComponent(sessionId)}/workspace/${encodeURIComponent(name)}/run`,
    { method: "POST" },
  );
}

export async function putCodeWorkspaceFile(
  identity: PrincipalIdentity,
  sessionId: string,
  file: File,
): Promise<WorkspaceResponse> {
  const response = await fetch(
    `/v1/code/sessions/${encodeURIComponent(sessionId)}/workspace/${encodeURIComponent(file.name)}`,
    {
      method: "PUT",
      headers: {
        accept: "application/json",
        ...identityHeaders(identity),
        // The browser's own guess, and `application/octet-stream` when it has
        // none. Sending the empty string would make the server read a header
        // that is present and meaningless.
        "content-type": file.type === "" ? "application/octet-stream" : file.type,
      },
      body: file,
    },
  );
  if (!response.ok) throw await parseError(response);
  return (await response.json()) as WorkspaceResponse;
}

export async function deleteChatSession(
  identity: PrincipalIdentity,
  sessionId: string,
): Promise<{ session_id: string }> {
  return apiRequest(identity, `/v1/chat/sessions/${encodeURIComponent(sessionId)}`, {
    method: "DELETE",
  });
}

export async function deleteTask(
  identity: PrincipalIdentity,
  taskId: string,
): Promise<{ task_id: string }> {
  return apiRequest(identity, `/v1/tasks/${encodeURIComponent(taskId)}`, {
    method: "DELETE",
  });
}

export async function getEvaluationReports(
  identity: PrincipalIdentity,
  signal?: AbortSignal,
): Promise<EvaluationReportsResponse> {
  return apiRequest(identity, "/v1/evaluation/reports", {
    ...(signal === undefined ? {} : { signal }),
  });
}

export async function getEvaluationRun(
  identity: PrincipalIdentity,
  signal?: AbortSignal,
): Promise<EvaluationCurrentRunResponse> {
  return apiRequest(identity, "/v1/evaluation/runs/current", {
    ...(signal === undefined ? {} : { signal }),
  });
}

export async function startEvaluationRun(
  identity: PrincipalIdentity,
  suite: EvaluationSuite,
): Promise<EvaluationRunView> {
  return apiRequest(identity, "/v1/evaluation/runs", {
    method: "POST",
    body: { suite },
  });
}

export async function cancelEvaluationRun(
  identity: PrincipalIdentity,
): Promise<void> {
  await apiRequest(identity, "/v1/evaluation/runs/current/cancel", {
    method: "POST",
  });
}
