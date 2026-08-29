import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { MarkdownContent } from "./MarkdownContent";
import { ErrorNotice, LoadingLine } from "./ui";

/**
 * A Markdown file, as the document it was written to be -- with its source one
 * click away.
 *
 * Props identical to `TextPreview`'s, on purpose: this is the viewer that
 * `previewKind === "text"` hands off to when the bytes are Markdown, so every
 * call site is a one-line swap and neither viewer can end up fetching or
 * caching differently from the other.
 *
 * ## Why this is not a sixth `PreviewKind`
 *
 * `previewKind` is the vocabulary **every** surface that shows a file reads,
 * Work's artifact panel included, and ADR-065 §4 turned down a sixth value for
 * `python` on the grounds that it would force each of those surfaces to answer
 * a question only one of them can. The same argument applies here from the
 * other direction: `text/markdown` and `text/plain` want the *same fetch* and
 * differ only in how the string is painted afterwards. So Markdown is a second
 * question asked inside the text arm -- `isMarkdown(...)`, next to
 * `isRunnablePython(...)`, which is the same shape for the same reason.
 *
 * ## Why 渲染 is the default, when 源码 is the default for a `.py`
 *
 * ADR-065 §4 makes running a script opt-in because the cost of the default is
 * a container on the server, and spending that for a reader who only wanted to
 * look at the file is wrong. Rendering Markdown costs a paint. So this follows
 * `HtmlPreview` instead, where "showing the artifact" and "rendering it" are
 * the same act (ADR-066): a report written as Markdown *is* the rendered
 * document, and handing back `<pre>` shows the reader the packaging rather
 * than the thing.
 *
 * The 源码 half is not symmetry for its own sake. This viewer's main caller is
 * a **coding** console, where a `.md` is as often a file being edited as a
 * document being read -- and the one question a rendered view cannot answer is
 * "what exactly is in there", which is the question you have right after an
 * agent wrote it.
 *
 * ## A cut file does not render
 *
 * Same rule as `HtmlPreview`, and the reason survives the change of format.
 * Half a document renders as something that never existed -- an unclosed fence
 * swallows the rest of the file into a code block, an unfinished table drops
 * its remaining rows -- and it is presented as the artifact. Markdown fails
 * more gently than HTML here, not differently: the honest view of a truncated
 * file is its source with the cut named.
 */
export function MarkdownPreview({
  load,
  queryKey,
}: {
  /** Fetches the body; called once and cached under `queryKey`. */
  load: () => Promise<{ text: string; truncated: boolean }>;
  /** Cache identity -- an artifact and a workspace file must never share. */
  queryKey: readonly unknown[];
}) {
  const [showSource, setShowSource] = useState(false);
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

  const { text, truncated } = body.data;
  const rendering = !truncated && !showSource;

  return (
    <>
      {/* The same control `HtmlPreview` and the docx panel use, because it is
          the same act: two views of one file, picked rather than scrolled
          past. Borrowing the classes rather than the component -- there is no
          component to borrow, and three buttons' worth of markup is a smaller
          thing to repeat than a shared wrapper nobody else would fit. */}
      <div className="aw-segmented aw-preview-views" aria-label="预览方式">
        <button
          aria-pressed={rendering}
          className={rendering ? "is-active" : ""}
          disabled={truncated}
          onClick={() => {
            setShowSource(false);
          }}
          type="button"
        >
          渲染
        </button>
        <button
          aria-pressed={!rendering}
          className={rendering ? "" : "is-active"}
          onClick={() => {
            setShowSource(true);
          }}
          type="button"
        >
          源码
        </button>
      </div>
      {rendering ? (
        <MarkdownContent text={text} />
      ) : (
        <pre className="aw-code-file-body">{text}</pre>
      )}
      {truncated ? (
        // Named rather than left to look like the whole file -- a reader who
        // takes a cut preview for the artifact goes looking for content that
        // is in the file and not on screen. It also says why 渲染 is refused,
        // because a disabled control with no reason reads as a broken one.
        <p className="aw-page-note">
          只显示了开头一部分，完整内容请下载。半份文档渲染出来会是它从来不是的样子，所以这里只给源码。
        </p>
      ) : null}
    </>
  );
}
