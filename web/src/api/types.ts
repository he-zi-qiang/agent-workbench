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

// --- Code sessions -------------------------------------------------------
//
// A coding turn answers with a report rather than an answer: nothing here
// crossed a publication fence, because a Code run has none. The files are the
// product, and `workspace_version` names the set of them the turn left behind.

export interface CodeAskResponse {
  report: string;
  workspace_version: Identifier | null;
  run_id: Identifier;
  status: string;
  stop_reason: string;
}

export interface PendingApprovalView {
  approval_id: Identifier;
  tool_name: string;
  argument_digest: string;
  risk: string | null;
}

export interface PendingApprovalsResponse {
  approvals: PendingApprovalView[];
}

export type ApprovalDecision = "approve_once" | "approve_for_session" | "deny";

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

/** Who made a submission-time decision (ADR-036): an explicit human choice,
 * an adopted triage verdict, or nobody -- the deployment default applied. */
export type IntentDecidedBy = "user" | "model" | "default";

/** Provenance the form attaches to a submission and the timeline shows back.
 * Never authority: the server stores it on the TaskSubmitted event and reads
 * none of it. */
export interface TaskIntent {
  graph_decided_by: IntentDecidedBy;
  wants_report_decided_by: IntentDecidedBy;
  reason?: string | null;
}

export interface TriageOption {
  graph: TaskGraphChoice;
  label: string;
}

/**
 * One of three outcomes. `default` is deliberately one answer for every cause
 * (disabled, no model, timeout, unreadable output): the instruction to this
 * client is the same -- submit exactly what it submitted before the endpoint
 * existed.
 */
export interface TriageResponse {
  status: "decided" | "ask" | "default";
  graph: TaskGraphChoice | null;
  wants_report: boolean | null;
  reason: string | null;
  question: string | null;
  options: TriageOption[];
}

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
  /**
   * How many agent invocations this Task has already paid for, across every
   * retry and every reclaim (ADR-040).
   *
   * Required rather than optional, even though the server's field carries a
   * default: the server always serialises it, and an optional number here is
   * one `?? 0` away from telling a reader that a Task which spent eleven
   * invocations spent none. A budget figure that silently reads as zero when
   * it is merely absent is worse than no figure at all.
   *
   * The ceiling it is spent against is deliberately *not* here, because the
   * response does not carry one: it lives in the Task's own
   * `run_semantics_snapshot` and only the Registry reads it. Anything this
   * client displayed as the limit would be this deployment's current setting,
   * not the one this Task was submitted under -- which is exactly the number
   * ADR-040 went to the trouble of freezing per Task.
   */
  agent_invocation_count: number;
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

/**
 * A Word document rendered as Markdown by the server, for showing inline.
 *
 * The counts below are what the extraction dropped or flattened on the way to
 * that Markdown. They are here because an omission is not guessable from the
 * text: truncated prose stops mid-sentence and announces itself, while the
 * paragraphs around a missing figure read as a finished argument. Server-side
 * (`adapters/documents/docx.py`) because only the reader of the .docx can know.
 *
 * All required, mirroring a pydantic model with no defaults
 * (`apps/api/routes/artifacts.py`). The alternative was optional counts, and it
 * is the wrong one for a reason specific to this payload: the panel shows a
 * count only when it is above zero, so an absent field and a zero render
 * identically -- as "the document has none". Optional would make the type
 * system content with a server that never says, which is exactly the silence
 * these fields exist to break. The two declarations are hand-written mirrors,
 * not generated, so they are changed in the same commit or not at all.
 */
export interface DocumentPreview {
  text: string;
  /**
   * The document continues past what `text` holds. Said, never implied.
   *
   * Read together with the counts rather than on its own, and the panel now
   * renders it as one of them (`PreviewGaps`). None of the seven moves when the
   * text stops early -- they are of the whole document -- so a plain document
   * cut in half scores zero on every one of them, and a list that showed only
   * counts would render empty, which is that list's way of saying nothing is
   * missing.
   */
  truncated: boolean;
  /**
   * Tables the document has, of the whole file like every count beside it.
   * It used to be of the part the extraction reached, so a truncated preview
   * counted only the tables above the cut -- and a table below it went missing
   * from the text and from the one number that would have mentioned it, which
   * left `PreviewGaps` rendering an empty list for a document that had lost a
   * table whole. The two readings agree whenever `truncated` is false.
   */
  table_count: number;
  /** Pictures, which the text preview drops entirely. */
  image_count: number;
  /** Header definitions -- the running title the preview never opens. */
  header_count: number;
  /** Footers, separate because a document may define one end and not the other. */
  footer_count: number;
  /**
   * Paragraphs Word numbers for itself. The digits are generated at layout
   * time, so an ordered list previews as unordered lines and a procedure
   * arrives without its order.
   */
  numbered_paragraph_count: number;
  /** Footnote marks. What is lost is the sentence, not the superscript. */
  footnote_count: number;
  /**
   * Paragraphs whose words came through and whose structure did not: the
   * Markdown is derived from built-in heading styles alone, so anything else --
   * including every style this project's own renderer applies -- is emitted as
   * a bare line.
   */
  flattened_paragraph_count: number;
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
  /**
   * Stored positions this page examined and could not decode.
   *
   * Required rather than optional because the server always sends it: an empty
   * array is its positive claim that *this page is complete*, not "the server
   * never looked". A short `events` tuple is also what the end of a stream
   * looks like, so dropping this field from the type is all it takes to render
   * a partial history as a whole one -- the exact failure the field exists to
   * prevent (`application/tasks.py`, `routes/tasks.py`).
   *
   * Positions, not a count, because they live in the namespace `events` and
   * `cursor` already use: a reader can be shown *where* the hole is, and an
   * operator handed a position can go find the row.
   */
  skipped_sequences: number[];
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
  /**
   * 这个身份能否往里加文档。用来决定「显示什么」，不用来决定「允许什么」——
   * 服务端的 require_writable 照样会拒绝，隐藏入口只是别让人白传一遍文件。
   */
  can_write: boolean;
  document_count: number;
  ready_document_count: number;
  processing_document_count: number;
  failed_document_count: number;
  created_at: string;
  updated_at: string;
}

export interface KnowledgeBaseListResponse {
  knowledge_bases: KnowledgeBaseView[];
}

export type KnowledgeDocumentStatus = "processing" | "ready" | "failed";

export interface KnowledgeDocumentView {
  document_id: Identifier;
  filename: string | null;
  media_type: string;
  size_bytes: number;
  source_revision: number;
  last_applied_revision: number;
  status: KnowledgeDocumentStatus;
  /** 摄取被拒的机器码；只在 status 是 failed 时有值。 */
  failure_code: string | null;
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
