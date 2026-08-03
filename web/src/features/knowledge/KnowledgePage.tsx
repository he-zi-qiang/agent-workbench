import { useMutation } from "@tanstack/react-query";
import {
  CheckCircle2,
  FileSearch,
  FileUp,
  Library,
  Search,
} from "lucide-react";
import { type FormEvent, useMemo, useState } from "react";
import {
  searchKnowledge,
  uploadDocument,
} from "../../api/client";
import type {
  DocumentVersion,
  SearchResponse,
} from "../../api/types";
import { useIdentity } from "../../app/IdentityContext";
import {
  EmptyState,
  ErrorNotice,
  LoadingLine,
  shortId,
} from "../../components/ui";

const DEFAULT_KNOWLEDGE_BASE = "kb_local";

export function KnowledgePage() {
  const { identity } = useIdentity();
  const [file, setFile] = useState<File | null>(null);
  const [documentId, setDocumentId] = useState("");
  const [knowledgeBaseId, setKnowledgeBaseId] = useState(DEFAULT_KNOWLEDGE_BASE);
  const [grantedPrincipals, setGrantedPrincipals] = useState(identity.principalId);
  const [completedVersion, setCompletedVersion] =
    useState<DocumentVersion | null>(null);

  const [query, setQuery] = useState("");
  const [searchKnowledgeBaseId, setSearchKnowledgeBaseId] = useState(
    DEFAULT_KNOWLEDGE_BASE,
  );
  const [topK, setTopK] = useState(8);
  const [searchResult, setSearchResult] = useState<SearchResponse | null>(null);
  const upload = useMutation({
    mutationFn: (input: Parameters<typeof uploadDocument>[1]) =>
      uploadDocument(identity, input),
    onSuccess: (version) => {
      setCompletedVersion(version);
      setFile(null);
    },
  });

  const search = useMutation({
    mutationFn: (input: Parameters<typeof searchKnowledge>[1]) =>
      searchKnowledge(identity, input),
    onSuccess: setSearchResult,
  });

  const principals = useMemo(
    () =>
      grantedPrincipals
        .split(",")
        .map((value) => value.trim())
        .filter(Boolean),
    [grantedPrincipals],
  );

  const submitUpload = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (file === null || !documentId.trim() || !knowledgeBaseId.trim()) return;
    setCompletedVersion(null);
    upload.mutate({
      file,
      documentId: documentId.trim(),
      knowledgeBaseId: knowledgeBaseId.trim(),
      grantedPrincipals: principals,
    });
  };

  const submitSearch = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!query.trim() || !searchKnowledgeBaseId.trim()) return;
    setSearchResult(null);
    search.mutate({
      query: query.trim(),
      knowledgeBaseId: searchKnowledgeBaseId.trim(),
      topK: Math.max(1, Math.min(50, topK)),
    });
  };

  return (
    <main className="aw-utility-page">
      <header className="aw-page-header">
        <div>
          <span className="aw-eyebrow">Knowledge</span>
          <h1>知识库</h1>
          <p>上传真实文档，随后用当前身份检查检索器实际返回的上下文。</p>
        </div>
        <div className="aw-page-note">
          <Library aria-hidden="true" size={17} />
          <span>上传完成、异步索引和检索可见是三个不同状态。</span>
        </div>
      </header>

      <div className="aw-card-grid">
        <section className="aw-card aw-section" aria-labelledby="upload-title">
          <div className="aw-card-header">
            <div>
              <span className="aw-eyebrow">Ingestion</span>
              <h2 id="upload-title">上传文档</h2>
            </div>
            <FileUp aria-hidden="true" size={20} />
          </div>

          <ol
            aria-busy={upload.isPending}
            aria-label="上传协议"
            className={`aw-upload-steps ${upload.isPending ? "is-running" : ""}`}
          >
            <li className={uploadStepClass(completedVersion)}>
              <span className="aw-step-marker">1</span>
              <div>
                <strong>创建上传意图</strong>
                <span>声明文件大小、摘要、类型和文件名</span>
              </div>
            </li>
            <li className={uploadStepClass(completedVersion)}>
              <span className="aw-step-marker">2</span>
              <div>
                <strong>传输原始字节</strong>
                <span>PUT 到服务端返回的数据面路径</span>
              </div>
            </li>
            <li className={uploadStepClass(completedVersion)}>
              <span className="aw-step-marker">3</span>
              <div>
                <strong>完成文档版本</strong>
                <span>校验 Artifact，并登记 ACL 与知识库归属</span>
              </div>
            </li>
          </ol>

          <form className="aw-form-stack" onSubmit={submitUpload}>
            <label className="aw-field">
              <span>文档文件</span>
              <input
                accept=".pdf,.md,.markdown,text/markdown,application/pdf"
                disabled={upload.isPending}
                key={completedVersion?.version_id ?? "pending-upload"}
                onChange={(event) => {
                  setCompletedVersion(null);
                  setFile(event.target.files?.[0] ?? null);
                }}
                required
                type="file"
              />
              <small>{file === null ? "PDF 或 Markdown" : `${file.name} · ${formatBytes(file.size)}`}</small>
            </label>

            <div className="aw-inline-fields">
              <label className="aw-field">
                <span>Document ID</span>
                <input
                  disabled={upload.isPending}
                  onChange={(event) => setDocumentId(event.target.value)}
                  placeholder="doc_handbook"
                  required
                  value={documentId}
                />
              </label>
              <label className="aw-field">
                <span>Knowledge Base ID</span>
                <input
                  disabled={upload.isPending}
                  onChange={(event) => setKnowledgeBaseId(event.target.value)}
                  required
                  value={knowledgeBaseId}
                />
              </label>
            </div>

            <label className="aw-field">
              <span>可读 Principal（逗号分隔）</span>
              <input
                disabled={upload.isPending}
                onChange={(event) => setGrantedPrincipals(event.target.value)}
                value={grantedPrincipals}
              />
              <small>ACL 由服务端保存；搜索时还会使用当前身份重新校验。</small>
            </label>

            {upload.error !== null && <ErrorNotice message={errorMessage(upload.error)} />}
            {upload.isPending && (
              <LoadingLine label="正在顺序执行创建、传输和 complete" />
            )}
            {completedVersion !== null && (
              <div className="aw-notice is-success" role="status">
                <CheckCircle2 aria-hidden="true" size={16} />
                <span>
                  文档版本 {shortId(completedVersion.version_id)} 已登记；这不代表 Worker
                  已完成解析和索引，请用右侧搜索验证可检索状态。
                </span>
              </div>
            )}

            <button
              className="aw-button is-primary"
              disabled={
                upload.isPending ||
                file === null ||
                !documentId.trim() ||
                !knowledgeBaseId.trim()
              }
              type="submit"
            >
              <FileUp aria-hidden="true" size={16} />
              执行三段上传
            </button>
          </form>
        </section>

        <section className="aw-card aw-section" aria-labelledby="search-title">
          <div className="aw-card-header">
            <div>
              <span className="aw-eyebrow">Retrieval probe</span>
              <h2 id="search-title">检索检查</h2>
            </div>
            <FileSearch aria-hidden="true" size={20} />
          </div>

          <form className="aw-form-stack" onSubmit={submitSearch}>
            <label className="aw-field">
              <span>问题</span>
              <textarea
                disabled={search.isPending}
                onChange={(event) => setQuery(event.target.value)}
                placeholder="输入要在文档中查找的问题"
                required
                rows={3}
                value={query}
              />
            </label>
            <div className="aw-inline-fields">
              <label className="aw-field">
                <span>Knowledge Base ID</span>
                <input
                  disabled={search.isPending}
                  onChange={(event) => setSearchKnowledgeBaseId(event.target.value)}
                  required
                  value={searchKnowledgeBaseId}
                />
              </label>
              <label className="aw-field">
                <span>Top K</span>
                <input
                  disabled={search.isPending}
                  max={50}
                  min={1}
                  onChange={(event) => setTopK(Number(event.target.value))}
                  required
                  type="number"
                  value={topK}
                />
              </label>
            </div>
            {search.error !== null && <ErrorNotice message={errorMessage(search.error)} />}
            <button
              className="aw-button is-primary"
              disabled={search.isPending || !query.trim() || !searchKnowledgeBaseId.trim()}
              type="submit"
            >
              <Search aria-hidden="true" size={16} />
              {search.isPending ? "正在检索" : "检查检索结果"}
            </button>
          </form>

          <SearchResults result={searchResult} />
        </section>
      </div>
    </main>
  );
}

function SearchResults({ result }: { result: SearchResponse | null }) {
  if (result === null) {
    return (
      <EmptyState
        description="搜索直接调用 /v1/search；这里不会用示例片段填充结果。"
        icon={<Search aria-hidden="true" size={20} />}
        title="尚未运行检索"
      />
    );
  }

  if (result.hits.length === 0) {
    return (
      <div className="aw-notice is-warning" role="status">
        <span>
          当前身份没有获得可返回的匹配片段。空结果只说明本次检索为空，不能据此推断文档不存在、
          尚未索引，或调用者没有权限。
        </span>
      </div>
    );
  }

  return (
    <div className="aw-result-list" aria-live="polite">
      <div className="aw-section-header">
        <div>
          <strong>{result.hits.length} 个上下文片段</strong>
          <span>Retriever：{result.retriever}</span>
        </div>
      </div>
      {result.hits.map((hit, index) => {
        const citation = result.citations.find(
          (candidate) => candidate.chunk_id === hit.chunk_id,
        );
        return (
          <article className="aw-result-card" key={hit.chunk_id}>
            <header>
              <span className="aw-step-marker">{index + 1}</span>
              <div>
                <strong>{shortId(hit.document_id, 18)}</strong>
                <span>
                  {shortId(hit.document_version, 16)}
                  {citation?.locator.page != null
                    ? ` · 第 ${citation.locator.page} 页`
                    : ""}
                </span>
              </div>
            </header>
            <p>{hit.text}</p>
            <code className="aw-code-value">{hit.chunk_id}</code>
          </article>
        );
      })}
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

function uploadStepClass(completedVersion: DocumentVersion | null): string {
  if (completedVersion !== null) return "is-complete";
  return "is-muted";
}
