import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  BookOpen,
  CheckCircle2,
  FileSearch,
  FileText,
  FileUp,
  Library,
  Lock,
  MessageSquare,
  PanelLeft,
  Plus,
  RefreshCw,
  Search,
  X,
} from "lucide-react";
import {
  type FormEvent,
  type KeyboardEvent as ReactKeyboardEvent,
  useEffect,
  useRef,
  useState,
} from "react";
import { Link, useSearchParams } from "react-router-dom";
import {
  createKnowledgeBase,
  listKnowledgeBaseDocuments,
  searchKnowledge,
  uploadDocument,
} from "../../api/client";
import type {
  KnowledgeBaseView,
  KnowledgeDocumentStatus,
  SearchResponse,
} from "../../api/types";
import { useIdentity } from "../../app/IdentityContext";
import {
  useWorkspaceSidebar,
  WorkspaceSidebarPortal,
} from "../../app/WorkspaceSidebar";
import {
  knowledgeBaseQueryKey,
  useKnowledgeBases,
} from "../../components/KnowledgeSourcePicker";
import {
  EmptyState,
  ErrorNotice,
  IconButton,
  LoadingLine,
  formatDateTime,
  shortId,
} from "../../components/ui";

export function KnowledgePage() {
  const { identity } = useIdentity();
  const queryClient = useQueryClient();
  const [searchParams, setSearchParams] = useSearchParams();
  const knowledgeBases = useKnowledgeBases(identity);
  const [selectedIdDraft, setSelectedId] = useState<string | null>(
    searchParams.get("kb"),
  );
  const [createOpen, setCreateOpen] = useState(false);
  const focusBeforeCreate = useRef<HTMLElement | null>(null);
  const mobileSidebarTriggerRef = useRef<HTMLButtonElement | null>(null);
  const workspaceSidebar = useWorkspaceSidebar();
  const availableKnowledgeBases = knowledgeBases.data?.knowledge_bases ?? [];
  const selectedId =
    availableKnowledgeBases.some(
      (item) => item.knowledge_base_id === selectedIdDraft,
    )
      ? selectedIdDraft
      : availableKnowledgeBases[0]?.knowledge_base_id ?? null;

  const selected = knowledgeBases.data?.knowledge_bases.find(
    (item) => item.knowledge_base_id === selectedId,
  );
  const selectKnowledgeBase = (id: string) => {
    setSelectedId(id);
    setSearchParams({ kb: id }, { replace: true });
    workspaceSidebar.close();
  };
  const openCreate = () => {
    focusBeforeCreate.current =
      workspaceSidebar.drawerOpen
        ? mobileSidebarTriggerRef.current
        : document.activeElement instanceof HTMLElement
          ? document.activeElement
          : null;
    // On mobile the create trigger lives inside the workspace drawer. Close it
    // before mounting a second modal surface; the dialog takes focus below.
    workspaceSidebar.close();
    setCreateOpen(true);
  };
  const closeCreate = () => {
    const returnTarget = focusBeforeCreate.current;
    setCreateOpen(false);
    window.requestAnimationFrame(() => {
      if (returnTarget?.isConnected) returnTarget.focus();
      if (focusBeforeCreate.current === returnTarget) focusBeforeCreate.current = null;
    });
  };

  return (
    <div className="aw-knowledge-page">
      <WorkspaceSidebarPortal>
        <aside className="aw-knowledge-list aw-knowledge-sidebar" aria-label="知识库列表">
          <header className="aw-knowledge-sidebar-header">
            <strong>知识库</strong>
            <div>
              <IconButton
                label="刷新知识库"
                disabled={knowledgeBases.isFetching}
                onClick={() => void knowledgeBases.refetch()}
              >
                <RefreshCw aria-hidden="true" size={15} />
              </IconButton>
              <IconButton
                className="aw-knowledge-sessions-close"
                label="关闭知识库列表"
                onClick={workspaceSidebar.close}
              >
                <X aria-hidden="true" size={17} />
              </IconButton>
            </div>
          </header>
          <button
            className="aw-new-session aw-new-knowledge"
            onClick={openCreate}
            type="button"
          >
            <Plus aria-hidden="true" size={15} />
            <span>新建知识库</span>
          </button>
          <div className="aw-knowledge-sidebar-list">
            {knowledgeBases.data?.knowledge_bases.map((knowledgeBase) => (
              <KnowledgeBaseRow
                active={knowledgeBase.knowledge_base_id === selectedId}
                key={knowledgeBase.knowledge_base_id}
                knowledgeBase={knowledgeBase}
                onClick={() => selectKnowledgeBase(knowledgeBase.knowledge_base_id)}
              />
            ))}
          </div>
        </aside>
      </WorkspaceSidebarPortal>

      <main className="aw-knowledge-main">
        <button
          aria-controls="workspace-sidebar-context"
          aria-expanded={workspaceSidebar.drawerOpen}
          aria-label="打开知识库列表"
          className="aw-icon-button aw-knowledge-mobile-sidebar"
          onClick={workspaceSidebar.open}
          ref={mobileSidebarTriggerRef}
          type="button"
        >
          <PanelLeft aria-hidden="true" size={18} />
        </button>
        {knowledgeBases.isPending ? <LoadingLine label="正在读取知识库" /> : null}
        {knowledgeBases.error === null ? null : (
          <ErrorNotice message={errorMessage(knowledgeBases.error)} />
        )}

        {!knowledgeBases.isPending &&
        knowledgeBases.error === null &&
        knowledgeBases.data?.knowledge_bases.length === 0 ? (
          <EmptyState
            action={
              <button
                className="aw-button is-primary"
                onClick={openCreate}
                type="button"
              >
                <Plus aria-hidden="true" size={16} />
                创建第一个知识库
              </button>
            }
            description="创建后上传 PDF、Word 或 Markdown；索引完成即可在对话和任务中使用。"
            icon={<Library aria-hidden="true" size={24} />}
            title="还没有知识库"
          />
        ) : null}

        {selected === undefined && (knowledgeBases.data?.knowledge_bases.length ?? 0) > 0 ? (
          <EmptyState
            description="从侧边栏选择一个知识库查看文档。"
            icon={<BookOpen aria-hidden="true" size={22} />}
            title="选择知识库"
          />
        ) : null}
        {selected === undefined ? null : (
          <KnowledgeBaseDetail
            identity={identity}
            key={selected.knowledge_base_id}
            knowledgeBase={selected}
            onChanged={async () => {
              await queryClient.invalidateQueries({
                queryKey: knowledgeBaseQueryKey(identity),
              });
            }}
          />
        )}
      </main>

      {createOpen ? (
        <CreateKnowledgeBaseDialog
          identity={identity}
          onClose={closeCreate}
          onCreated={(knowledgeBase) => {
            void queryClient.invalidateQueries({
              queryKey: knowledgeBaseQueryKey(identity),
            });
            selectKnowledgeBase(knowledgeBase.knowledge_base_id);
            // `selectKnowledgeBase` also closes the workspace drawer. Schedule
            // the dialog's return focus afterwards so that close cannot steal
            // focus back to an older drawer opener.
            closeCreate();
          }}
        />
      ) : null}
    </div>
  );
}

function KnowledgeBaseRow({
  knowledgeBase,
  active,
  onClick,
}: {
  knowledgeBase: KnowledgeBaseView;
  active: boolean;
  onClick: () => void;
}) {
  return (
    <button
      aria-current={active ? "true" : undefined}
      className={`aw-knowledge-row ${active ? "is-active" : ""}`}
      onClick={onClick}
      type="button"
    >
      <span className="aw-knowledge-icon">
        <Library aria-hidden="true" size={18} />
      </span>
      <span>
        <strong>{knowledgeBase.name}</strong>
        <small>
          {knowledgeBase.document_count} 个文档 · {knowledgeBase.ready_document_count} 个可用
          {knowledgeBase.failed_document_count > 0
            ? ` · ${knowledgeBase.failed_document_count} 个失败`
            : ""}
        </small>
      </span>
      {knowledgeBase.processing_document_count > 0 ? (
        <i title={`${knowledgeBase.processing_document_count} 个文档正在处理`} />
      ) : null}
    </button>
  );
}

function KnowledgeBaseDetail({
  identity,
  knowledgeBase,
  onChanged,
}: {
  identity: ReturnType<typeof useIdentity>["identity"];
  knowledgeBase: KnowledgeBaseView;
  onChanged: () => Promise<void>;
}) {
  const [file, setFile] = useState<File | null>(null);
  const [grantedTo, setGrantedTo] = useState("");
  const [uploadNotice, setUploadNotice] = useState<string | null>(null);
  const documents = useQuery({
    queryKey: [
      "knowledge-base-documents",
      identity.tenantId,
      identity.principalId,
      [...identity.scopes].sort().join(","),
      knowledgeBase.knowledge_base_id,
    ],
    queryFn: ({ signal }) =>
      listKnowledgeBaseDocuments(identity, knowledgeBase.knowledge_base_id, signal),
    refetchInterval: (query) =>
      query.state.data?.documents.some((document) => document.status === "processing")
        ? 2_000
        : false,
  });
  const upload = useMutation({
    mutationFn: async (selectedFile: File) =>
      uploadDocument(identity, {
        file: selectedFile,
        documentId: `doc_${crypto.randomUUID().replaceAll("-", "")}`,
        knowledgeBaseId: knowledgeBase.knowledge_base_id,
        grantedPrincipals: parseGrantedPrincipals(grantedTo),
      }),
    onSuccess: async () => {
      setFile(null);
      // 授权名单跟着文件一起清掉：它是给刚传的那一份文档写的 ACL，留在框里
      // 会让下一份文件默默继承上一份的授权对象。
      setGrantedTo("");
      setUploadNotice("文件已经上传，正在建立索引。完成前不会用于回答。 ");
      await documents.refetch();
      await onChanged();
    },
  });
  const [query, setQuery] = useState("");
  const [searchResult, setSearchResult] = useState<SearchResponse | null>(null);
  const search = useMutation({
    mutationFn: (question: string) =>
      searchKnowledge(identity, {
        query: question,
        knowledgeBaseId: knowledgeBase.knowledge_base_id,
        topK: 8,
      }),
    onSuccess: setSearchResult,
  });

  const ready = documents.data?.documents.filter(
    (document) => document.status === "ready",
  ).length;
  const processing = documents.data?.documents.filter(
    (document) => document.status === "processing",
  ).length;
  const failed = documents.data?.documents.filter(
    (document) => document.status === "failed",
  ).length;

  return (
    <section className="aw-knowledge-detail">
      <header className="aw-knowledge-detail-header">
        <div>
          <span className="aw-eyebrow">资料库</span>
          <h2>{knowledgeBase.name}</h2>
          <p>{knowledgeBase.description || "没有说明"}</p>
        </div>
        <div className="aw-page-actions">
          <Link
            className="aw-button is-ghost"
            to={`/chat?kb=${encodeURIComponent(knowledgeBase.knowledge_base_id)}`}
          >
            <MessageSquare aria-hidden="true" size={15} />
            在 Chat 中使用
          </Link>
          <Link
            className="aw-button is-ghost"
            to={`/work?kb=${encodeURIComponent(knowledgeBase.knowledge_base_id)}`}
          >
            在 Work 中使用
          </Link>
        </div>
      </header>

      {/* 四张卡，四种状态各占一张，数字排在标签之上。
       *
       * 此前是一条挤在一行里的统计条，四个数字和四个词交替出现，读者要先把它们
       * 两两配对才能读到「1 个索引失败」。分开之后，索引失败那一张自己带红底：
       * 一个知识库里有多少东西是回答不了问题的，是打开这一页最该先看见的事。
       *
       * 「索引失败」为 0 时照样画，不再隐藏。隐藏它省下的是一张卡，代价是读者
       * 分不清「没有失败」和「这一版根本不报失败」。 */}
      <div className="aw-kb-stats">
        <div className="aw-kb-stat">
          <strong>{documents.data?.documents.length ?? knowledgeBase.document_count}</strong>
          <span>文档</span>
        </div>
        <div className="aw-kb-stat is-success">
          <strong>{ready ?? knowledgeBase.ready_document_count}</strong>
          <span>可以检索</span>
        </div>
        <div className="aw-kb-stat is-warning">
          <strong>{processing ?? knowledgeBase.processing_document_count}</strong>
          <span>正在索引</span>
        </div>
        <div className="aw-kb-stat is-danger">
          <strong>{failed ?? knowledgeBase.failed_document_count}</strong>
          <span>索引失败</span>
        </div>
      </div>
      <p className="aw-kb-updated">
        最近更新 {formatDateTime(knowledgeBase.updated_at)}
      </p>

      {knowledgeBase.can_write ? (
        <section className="aw-card aw-knowledge-upload" aria-labelledby="knowledge-upload-title">
          <div className="aw-card-header">
            <div>
              <h3 id="knowledge-upload-title">添加文档</h3>
              {/* 四种格式，和 `adapters/ingestion/parser.py` 的
                  SUPPORTED_MEDIA_TYPES 一一对应。这行字之前只说 PDF 和
                  Markdown，而 .docx 的解析路径、accept 属性、客户端的媒体类型表
                  一直都是齐的——少的只是这句话，于是没有人会去试 Word 文档。 */}
              <p>支持 PDF、Word（.docx）、Markdown 和纯文本。上传后会由文档 Worker 异步解析与索引。</p>
            </div>
            <FileUp aria-hidden="true" size={20} />
          </div>
          <label className="aw-drop-zone">
            <FileUp aria-hidden="true" size={22} />
            <strong>{file === null ? "选择一个文件" : file.name}</strong>
            <span>{file === null ? "PDF / Word / Markdown / 文本" : formatBytes(file.size)}</span>
            <input
              accept=".pdf,.md,.markdown,.docx,.txt,text/plain,text/markdown,application/pdf"
              disabled={upload.isPending}
              onChange={(event) => {
                setUploadNotice(null);
                setFile(event.target.files?.[0] ?? null);
              }}
              type="file"
            />
          </label>
          <label className="aw-field">
            <span>同时授权给（可选）</span>
            <input
              disabled={upload.isPending}
              onChange={(event) => setGrantedTo(event.target.value)}
              placeholder="principal id，多个用逗号分隔"
              value={grantedTo}
            />
          </label>
          <p className="aw-muted">
            只有这里列出的人，加上你自己，能在检索里读到这份文档；留空就只有你自己。
          </p>
          {upload.error === null ? null : <ErrorNotice message={errorMessage(upload.error)} />}
          {uploadNotice === null ? null : (
            <div className="aw-notice is-success" role="status">
              <CheckCircle2 aria-hidden="true" size={16} />
              <span>{uploadNotice}</span>
            </div>
          )}
          <button
            className="aw-button is-primary"
            disabled={file === null || upload.isPending}
            onClick={() => file !== null && upload.mutate(file)}
            type="button"
          >
            {upload.isPending ? <RefreshCw aria-hidden="true" className="aw-spin" size={15} /> : <FileUp aria-hidden="true" size={15} />}
            {upload.isPending ? "正在上传" : "上传并开始索引"}
          </button>
        </section>
      ) : (
        // 入口整块不渲染，而不是渲染成禁用状态：这个知识库是别人分享进来的，
        // 「传完再吃 404」正是这里要消掉的体验——服务端仍然会拒绝，隐藏只是
        // 别让人先把整份文件传上去。
        <section className="aw-card is-evidence aw-knowledge-upload" aria-labelledby="knowledge-readonly-title">
          <div className="aw-card-header">
            <div>
              <h3 id="knowledge-readonly-title">只读知识库</h3>
              <p>这个知识库是别人分享给你阅读的，只有它的创建者可以添加文档。</p>
            </div>
            <Lock aria-hidden="true" size={20} />
          </div>
          <p className="aw-muted">
            你可以照常检索它、在 Chat 和 Work 中引用它；需要新增资料请联系创建者，
            或者在自己的知识库里上传。
          </p>
        </section>
      )}

      <section className="aw-card aw-knowledge-documents" aria-labelledby="document-list-title">
        <div className="aw-card-header">
          <div>
            <h3 id="document-list-title">文档</h3>
            <p>
              按 revision 管理；上传完成不等于已索引，只有“可以检索”的文档会参与
              知识库回答。
            </p>
          </div>
          <button
            aria-label="刷新文档状态"
            className="aw-button is-ghost is-small"
            disabled={documents.isFetching}
            onClick={() => void documents.refetch()}
            type="button"
          >
            <RefreshCw aria-hidden="true" size={14} />
            刷新状态
          </button>
        </div>
        {documents.isPending ? <LoadingLine label="正在读取文档" /> : null}
        {documents.error === null ? null : <ErrorNotice message={errorMessage(documents.error)} />}
        {documents.data?.documents.length === 0 ? (
          <EmptyState
            description="从上方上传第一份 PDF、Word 或 Markdown。"
            icon={<FileText aria-hidden="true" size={21} />}
            title="这个知识库还是空的"
          />
        ) : (
          <div className="aw-document-list">
            {documents.data?.documents.map((document) => (
              <article className="aw-document-row" key={document.document_id}>
                <FileText aria-hidden="true" size={17} />
                <span>
                  <strong>{document.filename || shortId(document.document_id, 24)}</strong>
                  <small>
                    {document.status === "failed"
                      ? failureReason(document.failure_code)
                      : `${document.media_type} · ${formatBytes(document.size_bytes)}`}
                  </small>
                </span>
                {/* 版本。稿子把它列成一栏是对的：撤权与重新导入都体现在这个
                    数字上，而「这条引用还成立吗」比对的正是它。
                    `last_applied_revision` 落后于 `source_revision` 时另说一句
                    ——那是"传完了但索引还是旧的"，两个数字都在接口上，只有它们
                    之间的差说得出这件事。 */}
                <span className="aw-document-revision">
                  <code>rev {document.source_revision}</code>
                  {document.last_applied_revision < document.source_revision ? (
                    <small title={`索引停在 rev ${String(document.last_applied_revision)}`}>
                      索引落后
                    </small>
                  ) : null}
                </span>
                <span className={`aw-document-status is-${document.status}`}>
                  {DOCUMENT_STATUS_LABELS[document.status]}
                </span>
                <time dateTime={document.updated_at}>{formatDateTime(document.updated_at)}</time>
              </article>
            ))}
          </div>
        )}
      </section>

      <details className="aw-card aw-knowledge-debug">
        <summary><FileSearch aria-hidden="true" size={16} />检索调试</summary>
        <p>这是工程验证入口，不影响 Chat 的正常使用。</p>
        <form
          className="aw-inline-form"
          onSubmit={(event: FormEvent<HTMLFormElement>) => {
            event.preventDefault();
            if (query.trim()) search.mutate(query.trim());
          }}
        >
          <input
            aria-label="检索测试问题"
            onChange={(event) => setQuery(event.target.value)}
            placeholder="输入一个问题，查看实际返回的片段"
            value={query}
          />
          <button className="aw-button is-ghost" disabled={!query.trim() || search.isPending} type="submit">
            <Search aria-hidden="true" size={15} />
            测试检索
          </button>
        </form>
        {search.error === null ? null : <ErrorNotice message={errorMessage(search.error)} />}
        <SearchResults result={searchResult} />
      </details>
    </section>
  );
}

function CreateKnowledgeBaseDialog({
  identity,
  onClose,
  onCreated,
}: {
  identity: ReturnType<typeof useIdentity>["identity"];
  onClose: () => void;
  onCreated: (knowledgeBase: KnowledgeBaseView) => void;
}) {
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const dialogRef = useRef<HTMLElement>(null);
  const nameRef = useRef<HTMLInputElement>(null);
  const create = useMutation({
    mutationFn: () => createKnowledgeBase(identity, { name: name.trim(), description }),
    onSuccess: onCreated,
  });
  useEffect(() => {
    // AppShell closes the mobile workspace drawer with its own focus-restoring
    // animation frame. Queue the dialog focus after mount so it wins that handoff.
    const frame = window.requestAnimationFrame(() => nameRef.current?.focus());
    return () => window.cancelAnimationFrame(frame);
  }, []);
  const handleKeyDown = (event: ReactKeyboardEvent<HTMLElement>) => {
    if (event.key === "Escape") {
      event.preventDefault();
      event.stopPropagation();
      onClose();
      return;
    }
    if (event.key !== "Tab" || dialogRef.current === null) return;
    const focusable = Array.from(
      dialogRef.current.querySelectorAll<HTMLElement>(
        'a[href], button:not([disabled]), input:not([disabled]), textarea:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])',
      ),
    );
    const first = focusable[0];
    const last = focusable.at(-1);
    if (first === undefined || last === undefined) return;
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  };
  return (
    <div className="aw-dialog-backdrop" role="presentation">
      <section
        aria-describedby="aw-create-knowledge-description"
        aria-labelledby="aw-create-knowledge-title"
        aria-modal="true"
        className="aw-dialog"
        onKeyDown={handleKeyDown}
        ref={dialogRef}
        role="dialog"
      >
        <header>
          <div>
            <h2 id="aw-create-knowledge-title">新建知识库</h2>
            <p id="aw-create-knowledge-description">
              给资料起一个人能看懂的名字；ID 由系统生成。
            </p>
          </div>
          <button
            aria-label="关闭新建知识库"
            className="aw-icon-button"
            onClick={onClose}
            type="button"
          >
            <X aria-hidden="true" size={17} />
          </button>
        </header>
        <form
          className="aw-form-stack"
          onSubmit={(event) => {
            event.preventDefault();
            if (name.trim()) create.mutate();
          }}
        >
          <label className="aw-field">
            <span>名称</span>
            <input
              autoFocus
              maxLength={120}
              onChange={(event) => setName(event.target.value)}
              placeholder="例如：校招项目资料"
              ref={nameRef}
              required
              value={name}
            />
          </label>
          <label className="aw-field">
            <span>说明（可选）</span>
            <textarea maxLength={500} onChange={(event) => setDescription(event.target.value)} placeholder="这里放哪些资料、准备解决什么问题" rows={3} value={description} />
          </label>
          {create.error === null ? null : <ErrorNotice message={errorMessage(create.error)} />}
          <footer className="aw-button-row">
            <button className="aw-button is-ghost" onClick={onClose} type="button">取消</button>
            <button className="aw-button is-primary" disabled={!name.trim() || create.isPending} type="submit">
              {create.isPending ? "正在创建" : "创建知识库"}
            </button>
          </footer>
        </form>
      </section>
    </div>
  );
}

function SearchResults({ result }: { result: SearchResponse | null }) {
  if (result === null) return null;
  return (
    <>
      {/* 只说检索栈叫什么，不说这一次做了什么。后端那个字段报的是检索栈被配置成
          了什么样子，单次调用里重排到底跑没跑在 AuthorizedContext 上，没有出现在
          搜索响应里——写成「本次已重排」就是替一个拿不到的事实下结论。同一批片段
          在 dense 和 hybrid+rerank 下含义不同，所以名字本身值得摆出来。 */}
      <p className="aw-muted">由 {result.retriever} 检索栈应答</p>
      {result.hits.length === 0 ? (
        <>
          <p className="aw-muted">本次没有返回可读的匹配片段。</p>
          {/* 空结果是三件事共用的一个样子，这里说清楚是哪三件。
              授权过滤跑在检索之后（application/retrieval.py 的
              `authorized_revisions`），所以被过滤掉的片段和从来不存在的片段
              在这条响应里长得完全一样；还在索引中的文档同样什么都不返回。
              合并的后果是：这个入口回答不了「有没有一份我读不到的文档命中了」
              ——而那正是一个枚举权限的问题。 */}
          <p className="aw-muted">
            空结果不区分三件事：没有匹配、还没索引完、以及你无权读。授权过滤发生
            在检索之后，被过滤掉的片段和不存在的片段在这里长得一样——所以这个入口
            也回答不了“是不是有我读不到的东西命中了”。
          </p>
        </>
      ) : (
        <div className="aw-result-list">
          {result.hits.map((hit, index) => (
            <article className="aw-result-card" key={hit.chunk_id}>
              <header>
                <span className="aw-step-marker">{index + 1}</span>
                <div><strong>{shortId(hit.document_id, 20)}</strong><span>{shortId(hit.chunk_id, 20)}</span></div>
              </header>
              <p>{hit.text}</p>
            </article>
          ))}
        </div>
      )}
    </>
  );
}

// 逗号、全角逗号、空白都当分隔符，空项丢掉：服务端的 principal id 是
// min_length=1 且有字符集约束的，一个手滑留下的尾逗号会让 /complete 吃 422，
// 而那时文件的字节已经传完了——退回来的只是一次白传。
function parseGrantedPrincipals(raw: string): string[] {
  return raw.split(/[,，\s]+/).filter((principal) => principal !== "");
}

const DOCUMENT_STATUS_LABELS: Record<KnowledgeDocumentStatus, string> = {
  ready: "可以检索",
  processing: "正在索引",
  failed: "索引失败",
};

// 只翻译摄取真的会产出的那几个码。给不认识的码编一个具体理由比说不知道更糟：
// 用户会照着那句话去改文件，然后再失败一次。
const FAILURE_REASONS: Record<string, string> = {
  invalid_tool_input: "文件内容无法解析，请换成可读的 PDF、Word 或 Markdown 重新上传。",
  not_found: "找不到上传时保存的原始文件，请重新上传一次。",
};

const FAILURE_FALLBACK = "解析没有完成，系统仍会重试；持续失败请联系管理员。";

function failureReason(code: string | null): string {
  if (code === null) return FAILURE_FALLBACK;
  return FAILURE_REASONS[code] ?? FAILURE_FALLBACK;
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : "请求失败，请稍后重试。";
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}
