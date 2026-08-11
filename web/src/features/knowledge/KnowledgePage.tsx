import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  BookOpen,
  CheckCircle2,
  FileSearch,
  FileText,
  FileUp,
  Library,
  MessageSquare,
  Plus,
  RefreshCw,
  Search,
  X,
} from "lucide-react";
import { type FormEvent, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import {
  createKnowledgeBase,
  listKnowledgeBaseDocuments,
  searchKnowledge,
  uploadDocument,
} from "../../api/client";
import type {
  KnowledgeBaseView,
  SearchResponse,
} from "../../api/types";
import { useIdentity } from "../../app/IdentityContext";
import {
  knowledgeBaseQueryKey,
  useKnowledgeBases,
} from "../../components/KnowledgeSourcePicker";
import {
  EmptyState,
  ErrorNotice,
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
  };

  return (
    <main className="aw-utility-page aw-knowledge-page">
      <header className="aw-page-header">
        <div>
          <span className="aw-eyebrow">Knowledge</span>
          <h1>知识库</h1>
          <p>集中查看已经创建的资料库、文档处理状态，并把它们带到 Chat 或 Work 中使用。</p>
        </div>
        <div className="aw-page-actions">
          <button
            className="aw-button is-ghost"
            disabled={knowledgeBases.isFetching}
            onClick={() => void knowledgeBases.refetch()}
            type="button"
          >
            <RefreshCw aria-hidden="true" size={15} />
            刷新
          </button>
          <button
            className="aw-button is-primary"
            onClick={() => setCreateOpen(true)}
            type="button"
          >
            <Plus aria-hidden="true" size={16} />
            新建知识库
          </button>
        </div>
      </header>

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
              onClick={() => setCreateOpen(true)}
              type="button"
            >
              <Plus aria-hidden="true" size={16} />
              创建第一个知识库
            </button>
          }
          description="创建知识库后上传 PDF 或 Markdown；文档完成索引后即可在 Chat 和 Work 中选择。"
          icon={<Library aria-hidden="true" size={24} />}
          title="还没有知识库"
        />
      ) : null}

      {(knowledgeBases.data?.knowledge_bases.length ?? 0) > 0 ? (
        <div className="aw-knowledge-layout">
          <aside className="aw-knowledge-list" aria-label="知识库列表">
            {knowledgeBases.data?.knowledge_bases.map((knowledgeBase) => (
              <KnowledgeBaseRow
                active={knowledgeBase.knowledge_base_id === selectedId}
                key={knowledgeBase.knowledge_base_id}
                knowledgeBase={knowledgeBase}
                onClick={() => selectKnowledgeBase(knowledgeBase.knowledge_base_id)}
              />
            ))}
          </aside>
          {selected === undefined ? (
            <EmptyState
              description="从左侧选择一个知识库查看文档。"
              icon={<BookOpen aria-hidden="true" size={22} />}
              title="选择知识库"
            />
          ) : (
            // Keyed by the knowledge base, so switching remounts rather than
            // re-renders. The panel holds a chosen file, an upload notice, a
            // query and its results, and every one of those is about one
            // knowledge base: without the key, a file picked in A uploads into
            // B -- the mutation reads the id when it fires, not when the file
            // was chosen -- and A's search results sit under B's heading.
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
        </div>
      ) : null}

      {createOpen ? (
        <CreateKnowledgeBaseDialog
          identity={identity}
          onClose={() => setCreateOpen(false)}
          onCreated={(knowledgeBase) => {
            setCreateOpen(false);
            void queryClient.invalidateQueries({
              queryKey: knowledgeBaseQueryKey(identity),
            });
            selectKnowledgeBase(knowledgeBase.knowledge_base_id);
          }}
        />
      ) : null}
    </main>
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
        grantedPrincipals: [],
      }),
    onSuccess: async () => {
      setFile(null);
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

      <div className="aw-knowledge-summary">
        <span><strong>{documents.data?.documents.length ?? knowledgeBase.document_count}</strong>文档</span>
        <span><strong>{ready ?? knowledgeBase.ready_document_count}</strong>可以检索</span>
        <span><strong>{processing ?? knowledgeBase.processing_document_count}</strong>处理中</span>
        <span><strong>{formatDateTime(knowledgeBase.updated_at)}</strong>最近更新</span>
      </div>

      <section className="aw-card aw-knowledge-upload" aria-labelledby="knowledge-upload-title">
        <div className="aw-card-header">
          <div>
            <h3 id="knowledge-upload-title">添加文档</h3>
            <p>支持 PDF 和 Markdown。上传后会由文档 Worker 异步解析与索引。</p>
          </div>
          <FileUp aria-hidden="true" size={20} />
        </div>
        <label className="aw-drop-zone">
          <FileUp aria-hidden="true" size={22} />
          <strong>{file === null ? "选择一个文件" : file.name}</strong>
          <span>{file === null ? "PDF / Markdown" : formatBytes(file.size)}</span>
          <input
            accept=".pdf,.md,.markdown,text/markdown,application/pdf"
            disabled={upload.isPending}
            onChange={(event) => {
              setUploadNotice(null);
              setFile(event.target.files?.[0] ?? null);
            }}
            type="file"
          />
        </label>
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

      <section className="aw-card aw-knowledge-documents" aria-labelledby="document-list-title">
        <div className="aw-card-header">
          <div>
            <h3 id="document-list-title">文档</h3>
            <p>只有“可以检索”的文档会参与知识库回答。</p>
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
            description="从上方上传第一份 PDF 或 Markdown。"
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
                  <small>{document.media_type} · {formatBytes(document.size_bytes)}</small>
                </span>
                <span className={`aw-document-status is-${document.status}`}>
                  {document.status === "ready" ? "可以检索" : "正在索引"}
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
  const create = useMutation({
    mutationFn: () => createKnowledgeBase(identity, { name: name.trim(), description }),
    onSuccess: onCreated,
  });
  return (
    <div className="aw-dialog-backdrop" role="presentation">
      <section aria-modal="true" className="aw-dialog" role="dialog">
        <header>
          <div>
            <h2>新建知识库</h2>
            <p>给资料起一个人能看懂的名字；ID 由系统生成。</p>
          </div>
          <button aria-label="关闭" className="aw-icon-button" onClick={onClose} type="button">
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
            <input autoFocus maxLength={120} onChange={(event) => setName(event.target.value)} placeholder="例如：校招项目资料" required value={name} />
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
  if (result.hits.length === 0) {
    return <p className="aw-muted">本次没有返回可读的匹配片段。</p>;
  }
  return (
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
  );
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : "请求失败，请稍后重试。";
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}
