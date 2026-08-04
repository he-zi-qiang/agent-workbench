import { useQuery } from "@tanstack/react-query";
import { BookOpen, ExternalLink } from "lucide-react";
import { Link } from "react-router-dom";
import { listKnowledgeBases } from "../api/client";
import type { KnowledgeBaseView, PrincipalIdentity } from "../api/types";

export function knowledgeBaseQueryKey(identity: PrincipalIdentity) {
  return [
    "knowledge-bases",
    identity.tenantId,
    identity.principalId,
    [...identity.scopes].sort().join(","),
  ] as const;
}

export function useKnowledgeBases(identity: PrincipalIdentity) {
  return useQuery({
    queryKey: knowledgeBaseQueryKey(identity),
    queryFn: ({ signal }) => listKnowledgeBases(identity, signal),
    staleTime: 10_000,
  });
}

export function KnowledgeSourcePicker({
  identity,
  value,
  onChange,
  disabled = false,
  compact = false,
  manageLink = true,
}: {
  identity: PrincipalIdentity;
  value: string | null;
  onChange: (knowledgeBase: KnowledgeBaseView | null) => void;
  disabled?: boolean;
  compact?: boolean;
  manageLink?: boolean;
}) {
  const query = useKnowledgeBases(identity);
  const knowledgeBases = query.data?.knowledge_bases ?? [];
  const selectedExists =
    value === null || knowledgeBases.some((item) => item.knowledge_base_id === value);

  return (
    <div className={`aw-source-picker ${compact ? "is-compact" : ""}`}>
      <BookOpen aria-hidden="true" size={15} />
      <label>
        <span className="aw-sr-only">回答资料</span>
        <select
          aria-label="回答资料"
          disabled={disabled || query.isPending}
          onChange={(event) => {
            const id = event.target.value;
            onChange(
              id === ""
                ? null
                : knowledgeBases.find((item) => item.knowledge_base_id === id) ?? null,
            );
          }}
          value={selectedExists ? (value ?? "") : ""}
        >
          <option value="">不使用知识库 · 自由回答</option>
          {knowledgeBases.map((item) => (
            <option key={item.knowledge_base_id} value={item.knowledge_base_id}>
              {item.name} · {item.ready_document_count}/{item.document_count} 可用
            </option>
          ))}
        </select>
      </label>
      {manageLink ? (
        <Link
          aria-label="管理知识库"
          className="aw-source-manage"
          title="管理知识库"
          to="/knowledge"
        >
          <ExternalLink aria-hidden="true" size={14} />
        </Link>
      ) : null}
      {query.error === null ? null : (
        <small className="aw-source-error">知识库列表暂时不可用，仍可自由回答。</small>
      )}
    </div>
  );
}

