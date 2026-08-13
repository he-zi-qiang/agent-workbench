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
import { useKnowledgeBases } from "./KnowledgeSourcePicker";

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
  /**
   * Whether the bytes reached the knowledge base, regardless of what happened
   * afterwards.
   *
   * Not derivable from `state` any more, and that is the point: a document
   * whose *indexing* was refused is `failed` and is also in the knowledge base
   * for good. Reading "failed" as "nothing was uploaded" is what would make
   * this component's × button start lying again -- it would offer to stop an
   * upload that already finished.
   */
  uploaded: boolean;
}

const ACCEPTED_EXTENSIONS = [".pdf", ".md", ".markdown", ".docx", ".txt"];
const MAX_ATTACHMENTS = 5;

export function useKnowledgeAttachments(
  identity: PrincipalIdentity,
  knowledgeBaseId: string | null,
) {
  const [items, setItems] = useState<KnowledgeAttachment[]>([]);
  const mounted = useRef(true);
  // Every upload that has left and not yet returned, keyed by the chip it
  // belongs to. A ref rather than state because what gets aborted is a request
  // already on the wire: waiting for a render to hand the controller over is
  // waiting for exactly the window this exists to close.
  const inFlight = useRef(new Map<string, AbortController>());

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  // Shares the picker's query, so this costs no extra request on either page:
  // both already render a `KnowledgeSourcePicker` against the same key.
  const knowledgeBases = useKnowledgeBases(identity);
  const selected = knowledgeBases.data?.knowledge_bases.find(
    (base) => base.knowledge_base_id === knowledgeBaseId,
  );
  /**
   * Why a file cannot be put in the selected base, or null when one can.
   *
   * Read here rather than at each call site so that Chat and Work cannot drift:
   * "this base is read-only" is a fact about the base, not about one paperclip.
   * It decides what to *offer* and nothing else -- the server's
   * `require_writable` still refuses, and this only stops a reader from picking
   * a file, watching it upload and being told no at the end.
   *
   * A base that is not in the list is unknown, not refused, and unknown must
   * not block: it means the list is still loading or does not surface that base,
   * and guessing "no" would hide an upload that would have succeeded.
   */
  const readOnlyReason =
    selected !== undefined && !selected.can_write
      ? "这个知识库对你是只读的，不能上传文件"
      : null;

  /**
   * Abort every upload still on the wire, saying why.
   *
   * The reason travels with the abort rather than being applied to the chips
   * here, because the chip that needs it is the one whose request is still
   * unwinding: `fetch` rejects with `signal.reason`, so the sentence arrives
   * where the failure does and nothing has to guess afterwards which of the
   * three callers below did the aborting.
   */
  const abortInFlight = useCallback((reason: string) => {
    for (const controller of inFlight.current.values()) controller.abort(reason);
    inFlight.current.clear();
  }, []);

  const startUpload = useCallback(
    (target: KnowledgeAttachment) => {
      if (knowledgeBaseId === null) return;
      if (readOnlyReason !== null) {
        // Only reachable when write access disappears underneath a file that is
        // already queued -- `addFiles` refuses to queue one while the base is
        // read-only. Failing it loudly beats dropping it: the reason stays on
        // screen and the retry control is there for when access comes back.
        setItems((current) =>
          current.map((item) =>
            item.localId === target.localId && canStart(item.state)
              ? { ...item, state: "failed", error: readOnlyReason }
              : item,
          ),
        );
        return;
      }
      const controller = new AbortController();
      inFlight.current.set(target.localId, controller);
      setItems((current) =>
        current.map((item) => {
          if (item.localId !== target.localId || !canStart(item.state)) return item;
          return withoutError({ ...item, state: "uploading" });
        }),
      );
      void uploadDocument(
        identity,
        {
          file: target.file,
          documentId: target.documentId,
          knowledgeBaseId,
          grantedPrincipals: [],
        },
        controller.signal,
      )
        .then(() => {
          inFlight.current.delete(target.localId);
          if (!mounted.current) return;
          setItems((current) =>
            current.map((item) =>
              item.localId === target.localId
                ? withoutError({ ...item, state: "indexing", uploaded: true })
                : item,
            ),
          );
        })
        .catch((error: unknown) => {
          inFlight.current.delete(target.localId);
          if (!mounted.current) return;
          // An abort is this hook's own doing, and it carries the sentence to
          // show for it. `clear` and `remove` drop the chip before aborting, so
          // their update lands on nothing; a knowledge-base switch keeps the
          // chip, and this is what stops it sitting at 正在上传到知识库 for the
          // rest of the session over a request that will never come back.
          setItems((current) =>
            current.map((item) =>
              item.localId === target.localId
                ? {
                    ...item,
                    state: "failed",
                    error: controller.signal.aborted
                      ? abortReason(controller.signal)
                      : error instanceof Error
                        ? error.message
                        : "上传失败",
                  }
                : item,
            ),
          );
        });
    },
    [identity, knowledgeBaseId, readOnlyReason],
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
        // Every status the list can return is answered, not just `ready`.
        // Treating "not ready" as "still working" is how a refused document
        // sat at 正在建立索引 forever: ingestion had already given up, said so,
        // and this loop had no branch that could hear it.
        const byId = new Map(
          response.documents.map((record) => [record.document_id, record] as const),
        );
        setItems((current) =>
          current.map((item) => {
            // Scoped to `indexing` so a poll in flight across a retry cannot
            // overwrite the chip: a retried file is `uploading` here, and the
            // server still reports the refusal of the revision it is replacing.
            if (item.state !== "indexing") return item;
            const record = byId.get(item.documentId);
            if (record === undefined) return item;
            if (record.status === "ready") return withoutError({ ...item, state: "ready" });
            if (record.status === "failed") {
              return {
                ...item,
                state: "failed",
                error: indexingFailure(record.failure_code),
              };
            }
            return item;
          }),
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

  /**
   * Nothing may keep uploading into a base that is no longer the selected one.
   *
   * Both pages happen to call `clear` before they switch, which aborts by the
   * same route -- but that is their courtesy, not this hook's guarantee, and the
   * failure it prevents is invisible while it happens: the bytes finish landing
   * in the base the reader just navigated away from, with the tray already
   * empty and nothing on screen that could say so.
   *
   * Keyed off the id changing rather than the effect's cleanup, because cleanup
   * also runs on unmount and an unmount is not a switch: leaving a page while a
   * file uploads into the base that was chosen for it is the upload working.
   */
  const previousKnowledgeBaseId = useRef(knowledgeBaseId);
  useEffect(() => {
    if (previousKnowledgeBaseId.current === knowledgeBaseId) return;
    previousKnowledgeBaseId.current = knowledgeBaseId;
    abortInFlight("已切换知识库，这次上传已取消");
  }, [abortInFlight, knowledgeBaseId]);

  const addFiles = useCallback(
    (files: File[]) => {
      // Refused here and not only at the disabled button: a read-only base is a
      // property of this hook, and a file can still arrive from a caller that
      // renders its own control or forgets to pass the flag on.
      if (readOnlyReason !== null) return;
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
            uploaded: false,
          }));
        return [...current, ...additions];
      });
    },
    [readOnlyReason],
  );

  const remove = useCallback((localId: string) => {
    inFlight.current.get(localId)?.abort("不再上传这个文件");
    inFlight.current.delete(localId);
    setItems((current) => current.filter((item) => item.localId !== localId));
  }, []);
  const retry = useCallback(
    (localId: string) => {
      const item = items.find((candidate) => candidate.localId === localId);
      if (item !== undefined) startUpload(item);
    },
    [items, startUpload],
  );
  const clear = useCallback(() => {
    abortInFlight("不再上传这些文件");
    setItems([]);
  }, [abortInFlight]);
  const hasBlockingItems = items.some((item) => item.state !== "ready");

  return useMemo(
    () => ({
      items,
      addFiles,
      remove,
      retry,
      clear,
      hasBlockingItems,
      readOnlyReason,
    }),
    [addFiles, clear, hasBlockingItems, items, readOnlyReason, remove, retry],
  );
}

export function AttachmentButton({
  inputRef,
  disabled = false,
  disabledReason,
  onFiles,
}: {
  inputRef?: RefObject<HTMLInputElement | null>;
  disabled?: boolean;
  /**
   * Why this control is off, when it is off for a reason worth stating.
   *
   * A dead paperclip and a dead paperclip that explains itself look the same
   * until someone clicks it. The one case that needs the sentence is a
   * read-only knowledge base: nothing about the composer suggests it, and
   * without this the reader's next move is to pick a file and wait.
   */
  disabledReason?: string;
  onFiles: (files: File[]) => void;
}) {
  const ownRef = useRef<HTMLInputElement>(null);
  const ref = inputRef ?? ownRef;
  const chooseFiles = (event: ChangeEvent<HTMLInputElement>) => {
    onFiles(Array.from(event.target.files ?? []));
    event.target.value = "";
  };
  const blocked = disabled && disabledReason !== undefined;
  return (
    <span className="aw-attachment-control">
      <button
        aria-label={blocked ? `无法上传文件：${disabledReason}` : "上传文件到知识库"}
        className="aw-attachment-button"
        disabled={disabled}
        onClick={() => ref.current?.click()}
        // Says where the file goes, because it goes somewhere permanent. The
        // old label ("添加附件") described a per-message attachment this system
        // does not have.
        title={
          blocked
            ? disabledReason
            : "上传 PDF、Word 或 Markdown 到所选知识库（最多 5 个，上传后会一直保留）"
        }
        type="button"
      >
        <Paperclip aria-hidden="true" size={17} />
      </button>
      <input
        accept=".pdf,.md,.markdown,.docx,.txt,text/plain,text/markdown,application/pdf"
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
  const anyUploaded = items.some((item) => item.uploaded);
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
              //
              // Which of the two sentences applies is keyed on whether the
              // bytes actually landed, not on the state name: a document the
              // indexer refused is `failed` and is in the knowledge base, so
              // "不再上传" there would offer to undo something already done.
              aria-label={
                item.uploaded
                  ? `从这个列表中移除 ${item.file.name}（文件仍在知识库中）`
                  : `不再上传 ${item.file.name}`
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

/**
 * What an aborted upload says, from the reason its aborter supplied.
 *
 * Falls back rather than trusting the shape: `signal.reason` is whatever was
 * passed to `abort()`, and a browser that aborts on its own (or a future caller
 * that passes an Error) puts something other than a sentence there.
 */
function abortReason(signal: AbortSignal): string {
  return typeof signal.reason === "string" ? signal.reason : "上传已取消";
}

function withoutError(item: KnowledgeAttachment): KnowledgeAttachment {
  const next = { ...item };
  delete next.error;
  return next;
}

/**
 * What a refused document says, at the size a chip can hold.
 *
 * Only the codes ingestion actually emits are translated. A code this table has
 * not learned gets a sentence that admits as much, because the alternative --
 * inventing a plausible cause -- sends the reader off to change the wrong thing
 * about the file and upload it into the same refusal.
 *
 * Shorter than the knowledge-base page's wording on purpose. That page has room
 * for an instruction under each document; this one has a single line inside a
 * chip next to a retry button, and a sentence that wraps to three lines there is
 * a sentence nobody finishes reading.
 */
const INDEXING_FAILURES: Readonly<Record<string, string>> = {
  invalid_tool_input: "文件内容无法解析，换一个可读的文件再试",
  not_found: "找不到上传的原始文件，请重新上传",
};

function indexingFailure(code: string | null): string {
  if (code === null) return "索引失败，可以重试一次";
  return INDEXING_FAILURES[code] ?? `索引失败（${code}），可以重试一次`;
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
