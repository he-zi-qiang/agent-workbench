import { useQuery } from "@tanstack/react-query";
import { useCallback } from "react";
import { MAX_LAYOUT_BYTES } from "../api/client";
import { browserShowsPdfInline } from "./media";
import { ErrorNotice, LoadingLine } from "./ui";

/**
 * What this page will hold in memory to show one picture.
 *
 * Smaller than the PDF ceiling because the failure mode is worse: a PDF frame
 * pages, an `<img>` decodes the whole bitmap before painting anything, and a
 * generated plot has no business being larger than this. Over the cap the
 * reader is pointed at the download, which is where a file that size was
 * heading anyway.
 */
const MAX_IMAGE_BYTES = 20 * 1024 * 1024;

/**
 * An image or a PDF, shown in place instead of saved to find out what it was.
 *
 * One component for the two kinds because everything hard about them is the
 * same thing: the bytes must be fetched (an embedded element carries no
 * identity headers -- see `getArtifactBlob`), the object URL must outlive the
 * render that made it, and the size must be judged *before* the fetch, from
 * the listing's own count, so a refusal costs nothing.
 *
 * The URL's lifetime is tied to the element through a ref callback and React
 * 19's ref cleanup -- created when the node appears, revoked when it goes --
 * copied from the Work page's layout frame, which learned this the hard way.
 *
 * No download button in here. Every surface that mounts this already carries
 * exactly one labelled 下载, and the Work page pins that count in a test; a
 * second control inside the preview would be the regression.
 */
export function BlobPreview({
  kind,
  load,
  name,
  queryKey,
  sizeBytes,
}: {
  kind: "image" | "pdf";
  /** Fetches the bytes; called once and cached under `queryKey`. */
  load: () => Promise<Blob>;
  name: string;
  /** Cache identity -- the caller knows whether this is an artifact or a
      workspace file, and two files must never share an entry. */
  queryKey: readonly unknown[];
  sizeBytes: number;
}) {
  const oversized =
    sizeBytes > (kind === "pdf" ? MAX_LAYOUT_BYTES : MAX_IMAGE_BYTES);
  // Asked before the fetch is: a browser that will not paint a PDF makes the
  // transfer pointless, and declining here costs one property read.
  const viewerShowsPdf = kind !== "pdf" || browserShowsPdfInline();
  const blobQuery = useQuery({
    queryKey,
    enabled: !oversized && viewerShowsPdf,
    staleTime: Number.POSITIVE_INFINITY,
    queryFn: load,
  });
  const blob = blobQuery.data ?? null;
  // Both elements read `src` the same way, so one callback serves them; typed
  // to the union rather than twice.
  const attach = useCallback(
    (node: HTMLIFrameElement | HTMLImageElement | null) => {
      if (node === null || blob === null) return;
      const url = URL.createObjectURL(blob);
      node.src = url;
      return () => {
        URL.revokeObjectURL(url);
      };
    },
    [blob],
  );

  if (oversized) {
    return (
      <p className="aw-page-note">
        这个文件太大，页面里不展开；请下载后查看。
      </p>
    );
  }
  if (!viewerShowsPdf) {
    return (
      <p className="aw-page-note">
        这个浏览器不显示内嵌 PDF，所以这里给不出预览（文件本身没问题）；请下载后打开。
      </p>
    );
  }
  if (blobQuery.isPending) {
    return <LoadingLine label="正在读取文件" />;
  }
  if (blobQuery.isError) {
    return (
      <>
        <ErrorNotice message="读取文件失败" />
        {/* The preview is the convenience; the file is the deliverable. */}
        <p className="aw-page-note">可以直接下载查看，或稍后重试。</p>
      </>
    );
  }
  if (kind === "image") {
    return <img alt={name} className="aw-preview-image" ref={attach} />;
  }
  return (
    <>
      <div className="aw-preview-frame">
        {/* No `sandbox`, for the reason the Work page's layout frame has none:
            the blob was typed by the client, so the frame can only be the
            browser's own PDF viewer -- and a sandbox strict enough to matter
            also stops that viewer, silently. */}
        <iframe ref={attach} title={`${name} 预览`} />
      </div>
      <p className="aw-page-note">
        这里若是一片空白或纯黑，是这个浏览器不显示内嵌 PDF——文件没问题，下载后可正常打开。
      </p>
    </>
  );
}
