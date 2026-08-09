import type {
  ApprovalListResponse,
  ApprovalStatus,
  ApprovalView,
  ArtifactDownloadTarget,
  AskResponse,
  CreateSessionResponse,
  CreateUploadResponse,
  DocumentVersion,
  HealthResponse,
  HistoryResponse,
  KnowledgeBaseListResponse,
  KnowledgeBaseView,
  KnowledgeDocumentListResponse,
  PrincipalIdentity,
  SearchResponse,
  TaskListResponse,
  TaskStatus,
  TaskTimelineResponse,
  TaskView,
  UploadContentResponse,
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
  method?: "GET" | "POST" | "PUT";
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
    },
  });
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

export async function listApprovals(
  identity: PrincipalIdentity,
  options: { statuses?: ApprovalStatus[]; cursor?: string; limit?: number } = {},
): Promise<ApprovalListResponse> {
  const params = new URLSearchParams({ limit: String(options.limit ?? 25) });
  options.statuses?.forEach((status) => params.append("status", status));
  if (options.cursor) params.set("cursor", options.cursor);
  return apiRequest(identity, `/v1/approvals?${params.toString()}`);
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

export async function uploadDocument(
  identity: PrincipalIdentity,
  input: {
    file: File;
    documentId: string;
    knowledgeBaseId: string;
    grantedPrincipals: string[];
  },
): Promise<DocumentVersion> {
  const declaredSha256 = await sha256(input.file);
  const intent = await apiRequest<CreateUploadResponse>(identity, "/v1/uploads", {
    method: "POST",
    body: {
      declared_size_bytes: input.file.size,
      declared_sha256: declaredSha256,
      media_type: input.file.type || "application/octet-stream",
      filename: input.file.name,
    },
  });

  const transferred = await uploadBytes(identity, intent.content_path, input.file);
  return apiRequest(identity, `/v1/uploads/${encodeURIComponent(intent.upload_id)}/complete`, {
    method: "POST",
    body: {
      artifact_id: transferred.artifact_id,
      document_id: input.documentId,
      knowledge_base_id: input.knowledgeBaseId,
      granted_principals: input.grantedPrincipals,
    },
  });
}

async function uploadBytes(
  identity: PrincipalIdentity,
  path: string,
  file: File,
): Promise<UploadContentResponse> {
  const response = await fetch(path, {
    method: "PUT",
    headers: {
      ...identityHeaders(identity),
      "content-type": file.type || "application/octet-stream",
    },
    body: file,
  });
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
  const blob = await response.blob();
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download =
    filenameFromContentDisposition(response.headers.get("content-disposition")) ??
    (typeof target === "string" ? null : safeDownloadFilename(target.filename)) ??
    defaultArtifactFilename(response.headers.get("content-type"));
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
