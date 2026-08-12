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

/**
 * Files added beside a question, and what that actually does.
 *
 * **This is not an attachment.** There is no per-request attachment in this
 * system: a Chat or Task request carries a `knowledge_base_id` and nothing
 * else, so a file added here is *uploaded into the selected knowledge base*,
 * indexed there, and stays there afterwards -- visible to anything else that
 * later selects the same knowledge base, including other people it is shared
 * with.
 *
 * The word "附件" and a paperclip promise the opposite of that: something
 * scoped to one message and gone when the message is sent. The copy below
 * therefore says "加入知识库" rather than "附件", and the remove control says
 * what it can actually do, which is take the file out of *this* list. It
 * cannot delete the uploaded document, because nothing in the system can:
 * there is no delete on the documents route, the document store port or its
 * Postgres adapter, and the chunks are in Qdrant besides. Removing the chip
 * and calling it removal is the misreading this component used to invite.
 */
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

const ACCEPTED_EXTENSIONS = [".pdf", ".md", ".markdown", ".docx"];
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
        aria-label="上传文件到知识库"
        className="aw-attachment-button"
        disabled={disabled}
        onClick={() => ref.current?.click()}
        // Says where the file goes, because it goes somewhere permanent. The
        // old label ("添加附件") described a per-message attachment this system
        // does not have.
        title="上传 PDF 或 Markdown 到所选知识库（最多 5 个，上传后会一直保留）"
        type="button"
      >
        <Paperclip aria-hidden="true" size={17} />
      </button>
      <input
        accept=".pdf,.md,.markdown,.docx,text/markdown,application/pdf"
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
  const anyUploaded = items.some(
    (item) => item.state === "indexing" || item.state === "ready",
  );
  return (
    <div className="aw-attachment-tray" aria-label="要加入知识库的文件">
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
            <button
              // Named for what it does. An uploaded file is in the knowledge
              // base and stays there; this only stops listing it here, and a
              // button labelled "移除" said otherwise.
              aria-label={
                item.state === "waiting_for_source" || item.state === "failed"
                  ? `不再上传 ${item.file.name}`
                  : `从这个列表中移除 ${item.file.name}（文件仍在知识库中）`
              }
              onClick={() => onRemove(item.localId)}
              type="button"
            >
              <X aria-hidden="true" size={14} />
            </button>
          )}
        </div>
      ))}
      {anyUploaded ? (
        <p className="aw-attachment-note">
          这些文件已经上传到所选知识库并会一直保留；这里的 ×
          只是不再列出它，不会删除已上传的文档。要管理它们，去「知识库」页面。
        </p>
      ) : null}
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
  if (item.state === "waiting_for_source") return "请先选择知识库，才能上传";
  if (item.state === "uploading") return "正在上传到知识库";
  if (item.state === "indexing") return "正在建立索引，完成后才能检索到";
  // Says where it went and that it stays. "已加入知识库，可以使用" was true and
  // still read like a per-message attachment.
  if (item.state === "ready") return "已存入知识库（会一直保留）";
  return item.error ?? "上传失败";
}
