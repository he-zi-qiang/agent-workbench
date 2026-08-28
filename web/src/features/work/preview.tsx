/**
 * 产出预览这一族共用的东西。
 *
 * 它们此前长在 `WorkPage.tsx` 里，只有 `TaskResult` 一个消费者。产出正文拆成
 * `ArtifactPreview` 之后变成两个消费者，而让 `ArtifactPreview` 反过来 import
 * `WorkPage` 会绕成一个环——两边都还要 import 对方。放在这里，两边各自向下依赖。
 */

import type { DocumentLayoutDecline } from "../../api/client";
import type { DocumentPreview } from "../../api/types";

export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${String(bytes)} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

/**
 * What the text preview did not bring across, zeros left out.
 *
 * This replaces a sentence -- "不含排版、图片与页眉页脚；共 N 张表格" -- and the
 * sentence is why it exists. Prose can hold one number; the server reports
 * seven, and threading them into that clause produces a paragraph nobody
 * finishes reading. A list also survives the next count without being rewritten.
 *
 * Zeros are dropped rather than shown as 0. A document with no footnotes has
 * nothing missing on that axis, and a row saying so is noise competing with the
 * rows that mean something. The cost is that a count the server failed to send
 * would read as a zero and disappear, which is why the wire model requires
 * every one of them (`api/types.ts`).
 *
 * **The cut is one of the rows.** It has to be, because rendering nothing is
 * how this list says the preview is faithful, and a preview that stopped
 * partway through the document is not entitled to say that. The seven counts
 * cannot cover it: every one of them is of the whole document
 * (`adapters/documents/docx.py`), so a truncated preview reports the pictures
 * and the footnotes below the cut correctly and reports nothing whatever about
 * the prose that went with them -- and a document of plain paragraphs, cut in
 * half, scores zero on all seven. It leads the list rather than sorting into
 * it, and it is the only row without a number: what is missing is exactly the
 * part this preview did not read, which is why there is nothing to count.
 *
 * So an empty list now claims what it can carry: the text is whole and none of
 * the seven kinds was lost. Not that the document is fully represented --
 * endnotes, text boxes and tables nested inside cells go missing with no count
 * naming them, and that is a gap in the extraction rather than something this
 * list can close by staying quiet.
 *
 * Only under the text view. In 版面 the pictures and the running titles are on
 * screen, so this list would be describing losses the reader can see did not
 * happen.
 */
export function PreviewGaps({ preview }: { preview: DocumentPreview }) {
  // Ordered by how invisible the loss is. A missing picture cannot be inferred
  // from the prose around it; a table that came through as plain rows is at
  // least visibly a table. Quantities carry their measure word, because "5" in
  // a column of counts says less than "5 段" does.
  const counted = [
    { label: "图片没有显示", count: preview.image_count, unit: "张" },
    { label: "脚注没有显示", count: preview.footnote_count, unit: "条" },
    { label: "页眉没有显示", count: preview.header_count, unit: "处" },
    { label: "页脚没有显示", count: preview.footer_count, unit: "处" },
    { label: "表格只保留文字", count: preview.table_count, unit: "张" },
    {
      label: "列表序号没有生成",
      count: preview.numbered_paragraph_count,
      unit: "段",
    },
    {
      label: "段落样式没有保留",
      count: preview.flattened_paragraph_count,
      unit: "段",
    },
  ].filter((gap) => gap.count > 0);
  if (!preview.truncated && counted.length === 0) return null;
  // The middle clause only when there are numbers under it to be read wrong.
  // They are of the whole file, which under a preview that stops early is the
  // difference between "four pictures" and "four pictures so far" -- and a
  // reader who takes the smaller reading concludes the rest is prose.
  const cutNote = [
    "正文只显示到这里，后面的内容没有进入预览。",
    counted.length === 0
      ? ""
      : "下面的数字是按整份文档数的，不只是显示出来的这部分。",
    "完整内容请下载。",
  ].join("");
  return (
    <ul className="aw-preview-gaps" aria-label="预览没有还原的部分">
      {preview.truncated ? (
        <li className="aw-preview-gap-cut">{cutNote}</li>
      ) : null}
      {counted.map((gap) => (
        <li key={gap.label}>
          <span>{gap.label}</span>
          <strong>
            {gap.count} {gap.unit}
          </strong>
        </li>
      ))}
    </ul>
  );
}

/**
 * Why this panel has no layout to show -- the server's reasons, plus one of
 * its own.
 *
 * The server's vocabulary stays the server's: `getDocumentPdf` answers about a
 * deployment and a document, and `viewer_unavailable` is a fact about neither.
 * They meet here because the reader is owed one sentence rather than a taxonomy
 * -- what they see either way is the text view and a note saying why.
 */
export type PanelLayoutDecline = DocumentLayoutDecline | "viewer_unavailable";

export function layoutDeclineNote(reason: PanelLayoutDecline): string {
  if (reason === "converter_unavailable") {
    return "这套部署没有版面预览：服务器上没有可用的文档转换器。下面是文字预览，需要原样查看请下载。";
  }
  if (reason === "too_large") {
    return "这份文档的版面太大，页面里不展开。下面是文字预览，需要原样查看请下载。";
  }
  if (reason === "viewer_unavailable") {
    return "这个浏览器不显示内嵌 PDF，所以这里给不出版面（文档本身没问题）。下面是文字预览，要看排版请下载后用 Word 打开，或换一个浏览器打开控制台。";
  }
  return "这套部署给不出这份文档的版面。下面是文字预览，需要原样查看请下载。";
}
