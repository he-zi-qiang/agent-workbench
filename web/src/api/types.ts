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

/**
 * 一轮烧了多少。**缺席表示这里问不出答案，不表示花了零。**
 *
 * 用户那一条永远没有（一句提问没有自己的花销），还没落定的那一轮也没有。给这两
 * 种情况一个零，会在每一轮下面多出一行说谎的脚注。
 */
export interface TurnUsageView {
  input_tokens: number;
  output_tokens: number;
  /** `input_tokens` 的子集，不是它之外的另一笔。 */
  cache_read_tokens: number;
  cache_write_tokens: number;
  cost_micro_usd: number;
}

export interface MessageView {
  role: string;
  text: string;
  usage?: TurnUsageView | null;
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

/**
 * 这一轮里，一次写入由谁拍板（ADR-087）。
 *
 * 和 `CodeTurnMode` 分开，因为它们收紧的是信封的两半：`mode` 收紧工具清单，
 * 这个收紧「哪些风险要停在人面前」。合成一个四档的值，读起来更像一条梯子，
 * 但在唯一用到它的地方还得再拆回两半。
 *
 * 没有第三档。「什么都别问我」要拿掉的是 `destructive`，而那是 `project_run`
 * ——在这台机器上跑一条命令，ADR-077 说它跑之前要给人看见。这一档只加不减。
 */
export type CodeTurnApprovals = "standard" | "before_write";

export interface CodeAskResponse {
  report: string;
  workspace_version: Identifier | null;
  run_id: Identifier;
  status: string;
  stop_reason: string;
  /**
   * Why the turn failed, when it did (ADR-0084). Both null on a turn that
   * completed.
   *
   * `stop_reason` alone is not enough to say anything useful about a failure:
   * every provider problem arrives as `"error"`, so an account with no credit
   * left and a model id that no longer exists produced the same sentence. The
   * code is what the page writes Chinese from; the message is the fallback for
   * a code it has not learned, on the same rule `explainFailure` uses.
   *
   * Optional because a server older than this field still answers turns.
   */
  error_code?: string | null;
  error_message?: string | null;
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

/**
 * What a delegating run would be allowed in *this* deployment.
 *
 * Read before a Task exists, and that is the whole of what it describes. The
 * numbers below are the API process's current configuration; a Task already
 * submitted froze the same section into its own `run_semantics_snapshot` and
 * runs under that. So this belongs on a submission form and nowhere near a
 * running Task -- the same distinction `TaskView.agent_invocation_count`
 * makes one interface up, and for the same reason.
 *
 * Which sub-agents exist is deliberately not here. The catalogue is chosen by
 * what a process *is* rather than by configuration, and the API process holds
 * the Code one -- so an API that answered would be naming `explorer` to a
 * console asking what a Task may delegate to.
 */
export interface DelegationCapabilities {
  enabled: boolean;
  /**
   * Meaningful only when `enabled`. With delegation off the server sends 1 for
   * the three tree ceilings and 0 for the token one, which describes a tree
   * that is not built rather than one that has run out of room -- do not
   * render them as limits.
   */
  max_delegation_depth: number;
  max_children_per_run: number;
  max_parallel_child_invocations: number;
  max_tokens_per_agent_invocation: number;
}

export interface TaskCapabilitiesResponse {
  delegation: DelegationCapabilities;
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

/**
 * 屏幕控制服务器此刻的样子，或者读不到它的理由。
 *
 * `reachable` 是要分支的那个字段，而它和「`session` 是空的」是两件事：那台服务器**默认
 * 不启动**，所以「没在跑」是普通机器上的普通答案，而「跑着、但没人批准任何应用」是另一
 * 回事。这一页从写下那天起就在小心同一件事——当初它宁可什么都不显示，也不肯画一张编出来
 * 的名单。
 */
export interface ComputerSessionResponse {
  reachable: boolean;
  session: ComputerSession | null;
  /** 读不到的理由，读得到时是空串。 */
  detail: string;
}

export interface ComputerSession {
  service: string;
  /**
   * `"process"`，而这个词是承重的。
   *
   * 门禁的 allowlist 是**进程级**的，不是 MCP 会话级（known-gap F-19）。一块写着
   * 「这次会话」的面板会是第一个把会话级 grant 读进存在的地方，所以服务端答的是它
   * 真正的作用域，界面照抄。
   */
  scope: string;
  granted: ComputerGrant[];
  frontmost: ComputerFrontmost;
  actions: ComputerAction[];
}

export interface ComputerGrant {
  bundle_id: string;
  name: string;
  /** `read` / `click` / `full`。由应用自己推出，不接受申请。 */
  tier: string;
}

/**
 * 此刻最前面的那扇窗。
 *
 * **它带名字，哪怕没被批准**——而模型在同一时刻收到的每一句拒绝都不带（ADR-095）。
 * 两者是同一条规则的两个读者：这块面板的读者就坐在那扇窗前面，而他要做的判断正是
 * 「要不要把我正在用的这个也批准进来」。
 */
export interface ComputerFrontmost {
  bundle_id: string;
  name: string;
  granted: boolean;
}

export interface ComputerAction {
  at: string;
  action: string;
  /** 当时最前面的那个应用；门禁没读到就为 null。 */
  application: { bundle_id: string; name: string } | null;
  allowed: boolean;
  /** 被拒时，模型读到的那句拒绝的第一行。放行时是空串。 */
  reason: string;
  /** 送到几个字符、点在哪、哪块屏。没有就是空串。 */
  detail: string;
}

/**
 * 跨模式的用量总账（`GET /v1/usage`）。
 *
 * 服务端把钱以 **micro-USD 整数**送出来，不做任何换算。一个人民币金额需要一个
 * 这个进程没有、也不该编的汇率；换算是显示决定，留给显示的那一层。
 */
export interface UsageTokenBreakdown {
  input_tokens: number;
  output_tokens: number;
  /**
   * `input_tokens` 的**子集**，不是它之外的另一笔——服务商的口径，原样透传。
   * 把这两个数相加会把每一次命中的提示词算两遍。
   */
  cache_read_tokens: number;
  cache_write_tokens: number;
}

export interface UsageBucket {
  tokens: UsageTokenBreakdown;
  cost_micro_usd: number;
  /** 这一格里已经结束的运行数。没有它，一个 0 说不清是「没花钱」还是「没跑过」。 */
  runs: number;
}

export type UsageWindow = "7d" | "30d" | "all";

export interface UsageResponse {
  window: UsageWindow;
  since: string | null;
  until: string;
  /** 三个模式一定都在，没花过的那个是 0 而不是缺席。 */
  by_mode: Record<string, UsageBucket>;
  by_model: Record<string, UsageBucket>;
  /** 在 `by_mode.task` **里面**，不是它旁边。两个数不能相加。 */
  delegated: UsageBucket;
  /** 已经开始、还没写下终止事件的运行。是个说明，不是一笔用量。 */
  runs_in_flight: number;
  /** 这一档在这个窗口里每次运行记下的费用都是 0：说「没配价目表」，不说「免费」。 */
  unpriced_profiles: string[];
}
