import { FileText, LoaderCircle, Paperclip, RotateCcw, X } from "lucide-react";
import {
  type ChangeEvent,
  type RefObject,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import {
  listKnowledgeBaseDocuments,
  uploadDocument,
} from "../api/client";
import type { PrincipalIdentity } from "../api/types";

export type AttachmentState =
  | "waiting_for_source"
  | "uploading"
  | "indexing"
  | "ready"
  | "failed";

export interface KnowledgeAttachment {
  localId: string;
  documentId: string;
  file: File;
  state: AttachmentState;
  error?: string;
}

const ACCEPTED_EXTENSIONS = [".pdf", ".md", ".markdown"];
const MAX_ATTACHMENTS = 5;

export function useKnowledgeAttachments(
  identity: PrincipalIdentity,
  knowledgeBaseId: string | null,
) {
  const [items, setItems] = useState<KnowledgeAttachment[]>([]);
  const mounted = useRef(true);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  const startUpload = useCallback(
    (target: KnowledgeAttachment) => {
      if (knowledgeBaseId === null) return;
      setItems((current) =>
        current.map((item) => {
          if (item.localId !== target.localId || !canStart(item.state)) return item;
          return withoutError({ ...item, state: "uploading" });
        }),
      );
      void uploadDocument(identity, {
        file: target.file,
        documentId: target.documentId,
        knowledgeBaseId,
        grantedPrincipals: [],
      })
        .then(() => {
          if (!mounted.current) return;
          setItems((current) =>
            current.map((item) =>
              item.localId === target.localId
                ? withoutError({ ...item, state: "indexing" })
                : item,
            ),
          );
        })
        .catch((error: unknown) => {
          if (!mounted.current) return;
          setItems((current) =>
            current.map((item) =>
              item.localId === target.localId
                ? {
                    ...item,
                    state: "failed",
                    error: error instanceof Error ? error.message : "上传失败",
                  }
                : item,
            ),
          );
        });
    },
    [identity, knowledgeBaseId],
  );

  useEffect(() => {
    if (knowledgeBaseId === null) return;
    const timer = window.setTimeout(() => {
      for (const item of items) {
        if (item.state === "waiting_for_source") startUpload(item);
      }
    }, 0);
    return () => window.clearTimeout(timer);
  }, [items, knowledgeBaseId, startUpload]);

  useEffect(() => {
    if (knowledgeBaseId === null || !items.some((item) => item.state === "indexing")) {
      return;
    }
    let cancelled = false;
    const refresh = async () => {
      try {
        const response = await listKnowledgeBaseDocuments(identity, knowledgeBaseId);
        if (cancelled || !mounted.current) return;
        const readyIds = new Set(
          response.documents
            .filter((document) => document.status === "ready")
            .map((document) => document.document_id),
        );
        setItems((current) =>
          current.map((item) =>
            item.state === "indexing" && readyIds.has(item.documentId)
              ? { ...item, state: "ready" }
              : item,
          ),
        );
      } catch {
        // Indexing is asynchronous and the list endpoint can be briefly
        // unavailable during startup. Keep polling; the upload itself already
        // owns the actionable error path.
      }
    };
    void refresh();
    const interval = window.setInterval(() => void refresh(), 2_000);
    return () => {
      cancelled = true;
      window.clearInterval(interval);
    };
  }, [identity, items, knowledgeBaseId]);

  const addFiles = useCallback((files: File[]) => {
    setItems((current) => {
      const available = Math.max(0, MAX_ATTACHMENTS - current.length);
      const additions = files
        .filter(isAcceptedFile)
        .slice(0, available)
        .map((file): KnowledgeAttachment => ({
          localId: `attachment:${crypto.randomUUID()}`,
          documentId: `doc_${crypto.randomUUID().replaceAll("-", "")}`,
          file,
          state: "waiting_for_source",
        }));
      return [...current, ...additions];
    });
  }, []);

  const remove = useCallback((localId: string) => {
    setItems((current) => current.filter((item) => item.localId !== localId));
  }, []);
  const retry = useCallback(
    (localId: string) => {
      const item = items.find((candidate) => candidate.localId === localId);
      if (item !== undefined) startUpload(item);
    },
    [items, startUpload],
  );
  const clear = useCallback(() => setItems([]), []);
  const hasBlockingItems = items.some((item) => item.state !== "ready");

  return useMemo(
    () => ({ items, addFiles, remove, retry, clear, hasBlockingItems }),
    [addFiles, clear, hasBlockingItems, items, remove, retry],
  );
}

export function AttachmentButton({
  inputRef,
  disabled = false,
  onFiles,
}: {
  inputRef?: RefObject<HTMLInputElement | null>;
  disabled?: boolean;
  onFiles: (files: File[]) => void;
}) {
  const ownRef = useRef<HTMLInputElement>(null);
  const ref = inputRef ?? ownRef;
  const chooseFiles = (event: ChangeEvent<HTMLInputElement>) => {
    onFiles(Array.from(event.target.files ?? []));
    event.target.value = "";
  };
  return (
    <span className="aw-attachment-control">
      <button
        aria-label="添加附件"
        className="aw-attachment-button"
        disabled={disabled}
        onClick={() => ref.current?.click()}
        title="添加 PDF 或 Markdown（最多 5 个）"
        type="button"
      >
        <Paperclip aria-hidden="true" size={17} />
      </button>
      <input
        accept=".pdf,.md,.markdown,text/markdown,application/pdf"
        className="aw-sr-only"
        disabled={disabled}
        multiple
        onChange={chooseFiles}
        ref={ref}
        tabIndex={-1}
        type="file"
      />
    </span>
  );
}

export function AttachmentTray({
  items,
  onRemove,
  onRetry,
}: {
  items: KnowledgeAttachment[];
  onRemove: (localId: string) => void;
  onRetry: (localId: string) => void;
}) {
  if (items.length === 0) return null;
  return (
    <div className="aw-attachment-tray" aria-label="附件">
      {items.map((item) => (
        <div className={`aw-attachment is-${item.state}`} key={item.localId}>
          {item.state === "uploading" || item.state === "indexing" ? (
            <LoaderCircle aria-hidden="true" className="aw-spin" size={15} />
          ) : (
            <FileText aria-hidden="true" size={15} />
          )}
          <span>
            <strong>{item.file.name}</strong>
            <small>{attachmentStateLabel(item)}</small>
          </span>
          {item.state === "failed" ? (
            <button aria-label={`重试 ${item.file.name}`} onClick={() => onRetry(item.localId)} type="button">
              <RotateCcw aria-hidden="true" size={14} />
            </button>
          ) : null}
          {item.state === "uploading" ? null : (
            <button aria-label={`移除 ${item.file.name}`} onClick={() => onRemove(item.localId)} type="button">
              <X aria-hidden="true" size={14} />
            </button>
          )}
        </div>
      ))}
    </div>
  );
}

function canStart(state: AttachmentState): boolean {
  return state === "waiting_for_source" || state === "failed";
}

function isAcceptedFile(file: File): boolean {
  const name = file.name.toLowerCase();
  return ACCEPTED_EXTENSIONS.some((extension) => name.endsWith(extension));
}

function withoutError(item: KnowledgeAttachment): KnowledgeAttachment {
  const next = { ...item };
  delete next.error;
  return next;
}

function attachmentStateLabel(item: KnowledgeAttachment): string {
  if (item.state === "waiting_for_source") return "请选择知识库后上传";
  if (item.state === "uploading") return "正在上传";
  if (item.state === "indexing") return "正在建立索引，完成后才能使用";
  if (item.state === "ready") return "已加入知识库，可以使用";
  return item.error ?? "上传失败";
}
