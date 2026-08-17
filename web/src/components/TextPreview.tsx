import { useQuery } from "@tanstack/react-query";
import { ErrorNotice, LoadingLine } from "./ui";

/**
 * A text file's body, fetched here rather than handed in.
 *
 * The sibling of `HtmlPreview` and `BlobPreview`, and it exists for the same
 * reason they do: a component that owns its own fetch can be dropped anywhere,
 * and one that expects its caller to have fetched already can only live where
 * that caller did the work.
 *
 * That distinction had a cost. Text was the one kind whose bytes arrived
 * through a prop, prefetched by the page when a file was opened in the side
 * panel -- so the produced-file card in the conversation, which has no such
 * prefetch, could offer an inline preview for an image or an HTML page and
 * *nothing at all* for a `.py`. On a coding console that is the wrong file to
 * leave out: the code is the product, and the card showed only its name.
 *
 * **No size gate, deliberately** -- and this is where it parts company with
 * `HtmlPreview`, which refuses anything over `MAX_PREVIEW_BYTES` before
 * transferring it. That refusal is not about the transfer: a truncated *page*
 * must not render, because half a document runs half its scripts and paints
 * something that never existed, presented as the artifact. Text has no such
 * failure. The head of a large file is a useful, honest thing to show as long
 * as the cut is named, which is what this does and what the side panel has
 * always done. Declining here would take away a view that works.
 *
 * The caller decides whether to open this *without being asked* -- see
 * `AUTO_PREVIEW_MAX_BYTES` in `CodeTurn`. That is the right place for the size
 * question, because the cost being weighed is an unrequested fetch, not a
 * requested one.
 *
 * Infinite `staleTime`, so a file previewed inline and then opened in the panel
 * is fetched once -- as long as both callers pass the same `queryKey`.
 */
export function TextPreview({
  load,
  queryKey,
}: {
  /** Fetches the body; called once and cached under `queryKey`. */
  load: () => Promise<{ text: string; truncated: boolean }>;
  /** Cache identity -- an artifact and a workspace file must never share. */
  queryKey: readonly unknown[];
}) {
  const body = useQuery({
    queryKey,
    staleTime: Number.POSITIVE_INFINITY,
    queryFn: load,
  });

  if (body.isPending) return <LoadingLine label="正在读取文件" />;
  if (body.isError) {
    return (
      <>
        <ErrorNotice message="读取文件失败" />
        {/* The preview is the convenience; the file is the deliverable. */}
        <p className="aw-page-note">可以直接下载查看，或稍后重试。</p>
      </>
    );
  }

  return (
    <>
      <pre className="aw-code-file-body">{body.data.text}</pre>
      {body.data.truncated ? (
        // Named rather than left to look like the whole file -- a reader who
        // takes a cut preview for the artifact goes looking for content that
        // is in the file and not on screen.
        <p className="aw-page-note">只显示了开头一部分，完整内容请下载。</p>
      ) : null}
    </>
  );
}
