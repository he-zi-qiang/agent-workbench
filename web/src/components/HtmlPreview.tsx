import { useQuery } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
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
 * The one script this console puts into somebody else's page, and what it buys.
 *
 * A page that throws on load paints nothing. So does a page that has not
 * started drawing yet, and a canvas game whose first frame is black. Three
 * different situations, one rectangle, and the reader has no way to tell them
 * apart -- which is the gap this closes. 2026-09-12, the report that prompted
 * it: a coding turn wrote a 40 KB game, could not run it (this deployment's
 * project sessions hold no execution tool at all, ADR-0109 §3.3), and said so
 * honestly -- "all the claims about playability above are from reading the
 * code, not from watching it". The frame beside that sentence had already run
 * the page. Nobody was listening to it.
 *
 * **It reports, it does not repair.** Three listeners and a `console.error`
 * wrapper, each forwarding one string to the parent. Nothing here changes what
 * the page does, and the original `console.error` is still called.
 *
 * **`postMessage` is not a hole in the sandbox.** The frame has an opaque
 * origin (no `allow-same-origin`, see the component), so the message arrives
 * with `origin: "null"` and the parent cannot use origin to authenticate it --
 * it compares `event.source` against this frame's own `contentWindow` instead.
 * What crosses is a string the page wrote, treated as what it is: untrusted
 * text, capped in length here and in count, rendered as text by React and
 * never as markup.
 *
 * Capture phase on `error`, because resource failures (a missing script, an
 * image that 404s behind `img-src`) do not bubble; those arrive with no
 * `message`, which is why there is a fallback sentence for them.
 */
const PREVIEW_REPORTER =
  "<script>(function(){var n=0;function s(t){if(n>=20)return;n++;" +
  'try{parent.postMessage({awPreviewError:String(t).slice(0,300)},"*")}' +
  "catch(e){}}" +
  'addEventListener("error",function(e){s(e&&e.message?e.message:' +
  '"资源加载失败（脚本、图片或样式没取到）")},true);' +
  'addEventListener("unhandledrejection",function(e){var r=e&&e.reason;' +
  's("未处理的 Promise 拒绝："+((r&&r.message)||r))});' +
  "var c=console.error;console.error=function(){" +
  'try{s(Array.prototype.map.call(arguments,String).join(" "))}catch(e){}' +
  "return c.apply(console,arguments)};})();</script>"

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
function insertEarly(html: string, markup: string): string {
  // `<head(\s…)?>` and not `<head[^>]*>`: the loose form also matches
  // `<header>`, and a page that opens with one -- an ordinary shape for a
  // generated fragment -- would take the head branch and have the meta
  // planted inside the implicit body, where a browser discards it outright.
  const head = /<head(\s[^>]*)?>/i.exec(html);
  if (head !== null) {
    const at = head.index + head[0].length;
    return html.slice(0, at) + markup + html.slice(at);
  }
  const root = /<html(\s[^>]*)?>/i.exec(html);
  if (root !== null) {
    const at = root.index + root[0].length;
    return html.slice(0, at) + markup + html.slice(at);
  }
  const doctype = /^\s*<!doctype[^>]*>/i.exec(html);
  if (doctype !== null) {
    const at = doctype.index + doctype[0].length;
    return html.slice(0, at) + markup + html.slice(at);
  }
  return markup + html;
}


/**
 * The CSP alone, which is what this module's oldest tests pin.
 *
 * Kept separate from `preparedPreview` rather than folded into it: the
 * placement rules below are a security argument with its own suite, and a
 * function that also injected a script would make those tests read as though
 * they were about the script.
 */
export function withPreviewCsp(html: string): string {
  return insertEarly(html, CSP_META);
}

/**
 * What actually goes into the frame: the policy, then the reporter, then the
 * page.
 *
 * One insertion rather than two, and the order inside it is the argument. The
 * reporter is a script, so it has to be governed by the policy that precedes
 * it; inserting them separately would put whichever ran last in front, and
 * "in front" is exactly where the meta has to be.
 */
export function preparedPreview(html: string): string {
  return insertEarly(html, CSP_META + PREVIEW_REPORTER);
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
  onFaults,
  onReport,
  queryKey,
  sizeBytes,
}: {
  /** Fetches the source; called once and cached under `queryKey`. */
  load: () => Promise<{ text: string; truncated: boolean }>;
  name: string;
  /**
   * Hands what the page reported back to whoever can act on it.
   *
   * Optional, and absent is the ordinary case: a Task artifact has no next
   * turn to give it to. Code passes one, and that is what closes the loop the
   * model cannot close itself -- it wrote the page, this frame ran it, and
   * until now the only thing between "it throws" and "fix it" was the reader
   * retyping the error.
   */
  onReport?: (report: string) => void;
  /**
   * Told what this page has reported about itself, as it arrives.
   *
   * Separate from `onReport`, which is one reader's decision to hand it over.
   * This one is the fact: the page said these things. A caller that wants to
   * act without being asked reads this; a caller that only draws reads
   * neither.
   */
  onFaults?: (faults: readonly string[], name: string) => void;
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
  const frameRef = useRef<HTMLIFrameElement | null>(null);
  // What the page reported about itself, and which document reported it.
  //
  // The document is carried *with* the list rather than cleared from an effect
  // when the source changes. Two reasons, and the second is the one that
  // decided it: clearing from an effect is a `setState` in an effect body,
  // which this codebase's lint rule rejects wherever it appears; and a stale
  // list would be worse than none -- errors from the previous version of a
  // page, printed under the new one, read as the new one's.
  const [faults, setFaults] = useState<{ doc: string; list: string[] }>({
    doc: "",
    list: [],
  });
  // The same value, held where the message handler can read it synchronously.
  //
  // Not a convenience: the dedup below has to see what the *previous* message
  // produced, and `onFaults` has to be called with the result -- neither is
  // available inside a functional `setState` updater, which React is free to
  // run twice. Reading state in the handler instead would read the value from
  // the render the listener was created in.
  const held = useRef<{ doc: string; list: string[] }>({ doc: "", list: [] });
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

  // The source this frame is currently showing, available before the early
  // returns below so the listener may depend on it. `""` while it loads, which
  // never matches a real document and so shows nothing.
  const showing = sourceQuery.data?.text ?? "";
  useEffect(() => {
    const onMessage = (event: MessageEvent) => {
      const frame = frameRef.current;
      // `event.origin` is `"null"` for an opaque-origin frame and therefore
      // authenticates nothing. The window identity does: only the frame this
      // component rendered can be its own `contentWindow`.
      if (frame === null || event.source !== frame.contentWindow) return;
      const data: unknown = event.data;
      const reported =
        typeof data === "object" &&
        data !== null &&
        "awPreviewError" in data &&
        typeof data.awPreviewError === "string"
          ? data.awPreviewError
          : null;
      if (reported === null) return;
      const current = held.current;
      let next: { doc: string; list: string[] };
      if (current.doc !== showing) {
        next = { doc: showing, list: [reported] };
      } else if (
        // Deduplicated and capped. A game loop that throws draws sixty frames
        // a second and would otherwise report sixty identical lines a second;
        // what the reader needs is *that* it throws and what it says, once.
        current.list.includes(reported) ||
        current.list.length >= 20
      ) {
        return;
      } else {
        next = { doc: showing, list: [...current.list, reported] };
      }
      held.current = next;
      setFaults(next);
      onFaults?.(next.list, name);
    };
    window.addEventListener("message", onMessage);
    return () => {
      window.removeEventListener("message", onMessage);
    };
  }, [name, onFaults, showing]);

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
        <div className="aw-segmented" aria-label="显示比例">
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
            页面在隔离的沙箱里运行：拿不到你的登录态，也读不到平台数据。它仍可能自行访问外部网络，来源不明的页面请谨慎打开。
          </p>
          <div
            className="aw-preview-frame aw-html-frame"
            data-scale={scaled === null ? "actual" : "fit"}
            ref={setBoxNode}
          >
            <iframe
              referrerPolicy="no-referrer"
              sandbox="allow-scripts"
              ref={frameRef}
              srcDoc={preparedPreview(text)}
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
          {faults.doc !== text || faults.list.length === 0 ? null : (
            /* Under the frame, because it is an outcome rather than a warning
               -- the caution above is about opening the page at all, this is
               about what happened when it ran. Inside the stage, so it is
               still there in fullscreen, where a blank canvas is at its most
               convincing.

               The text is the page's own. Rendered as text by React and never
               as markup, capped at 20 lines here and 300 characters in the
               reporter: it is untrusted content that happens to be useful. */
            <div className="aw-page-note aw-preview-faults" role="status">
              <strong>
                这个页面运行时报了 {faults.list.length} 条错误
              </strong>
              <ul>
                {faults.list.map((fault) => (
                  <li key={fault}>{fault}</li>
                ))}
              </ul>
              {onReport === undefined ? null : (
                <button
                  className="aw-button"
                  onClick={() => {
                    // Composed here rather than at the call site: this
                    // component is the only thing that knows both the file's
                    // name and what it said, and a caller assembling the
                    // sentence from two props would be a second place the
                    // wording lives.
                    onReport(
                      `预览里运行 ${name} 时报了这些错误，请修掉：\n` +
                        faults.list.map((fault) => `- ${fault}`).join("\n"),
                    );
                  }}
                  type="button"
                >
                  把这些错误交给它
                </button>
              )}
            </div>
          )}
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
