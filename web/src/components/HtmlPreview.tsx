import { useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";
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
 * How wide the page believes it is, in 适应宽度 mode.
 *
 * Read from the stylesheet rather than hard-coded here, so the number sits
 * beside the two preview heights it has to stay consistent with.
 */
function logicalWidth(): number {
  if (typeof window === "undefined") return FALLBACK_LOGICAL_WIDTH;
  const raw = getComputedStyle(document.documentElement).getPropertyValue(
    "--aw-preview-logical-width",
  );
  const parsed = Number.parseFloat(raw);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : FALLBACK_LOGICAL_WIDTH;
}

/** Used when the stylesheet has not loaded, and under jsdom. */
const FALLBACK_LOGICAL_WIDTH = 1024;

/**
 * The box's measured size, or null before it has one.
 *
 * `ResizeObserver` rather than a one-shot read on mount: the box is inside a
 * column whose width changes when the preview panel opens, and a scale
 * computed once would leave the page half off the frame from then on.
 *
 * Takes the **node**, not a ref object, and that is the fix for a bug this
 * shipped with for one build. The frame only exists once the fetch resolves --
 * before that the component returns a loading line -- so an effect keyed on a
 * stable ref object runs once, on mount, finds `ref.current === null`, and
 * never runs again when the div finally appears. Nothing observed anything,
 * `size` stayed null, and 适应宽度 silently rendered at 100%. A callback ref
 * puts the node in state, so the effect re-runs at exactly the moment there is
 * something to measure.
 *
 * jsdom has no layout and every rect is 0, so this returns null there. That is
 * the honest answer, and the reason the caller falls back to unscaled
 * rendering rather than dividing by zero.
 */
function useBoxSize(
  node: HTMLDivElement | null,
  revision: number,
): { width: number; height: number } | null {
  const [size, setSize] = useState<{ width: number; height: number } | null>(
    null,
  );
  useEffect(() => {
    if (node === null) return;
    // Both, and both positive. A frame mid-transition reports one of them as
    // zero, and a scale of zero paints nothing at all -- which looks exactly
    // like a page that failed to load.
    const measure = () => {
      const { width, height } = node.getBoundingClientRect();
      setSize(width > 0 && height > 0 ? { width, height } : null);
    };
    // Measured once, synchronously, *before* observing -- and that is not
    // belt-and-braces. `ResizeObserver` notifications are delivered as part of
    // the rendering steps, so a document that is not being rendered never gets
    // the initial callback: measured in a hidden pane, an observer on a node
    // with a real 794x468 rect stayed silent indefinitely, and 适应宽度 sat
    // there quietly behaving as 实际大小. Reading the rect does not depend on
    // any of that.
    measure();
    if (typeof ResizeObserver === "undefined") return;
    // The observer is for what happens *after*: the preview panel opening,
    // the window resizing, the card expanding.
    const observer = new ResizeObserver(measure);
    observer.observe(node);
    return () => {
      observer.disconnect();
    };
    // `revision` is not read in the body, and that is the point: it is bumped
    // when the box changes size for a reason no observation of *this node*
    // reports promptly -- entering or leaving fullscreen -- and re-running the
    // effect takes the synchronous measurement again.
  }, [node, revision]);
  return size;
}


/**
 * Whether ``element`` is the document's fullscreen element right now.
 *
 * Tracked from the `fullscreenchange` event rather than from the click that
 * asked, because the browser is the authority and it can disagree: a request
 * can be refused outright (no user gesture, a policy), and the reader can
 * leave with Escape without touching any control this component drew. State
 * set optimistically on click would then say 退出全屏 over a window that is
 * not fullscreen.
 */
function useIsFullscreen(element: HTMLElement | null): boolean {
  const [full, setFull] = useState(false);
  useEffect(() => {
    if (element === null) return;
    const sync = () => {
      setFull(document.fullscreenElement === element);
    };
    sync();
    document.addEventListener("fullscreenchange", sync);
    return () => {
      document.removeEventListener("fullscreenchange", sync);
    };
  }, [element]);
  return full;
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
  // 适应宽度 by default, and that is the whole point of the mode existing: a
  // preview's job is to show the page as it was laid out, and a page authored
  // for a desktop viewport reflowed into a 400px column is not that page. The
  // reader who wants to *use* it -- click, type, play -- switches to 实际大小.
  const [fit, setFit] = useState(true);
  const [boxNode, setBoxNode] = useState<HTMLDivElement | null>(null);
  const [stageNode, setStageNode] = useState<HTMLDivElement | null>(null);
  const fullscreen = useIsFullscreen(stageNode);
  // Entering fullscreen changes the box's size without changing the node, and
  // the observer's delivery is not something to depend on for a transition the
  // reader is watching. Bumped here, consumed by the effect.
  const box = useBoxSize(boxNode, fullscreen ? 1 : 0);
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
  // Null means "render at 100%", which is both the 实际大小 choice and the
  // honest answer whenever the box has no measured size yet -- on the first
  // paint, and under jsdom, where every rect is zero. Falling back to unscaled
  // is what keeps a missing measurement from becoming a blank frame.
  //
  // The arithmetic, once: the iframe is laid out `LOGICAL` CSS pixels wide and
  // `height / factor` tall, then multiplied by `factor = boxWidth / LOGICAL`.
  // Rendered, that is exactly `boxWidth × boxHeight` -- the frame is filled,
  // not letterboxed -- while the document inside believes it has a
  // `LOGICAL`-wide viewport. That belief is the entire feature: `92vw` and
  // `@media (min-width: 560px)` resolve against a desktop width instead of
  // against whatever narrow column this preview happens to sit in.
  const scaled =
    fit && box !== null
      ? (() => {
          const logical = logicalWidth();
          const factor = box.width / logical;
          return {
            width: logical,
            height: box.height / factor,
            factor,
          };
        })()
      : null;

  return (
    <>
      {/* The same control the docx panel uses for 版面/文字: two views of one
          file, picked rather than scrolled past. */}
      <div className="aw-preview-controls">
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
        <div className="aw-segmented aw-preview-zoom" aria-label="显示比例">
          <button
            aria-pressed={fit}
            className={fit ? "is-active" : ""}
            onClick={() => {
              setFit(true);
            }}
            type="button"
          >
            适应宽度
          </button>
          <button
            aria-pressed={!fit}
            className={fit ? "" : "is-active"}
            onClick={() => {
              setFit(false);
            }}
            type="button"
          >
            实际大小
          </button>
        </div>
      ) : null}
      {rendering && stageNode !== null ? (
        <button
          className="aw-button aw-preview-fullscreen"
          onClick={() => {
            if (fullscreen) {
              void document.exitFullscreen().catch(() => undefined);
              return;
            }
            // The **stage**, never the iframe. Fullscreening the frame itself
            // would hand an agent-written page the whole screen with nothing
            // around it -- no console, no browser chrome, and no sentence
            // saying what it is. A page painting a convincing sign-in box at
            // that point has no contradicting context anywhere on the display.
            // The stage keeps the caution bar on screen, and the page gets the
            // rest (ADR-071).
            //
            // Rejected rather than thrown: a request without a user gesture,
            // or one a policy refuses, is the browser declining -- the preview
            // stays where it is, which is the correct outcome and not an error
            // worth showing.
            void stageNode.requestFullscreen().catch(() => undefined);
          }}
          type="button"
        >
          {fullscreen ? "退出全屏" : "全屏"}
        </button>
      ) : null}
      </div>
      {rendering ? (
        /* The fullscreen element, and the reason it is a wrapper rather than
           the frame: everything inside it survives the transition. The caution
           below is inside deliberately -- see the button's comment. */
        <div
          className="aw-preview-stage"
          data-fullscreen={fullscreen ? "yes" : "no"}
          ref={setStageNode}
        >
          {/* Above the frame, not below it. What is promised here is exactly
              what is guaranteed -- the earlier wording also claimed the page
              could not reach the internet, which is best-effort rather than
              true (known-gaps F-12) -- and a reader who opens an unknown page
              on the strength of an overstated promise is the person that gap
              costs.

              The position is the second half of the same argument, and it is
              an ADR-066 change rather than a cosmetic one. An HTML artifact is
              `free`: showing it *is* checking it, so this frame paints itself
              without being asked, and a caution printed underneath arrives
              after the thing it is a caution about has already run. Whatever
              this note is worth, it is worth it before the load, and 24
              characters of text costs nothing to read on the way past. */}
          <p className="aw-page-note">
            页面在隔离的沙箱里运行：拿不到你的登录态，也读不到平台数据。
            它仍可能自行访问外部网络，来源不明的页面请谨慎打开。
          </p>
          <div
            className="aw-preview-frame aw-html-frame"
            data-scale={scaled === null ? "actual" : "fit"}
            ref={setBoxNode}
          >
            <iframe
              referrerPolicy="no-referrer"
              sandbox="allow-scripts"
              srcDoc={withPreviewCsp(text)}
              // In 适应宽度 the frame is laid out at a logical desktop width and
              // scaled down to the box, so the page sees the viewport it was
              // written for. In 实际大小 nothing is set and the iframe fills the
              // box at 100%, which is what it always did.
              style={
                scaled === null
                  ? undefined
                  : {
                      width: `${String(scaled.width)}px`,
                      height: `${String(scaled.height)}px`,
                      transform: `scale(${String(scaled.factor)})`,
                    }
              }
              title={`${name} 预览`}
            />
          </div>
        </div>
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
