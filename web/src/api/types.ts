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
  title: string | null;
}

export interface CreateChatSessionResponse {
  session_id: Identifier;
}

/**
 * 一件事，有名字，属于一个人（ADR-071）。
 *
 * 它是一层归属标注，不是容器：底下的对话、任务、编码会话和知识库各自有自己的接口和自己的
 * 生命周期，项目只记「它们是为同一件事做的」。所以这里没有任何权限字段——归属
 * 不影响可见性。
 */
export interface ProjectView {
  project_id: Identifier;
  name: string;
  created_at: string;
  updated_at: string;
  /** 归档只是从侧栏收起来，深链照样打得开。`null` 表示没归档。 */
  archived_at: string | null;
  /**
   * 这个项目所在的本机目录（ADR-072），`null` 表示没登记过。
   *
   * 出现且为 `null`，而不是不出现：客户端必须能区分「没登记目录」和「这个后端
   * 不支持目录」，而一个缺席的字段两者都说不了。
   */
  root_path: string | null;
}

export interface ProjectListResponse {
  projects: ProjectView[];
}

/** 一个可以被选中的目录（ADR-074）。 */
export interface DirectoryEntryView {
  name: string;
  /**
   * 绝对路径。这是整个 API 里唯一返回绝对路径的地方，而且必须是——下一次请求
   * 用的就是这条路径，返回相对名字等于把路径拼接推给客户端，而那正是 ADR-072
   * 要消除的东西。
   */
  path: string;
}

export interface DirectoryListingResponse {
  path: string;
  /** 到了文件系统根就是 `null`。由服务端给出，客户端不自己算上一级。 */
  parent: string | null;
  entries: DirectoryEntryView[];
  truncated: boolean;
}

export type ProjectItemKind = "chat" | "code" | "task" | "knowledge_base";

export interface ProjectItemView {
  kind: ProjectItemKind;
  item_id: Identifier;
  /** 会话由第一句指令命名，没说过话的会话就没有名字。不拿 id 编一个。 */
  title: string | null;
  ordered_at: string;
}

export interface ProjectContentsResponse {
  project_id: Identifier;
  items: ProjectItemView[];
}

export type ProjectEntryKind = "file" | "directory";

export interface ProjectFileEntryView {
  /** 永远是项目内的相对路径，永远不是绝对路径——绝对路径是服务端的事。 */
  path: string;
  kind: ProjectEntryKind;
  /** 目录是 `null`，不是 `0`：目录没有大小，不是大小为零。 */
  size_bytes: number | null;
  modified_at: string;
}

export interface ProjectListingResponse {
  path: string;
  entries: ProjectFileEntryView[];
  /**
   * 这次列举有没有被上限截断。忽略它的客户端会把半棵树画成整棵，而那在人看来
   * 就是「这个项目只有 500 个文件」。
   */
  truncated: boolean;
}

export interface ProjectFileContentResponse {
  path: string;
  /** 不是 UTF-8 时为 `null`。 */
  text: string | null;
  size_bytes: number;
  /**
   * 单独一个字段，不能由 `text === null` 推：一个合法的空文件 `text` 是 `""`，
   * 而它确实是文本。
   */
  is_text: boolean;
  modified_at: string;
}

/** A server-owned Chat session visible to its tenant + principal owner. */
export interface ChatSessionView {
  session_id: Identifier;
  title: string | null;
  last_activity_at: string | null;
  /** 这段对话是为哪个项目开的，`null` 表示不属于任何项目（ADR-071）。 */
  project_id: Identifier | null;
}

export interface ChatSessionListResponse {
  sessions: ChatSessionView[];
}

/**
 * One coding session, as the server lists it.
 *
 * The list used to live in `localStorage`, which answered a narrower question
 * than it looked like it answered: "what did I open in this browser since I
 * last cleared it". The sessions it forgot were still there and still owned --
 * only the link was gone.
 */
export interface CodeSessionView {
  session_id: Identifier;
  title: string | null;
  last_activity_at: string | null;
  /** 归在哪个项目下，或者没有（ADR-071）。 */
  project_id: Identifier | null;
}

export interface CodeSessionListResponse {
  sessions: CodeSessionView[];
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

/**
 * The stored text behind one citation (ADR-067).
 *
 * Served by a route that re-decides authorization from scratch, so a citation
 * the console is still displaying can correctly answer 404: the grant may have
 * been revoked, or the document re-ingested, since the answer was published.
 */
export interface CitedPassageView {
  chunk_id: Identifier;
  document_id: Identifier;
  document_version: Identifier;
  text: string;
  ordinal: number;
  /** Absent for every format without pages, and never defaulted to 1. */
  page: number | null;
}

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

/**
 * Whether a coding turn may change anything (ADR-0079).
 *
 * `"plan"` narrows the turn to the tools that only read, and says so in the
 * prompt. A named pair rather than a boolean, for the reason the API body uses
 * one: `plan: false` reads as an absence where `"act"` reads as a choice.
 */
export type CodeTurnMode = "act" | "plan";

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
  /**
   * The arguments as far as they fit, with `...[truncated]` where they were
   * cut. Bounded server-side at 2048 characters, so it is safe to render
   * verbatim.
   *
   * Beside the digest, not instead of it: the digest is the identity a
   * standing session rule is keyed by, and this is the only part a person can
   * actually read before answering.
   */
  approval_preview: string;
  risk: string | null;
}

export interface PendingApprovalsResponse {
  approvals: PendingApprovalView[];
}

export type ApprovalDecision = "approve_once" | "approve_for_session" | "deny";

export interface WorkspaceEntryView {
  name: string;
  size_bytes: number;
  media_type: string;
}

export interface WorkspaceResponse {
  files: WorkspaceEntryView[];
}

/**
 * What one click on 运行 produced (ADR-065).
 *
 * `stdout` and `stderr` stay apart because the two streams were written
 * independently and no interleaving after the fact would be a transcript --
 * it would be a guess presented as one. `omitted_inputs` is the honest half:
 * a working set larger than one sandbox call may carry does not lose files
 * silently, it says which ones stayed behind.
 */
export interface RunFileResponse {
  exit_code: number;
  stdout: string;
  stderr: string;
  written: string[];
  workspace_version: Identifier | null;
  omitted_inputs: string[];
  /**
   * The working set as it stands after this run.
   *
   * Carried so a produced file can be *shown* on the first render: `written`
   * is names, and a card needs the type and the size too. Re-reading the
   * listing to get them races the very response this is reacting to, which for
   * one render left every produced file as a line of text (known-gaps F-15).
   *
   * Optional on the wire: a server older than this field simply omits it, and
   * the caller falls back to the listing it already holds.
   */
  files?: WorkspaceEntryView[];
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

/**
 * `GET /v1/approvals` has no client any more.
 *
 * The endpoint is still served and still tested -- ADR-048 removed the console's
 * cross-task inbox, not the ability to answer -- but the answer now happens in
 * the Task that is waiting, and `getApproval` / `decideApproval` are what that
 * needs. A typed wrapper for a list nothing renders would be a wrapper that
 * drifts from the endpoint without anybody noticing.
 */

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
  /**
   * 这段对话属于哪个项目，`null` 表示不属于任何项目（ADR-071）。
   *
   * 可选而不是必填：这个本地投影早于服务端的归属存在，一段还没和服务端对过账的
   * 会话对这个问题**没有答案**——而 `null` 说的是「不属于任何项目」，是一个答案。
   * 两者不该混为一谈。
   */
  projectId?: Identifier | null;
}

export interface LocalTaskMetadata {
  taskId: Identifier;
  objective: string;
  knowledgeBaseId?: Identifier;
  createdAt: string;
}

export type EvaluationSuite = "rag" | "chat" | "triage";

/**
 * One report file, exactly as its runner wrote it.
 *
 * `payload` is deliberately open. The three suites measure different things,
 * and a shared shape here would be this file deciding which parts of a
 * measurement matter -- the same claim ADR-039 refuses to let a metric name
 * make. A surface that renders one checks for what it needs.
 */
export interface EvaluationReportView {
  suite: EvaluationSuite;
  name: string;
  payload: Record<string, unknown>;
}

export interface EvaluationReportsResponse {
  reports: EvaluationReportView[];
  runs_enabled: boolean;
  how_to_run: Record<string, string>;
}

export interface EvaluationRunView {
  suite: EvaluationSuite;
  status: "running" | "succeeded" | "failed";
  started_at: string;
  finished_at: string | null;
  exit_code: number | null;
  recent_output: string[];
}

export interface EvaluationCurrentRunResponse {
  /** `null` means this API process has not started one -- not that none exist. */
  run: EvaluationRunView | null;
}
