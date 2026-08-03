import type {
  ApprovalListResponse,
  ApprovalStatus,
  ApprovalView,
  AskResponse,
  CreateSessionResponse,
  CreateUploadResponse,
  DocumentVersion,
  HealthResponse,
  HistoryResponse,
  PrincipalIdentity,
  SearchResponse,
  TaskListResponse,
  TaskStatus,
  TaskTimelineResponse,
  TaskView,
  UploadContentResponse,
} from "./types";

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
  input: { question: string; knowledgeBaseId: string; topK?: number },
  idempotencyKey: string,
  signal?: AbortSignal,
): Promise<AskResponse> {
  return apiRequest(identity, `/v1/chat/sessions/${encodeURIComponent(sessionId)}/messages`, {
    method: "POST",
    headers: { "Idempotency-Key": idempotencyKey },
    body: {
      question: input.question,
      knowledge_base_id: input.knowledgeBaseId,
      top_k: input.topK ?? 8,
    },
    ...(signal === undefined ? {} : { signal }),
  });
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
  input: { objective: string; maxRevisions: number; knowledgeBaseId?: string },
  idempotencyKey = newIdempotencyKey("task"),
): Promise<TaskView> {
  return apiRequest(identity, "/v1/tasks", {
    method: "POST",
    headers: { "Idempotency-Key": idempotencyKey },
    body: {
      objective: input.objective,
      max_revisions: input.maxRevisions,
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
  artifactId: string,
): Promise<void> {
  const response = await fetch(`/v1/artifacts/${encodeURIComponent(artifactId)}`, {
    headers: identityHeaders(identity),
  });
  if (!response.ok) throw await parseError(response);
  const blob = await response.blob();
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = artifactId;
  anchor.click();
  URL.revokeObjectURL(url);
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
