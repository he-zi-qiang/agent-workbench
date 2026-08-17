import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { MAX_PREVIEW_BYTES } from "../api/client";
import { ErrorNotice, LoadingLine } from "./ui";

/**
 * The policy injected into every rendered page. Everything self-contained is
 * allowed (inline scripts and styles, data:/blob: assets -- an agent-built
 * page has nowhere else to keep them) and every reach outward is not:
 * `connect-src 'none'` closes fetch/XHR/WebSocket, `default-src 'none'`
 * closes external scripts, styles, frames and images, `form-action 'none'`
 * closes submits, `base-uri 'none'` closes retargeting relative URLs.
 */
const PREVIEW_CSP =
  "default-src 'none'; script-src 'unsafe-inline' data: blob:; " +
  "style-src 'unsafe-inline' data: blob:; img-src data: blob:; " +
  "font-src data: blob:; media-src data: blob:; connect-src 'none'; " +
  "form-action 'none'; base-uri 'none'";

const CSP_META = `<meta http-equiv="Content-Security-Policy" content="${PREVIEW_CSP}">`;

/**
 * Place the CSP `<meta>` where the parser will honour it: as early in the
 * head as the document's own markup allows.
 *
 * A meta CSP applies from its parse position, so this is defence in depth
 * rather than the boundary -- markup that runs before an injected head tag
 * escapes it, and the component does not pretend otherwise. The boundary is
 * the frame itself: `sandbox` without `allow-same-origin` gives the document
 * an opaque origin, no cookies, no storage, and no way to send the identity
 * headers the platform's API requires. What this meta adds on top is the
 * common case: an ordinary generated page also cannot phone out.
 *
 * Three insertion points, tried in order, because HTML makes all three legal:
 * after `<head>` where there is one, after `<html>` where the head is
 * implicit, and after the doctype (or at the start) for fragment-shaped
 * documents -- the parser hoists a leading `<meta>` into the head it creates.
 */
export function withPreviewCsp(html: string): string {
  // `<head(\s…)?>` and not `<head[^>]*>`: the loose form also matches
  // `<header>`, and a page that opens with one -- an ordinary shape for a
  // generated fragment -- would take the head branch and have the meta
  // planted inside the implicit body, where a browser discards it outright.
  const head = /<head(\s[^>]*)?>/i.exec(html);
  if (head !== null) {
    const at = head.index + head[0].length;
    return html.slice(0, at) + CSP_META + html.slice(at);
  }
  const root = /<html(\s[^>]*)?>/i.exec(html);
  if (root !== null) {
    const at = root.index + root[0].length;
    return html.slice(0, at) + CSP_META + html.slice(at);
  }
  const doctype = /^\s*<!doctype[^>]*>/i.exec(html);
  if (doctype !== null) {
    const at = doctype.index + doctype[0].length;
    return html.slice(0, at) + CSP_META + html.slice(at);
  }
  return CSP_META + html;
}

/**
 * An HTML artifact, run instead of read.
 *
 * The page an agent builds -- a chart, a demo, a small tool -- only answers
 * "did it work?" by rendering, so this frame renders it, scripts included.
 * What makes that admissible is the `sandbox` attribute below, and one
 * omission in it carries **the whole design, alone**: **no
 * `allow-same-origin`.** With the flag absent the document gets an opaque
 * origin -- no parent DOM, no cookies, no storage, and any call at the
 * platform's API fails for want of the identity headers only the console can
 * add. A test pins the attribute value, and `BlobPreview` has the mirror test
 * pinning that its PDF frame has none.
 *
 * **There is no second line.** An `about:srcdoc` document inherits its
 * parent's origin exactly as a `blob:` URL does; the choice between them buys
 * convenience (one string, no object-URL lifetime) and buys *nothing*
 * security-wise. Adding `allow-same-origin` here, or dropping `sandbox`,
 * hands an agent-written page this console's own origin: `parent.document`,
 * the stored identity, and every `/v1/*` route under the reader's
 * credentials. Nothing else in this file would stop it.
 *
 * Residual risk, stated rather than hidden: the injected meta CSP is defence
 * in depth and not a network boundary. Markup that runs before it escapes it,
 * and a page can navigate *itself* out (`location.href = …`), which no CSP
 * directive here forbids. Platform data stays out of reach either way; the
 * public internet is best-effort (known-gaps F-12).
 *
 * The 源码 view is part of this component rather than the caller's text path
 * because both views are one fetch: the same string either goes into the
 * frame or into a `<pre>`, and the reader flips between them without a
 * second request.
 *
 * No download button in here -- every surface that mounts this already
 * carries exactly one labelled 下载, and the Work page pins that count.
 */
export function HtmlPreview({
  load,
  name,
  queryKey,
  sizeBytes,
}: {
  /** Fetches the source; called once and cached under `queryKey`. */
  load: () => Promise<{ text: string; truncated: boolean }>;
  name: string;
  /** Cache identity -- an artifact and a workspace file must never share. */
  queryKey: readonly unknown[];
  sizeBytes: number;
}) {
  const [showSource, setShowSource] = useState(false);
  // Judged from the listing's own count, before any transfer, the same way
  // BlobPreview declines: a refusal that costs nothing. The cap is the text
  // preview's, because that is what both views hold in memory.
  const oversized = sizeBytes > MAX_PREVIEW_BYTES;
  const sourceQuery = useQuery({
    queryKey,
    enabled: !oversized,
    staleTime: Number.POSITIVE_INFINITY,
    queryFn: load,
  });

  if (oversized) {
    return (
      <p className="aw-page-note">这个文件太大，页面里不展开；请下载后查看。</p>
    );
  }
  if (sourceQuery.isPending) {
    return <LoadingLine label="正在读取文件" />;
  }
  if (sourceQuery.isError) {
    return (
      <>
        <ErrorNotice message="读取文件失败" />
        {/* The preview is the convenience; the file is the deliverable. */}
        <p className="aw-page-note">可以直接下载查看，或稍后重试。</p>
      </>
    );
  }
  const { text, truncated } = sourceQuery.data;
  // A truncated page must not render: half a document runs half its scripts
  // and paints something that never existed, presented as the artifact. The
  // size gate above makes this unreachable in practice (both caps are
  // MAX_PREVIEW_BYTES); if the listing's count and the body ever disagree,
  // the honest view is the source with the cut named.
  const canRender = !truncated;
  const rendering = canRender && !showSource;

  return (
    <>
      {/* The same control the docx panel uses for 版面/文字: two views of one
          file, picked rather than scrolled past. */}
      <div className="aw-segmented aw-preview-views" aria-label="预览方式">
        <button
          aria-pressed={rendering}
          className={rendering ? "is-active" : ""}
          disabled={!canRender}
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
        <>
          <div className="aw-preview-frame aw-html-frame">
            <iframe
              referrerPolicy="no-referrer"
              sandbox="allow-scripts"
              srcDoc={withPreviewCsp(text)}
              title={`${name} 预览`}
            />
          </div>
          {/* What is promised here is exactly what is guaranteed. The earlier
              wording also claimed the page could not reach the internet,
              which is best-effort rather than true (known-gaps F-12) -- and a
              reader who opens an unknown page on the strength of an
              overstated promise is the person that gap costs. */}
          <p className="aw-page-note">
            页面在隔离的沙箱里运行：拿不到你的登录态，也读不到平台数据。
            它仍可能自行访问外部网络，来源不明的页面请谨慎打开。
          </p>
        </>
      ) : (
        <>
          <pre className="aw-code-file-body">{text}</pre>
          {truncated ? (
            <p className="aw-page-note">只显示了开头一部分，完整内容请下载。</p>
          ) : null}
        </>
      )}
    </>
  );
}
