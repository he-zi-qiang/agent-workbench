export type Identifier = string;

export interface PrincipalIdentity {
  tenantId: string;
  principalId: string;
  scopes: string[];
}

export interface SourceLocator {
  page?: number | null;
  paragraph?: number | null;
  char_start?: number | null;
  char_end?: number | null;
}

export interface Citation {
  chunk_id: Identifier;
  document_id: Identifier;
  document_version: Identifier;
  locator: SourceLocator;
  quote?: string | null;
}

export interface CreateSessionResponse {
  session_id: Identifier;
}

export interface AskResponse {
  answer: string;
  citations: Citation[];
  withheld: boolean;
  // False when no evidence was retrieved (ADR-018). Not inferable from an
  // empty citation list: a grounded answer may cite nothing.
  grounded: boolean;
  run_id: Identifier;
  turn_id: Identifier;
}

export type ChatAnswerMode = "direct" | "rag";

export interface MessageView {
  role: string;
  text: string;
}

export interface HistoryResponse {
  messages: MessageView[];
}

export type TaskStatus =
  | "queued"
  | "running"
  | "waiting_approval"
  | "waiting_migration"
  | "succeeded"
  | "failed"
  | "cancelled"
  | "dead_letter";

/**
 * Which pipeline a submission asks for: `research` writes a grounded report,
 * `general` does the work and reviews it (ADR-031). A shape, deliberately not
 * a graph version string -- the server maps it, so a client can never pin
 * itself to a version nobody deploys.
 */
export type TaskGraphChoice = "research" | "general";

export interface TaskView {
  task_id: Identifier;
  status: TaskStatus;
  status_detail: string | null;
  // A bounded copy of the submitted objective, for lists. Absent on Tasks
  // submitted before the server recorded one; the full objective always comes
  // from the input artifact.
  objective_preview: string | null;
  created_at: string;
  updated_at: string;
}

export interface TaskListResponse {
  tasks: TaskView[];
  cursor: string | null;
}

export type ApprovalStatus = "pending" | "approved" | "rejected";

export interface ApprovalView {
  approval_id: Identifier;
  task_id: Identifier;
  status: ApprovalStatus;
  decision_version: number;
  decided_at: string | null;
  created_at: string;
}

export interface ApprovalListResponse {
  approvals: ApprovalView[];
  cursor: string | null;
}

export interface TokenUsage {
  input_tokens?: number;
  output_tokens?: number;
  cache_read_tokens?: number;
  cache_write_tokens?: number;
}

export interface BudgetUsage {
  steps?: number;
  tool_calls?: number;
  tokens?: TokenUsage;
  cost_micro_usd?: number;
}

export interface ArtifactRef {
  schema_version?: number;
  artifact_id: Identifier;
  tenant_id?: Identifier;
  kind: string;
  media_type: string;
  size_bytes: number;
  sha256: string;
  filename?: string | null;
}

export type ArtifactDownloadTarget = Pick<
  ArtifactRef,
  "artifact_id" | "filename"
>;

export type EventPayload = {
  kind: string;
  [key: string]: unknown;
};

export interface EventEnvelope {
  schema_version: number;
  event_id: Identifier;
  stream_id: Identifier;
  run_id: Identifier;
  event_type: string;
  durability: "durable" | "transient";
  timestamp: string;
  payload: EventPayload;
  sequence: number | null;
  task_id: Identifier | null;
  graph_node_id: Identifier | null;
  parent_event_id: Identifier | null;
}

export interface TaskTimelineResponse {
  task_id: Identifier;
  events: EventEnvelope[];
  cursor: string | null;
}

export interface SearchHit {
  chunk_id: Identifier;
  document_id: Identifier;
  document_version: Identifier;
  text: string;
}

export interface SearchResponse {
  hits: SearchHit[];
  citations: Citation[];
  retriever: string;
}

export interface CreateUploadResponse {
  upload_id: Identifier;
  content_path: string;
}

export interface UploadContentResponse {
  artifact_id: Identifier;
  size_bytes: number;
  sha256: string;
}

export interface DocumentVersion {
  schema_version: number;
  version_id: Identifier;
  document_id: Identifier;
  source_revision: number;
  artifact_id: Identifier;
  content_sha256: string;
}

export interface KnowledgeBaseView {
  knowledge_base_id: Identifier;
  name: string;
  description: string | null;
  document_count: number;
  ready_document_count: number;
  processing_document_count: number;
  created_at: string;
  updated_at: string;
}

export interface KnowledgeBaseListResponse {
  knowledge_bases: KnowledgeBaseView[];
}

export type KnowledgeDocumentStatus = "processing" | "ready";

export interface KnowledgeDocumentView {
  document_id: Identifier;
  filename: string | null;
  media_type: string;
  size_bytes: number;
  source_revision: number;
  last_applied_revision: number;
  status: KnowledgeDocumentStatus;
  created_at: string;
  updated_at: string;
}

export interface KnowledgeDocumentListResponse {
  documents: KnowledgeDocumentView[];
}

export interface HealthResponse {
  status: "live" | "ready" | "unready";
}

export interface LocalChatSession {
  sessionId: Identifier;
  title: string;
  answerMode: ChatAnswerMode;
  knowledgeBaseId: Identifier | null;
  createdAt: string;
  updatedAt: string;
}

export interface LocalTaskMetadata {
  taskId: Identifier;
  objective: string;
  knowledgeBaseId?: Identifier;
  createdAt: string;
}
