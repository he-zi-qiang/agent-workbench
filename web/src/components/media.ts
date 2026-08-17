/**
 * What a surface can do with a stored file, decided from its media type.
 *
 * These lived inside the Work page while Work was the only place that showed a
 * produced file. A coding session's working set is the second, and it asks the
 * same question about the same kind of bytes -- so the answer moved here rather
 * than being written a second time. Two copies of "is this text?" is how one of
 * them ends up recognising `application/x-ndjson` and the other not.
 */

/** What a .docx is on the wire. Long enough to be worth naming once. */
export const DOCX_MEDIA_TYPE =
  "application/vnd.openxmlformats-officedocument.wordprocessingml.document";

/**
 * Which viewer a media type gets, in one word.
 *
 * This replaces a pair of booleans (`readable`, `isDocument`) whose falses
 * multiplied: every surface that showed a file re-derived "so what do I do
 * with an image?" from their negation, and the answer each had reached was
 * "download it, silently" -- which is how clicking a produced .png saved a
 * file instead of showing a picture. One closed vocabulary, one place to add
 * the next kind.
 */
export type PreviewKind = "text" | "docx" | "image" | "pdf" | "html" | "none";

export function previewKind(mediaType: string): PreviewKind {
  // Before the readable check, which would otherwise swallow it: `text/html`
  // is readable, but showing an agent-built page as its own source (or worse,
  // feeding it to a Markdown renderer that sanitises it to nothing, which is
  // what the Work page used to do) answers a different question than the one
  // the click asked. The viewer runs it in an opaque-origin sandbox frame --
  // see `HtmlPreview` for why that is safe to admit.
  if (mediaType === "text/html" || mediaType === "application/xhtml+xml") {
    return "html";
  }
  if (isReadableMedia(mediaType)) return "text";
  if (mediaType === DOCX_MEDIA_TYPE) return "docx";
  // Through `<img>`, which is why every image/* subtype is safe to admit:
  // an image element rasterises -- an SVG's scripts never run there. That is
  // also why SVG stays here rather than joining `html`: rasterised it is
  // already visible, and the rare scripted SVG is not worth a second
  // executable surface.
  if (mediaType.startsWith("image/")) return "image";
  if (mediaType === "application/pdf") return "pdf";
  return "none";
}

/** Whether opening this file shows it, or can only save it. */
export function isPreviewable(mediaType: string): boolean {
  return previewKind(mediaType) !== "none";
}

/**
 * Text a page can render *by fetching it*. A .docx is deliberately not here:
 * it is readable too, but only through the server's extraction endpoint, and
 * folding it in would send a plain blob fetch at a zip.
 */
export function isReadableMedia(mediaType: string): boolean {
  return (
    mediaType.startsWith("text/") ||
    mediaType === "application/json" ||
    mediaType.endsWith("+json")
  );
}

/**
 * Whether this browser paints a PDF inside a frame.
 *
 * The layout view is the browser's own PDF viewer and nothing else -- this app
 * ships no renderer -- so a browser without one leaves the frame showing its
 * empty backdrop: a flat dark rectangle, no error, no event, nothing saying
 * why. Embedded browsers are where this bites (an app's built-in web view
 * rather than a browser window), and the reader has no reason to suspect the
 * viewer rather than the document.
 *
 * `!== false` rather than `=== true` on purpose. The property is absent in
 * browsers too old to have been asked, and the honest default there is to try:
 * a frame that works is the good outcome, and a frame that does not is covered
 * by the note under it -- because this check catches only browsers that *admit*
 * it. One that reports `true` and then paints nothing is exactly what a
 * Chromium-based web view does, and no property will say so.
 */
export function browserShowsPdfInline(): boolean {
  return navigator.pdfViewerEnabled !== false;
}
