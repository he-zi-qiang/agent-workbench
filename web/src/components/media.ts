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
 * What a name says about its bytes, when the stored type says nothing.
 *
 * Only ever consulted for `application/octet-stream` and the empty string --
 * the two values that mean "nobody knew", not the ones that mean something.
 * A stored `text/plain` stays `text/plain` even for a name ending `.png`,
 * because a writer that said `text/plain` was making a claim and this table is
 * not entitled to overrule it.
 *
 * The case this exists for: a person attaches `notes.md` to a coding session.
 * `PUT /v1/code/sessions/{id}/workspace/{name}` types an upload from the
 * request's own `content-type` and defaults to `application/octet-stream`
 * (`routes/code.py`), and a browser sends an empty `file.type` for any suffix
 * it does not recognise -- `.md` among them. So the reader's own note landed
 * in the console as an unviewable blob, and the only thing on offer for it was
 * 下载.
 *
 * Deliberately **not** fixed by teaching that route to guess from the suffix.
 * Its docstring argues the opposite position on purpose -- an upload is typed
 * by what the client said, and "honestly unknown" beats "confidently guessed"
 * for a value that is written into a manifest and cannot be revised. This is
 * the same information applied where it is cheap and reversible: a display
 * decision, in the browser, that no authorization reads. `isRunnablePython`
 * already draws that line for the same reason.
 */
const NAME_MEDIA_TYPES: ReadonlyArray<readonly [string, string]> = [
  [".md", "text/markdown"],
  [".markdown", "text/markdown"],
  [".txt", "text/plain"],
  [".log", "text/plain"],
  [".csv", "text/csv"],
  [".json", "application/json"],
  [".jsonl", "application/json"],
  [".ndjson", "application/json"],
  [".html", "text/html"],
  [".htm", "text/html"],
  [".py", "text/x-python"],
  [".js", "text/plain"],
  [".ts", "text/plain"],
  [".tsx", "text/plain"],
  [".css", "text/plain"],
  [".toml", "text/plain"],
  [".yaml", "text/plain"],
  [".yml", "text/plain"],
  [".xml", "text/plain"],
  [".sh", "text/plain"],
  [".png", "image/png"],
  [".jpg", "image/jpeg"],
  [".jpeg", "image/jpeg"],
  [".gif", "image/gif"],
  [".webp", "image/webp"],
  [".svg", "image/svg+xml"],
  [".pdf", "application/pdf"],
];

/** The media type a surface should display this file as. */
export function effectiveMediaType(mediaType: string, name: string): string {
  const base = mediaType.split(";")[0]?.trim().toLowerCase() ?? "";
  if (base !== "" && base !== "application/octet-stream") return mediaType;
  const lowered = name.toLowerCase();
  for (const [suffix, guessed] of NAME_MEDIA_TYPES) {
    if (lowered.endsWith(suffix)) return guessed;
  }
  return mediaType;
}

/**
 * What a `.py` is called on the wire, and why the name is asked as well.
 *
 * `WorkspaceWriteTool` and the sandbox's own output labeller both type a `.py`
 * as `text/x-python`, so a file this project produced always carries it. A
 * file a *person* attached carries whatever their browser guessed -- usually
 * `text/plain`, sometimes `application/octet-stream` -- and an uploaded script
 * is exactly as runnable as a written one. The suffix is therefore a second
 * answer to the same question, not a fallback for a broken case.
 */
const PYTHON_MEDIA_TYPES = new Set(["text/x-python", "text/x-python-script"]);

/**
 * Whether this file is one a coding session can *run* (ADR-065).
 *
 * Deliberately **not** a sixth `PreviewKind`. A kind answers "which viewer",
 * and every surface that shows a stored file reads that vocabulary -- Work's
 * artifact panel included, where a `.py` is a document to read and there is no
 * working set to run it in. Widening the shared enum would have made every one
 * of those surfaces answer a question only one of them can. A `.py` stays
 * `text` everywhere, and this is the extra thing the Code viewer asks on top.
 *
 * The server checks the same two things again on its own terms
 * (`routes/code.py`), because a client-side gate is a UI affordance and never
 * an authorization.
 */
export function isRunnablePython(mediaType: string, name: string): boolean {
  return PYTHON_MEDIA_TYPES.has(mediaType.split(";")[0]?.trim() ?? "")
    || name.endsWith(".py");
}

/**
 * What a surface can do beyond painting bytes, as three answers it knows about
 * itself.
 *
 * An object rather than the surface's *name* (`"code" | "work" | "chat"`), and
 * that is the whole reason `checkCost` is allowed to exist next to
 * `previewKind` at all. ADR-065 §4 refused a sixth `PreviewKind` because the
 * enum is read by surfaces that cannot answer "can this run?" -- widening it
 * would have made every one of them answer a question only one of them can.
 * A union of surface names re-creates exactly that coupling one module over:
 * the shared layer would once again know how many surfaces exist and what they
 * are called, and adding a fourth would mean editing this file. Three booleans
 * do not.
 *
 * `showsPdfInline` is optional and defaults to true, matching
 * `browserShowsPdfInline`'s deliberate optimism (see its own note): the
 * property is absent in browsers too old to have been asked, and the honest
 * default there is to try.
 */
export interface SurfaceAbilities {
  /** Whether a script can be run here -- a working set stands behind it. */
  canRun: boolean;
  /** Whether a document can be converted to a layout here. */
  canConvert: boolean;
  /** Whether this browser paints a PDF inside a frame. */
  showsPdfInline?: boolean;
}

/**
 * What it costs the reader to find out whether a produced file is any good.
 *
 * The second half of a pair whose halves used to be one word. `previewKind`
 * answers "which element displays these bytes"; four places had quietly grown
 * a private answer to the *other* question -- whether the reader can tell the
 * file is right -- and each of them was a boolean whose false meant something
 * different: `isRunnablePython` (a .py is checked by running it),
 * `HtmlPreview`'s 渲染/源码 (a page is checked by painting it), Work's
 * `textFor` (a .docx is checked by converting it), `browserShowsPdfInline` (a
 * PDF cannot be checked at all in a viewer that will not paint one). Four
 * booleans, no shared vocabulary, and no surface able to say the one sentence
 * that mattered: *nothing here can check this file.*
 *
 * The evidence that these are two questions and not one is a measured
 * failure: a script exits 0, prints 已生成, and the chart it wrote has hollow
 * boxes where every Chinese label should be, because matplotlib's default font
 * has no CJK glyphs. Exit code, stdout and stderr all say success. `previewKind`
 * says `image`. Only *looking at the picture* says otherwise -- and until this
 * existed, nothing in the console distinguished "shown" from "checked".
 *
 * The five values are a cost, not a quality: they say how much the reader must
 * spend, never whether the file passed. Nothing in this console records that a
 * file was checked -- see ADR-066 §6 for why a stored 已验收 would be a claim
 * this system cannot back.
 *
 * Pure, and pure on purpose. The other place "cannot be checked here" is
 * discovered is *after* a run, from CPython's own strings in a traceback
 * (`containerLimitNote`), and that one deliberately does not fold in here:
 * a function of (media type, abilities) cannot know what a container said, and
 * a value that had to be re-derived at each call site is precisely the disease
 * `previewKind`'s own docstring records.
 */
export type CheckCost =
  /** One paint or one decode is the answer. Showing it *is* checking it. */
  | "free"
  /** Every byte is already on screen; whether they are right is a person's
      job. Text, and a PDF a browser will paint. */
  | "reader"
  /** One more action checks it, and this surface offers that action. */
  | "one-action"
  /** One more action checks it, but not here. The reader should be told
      where, rather than handed the sentence reserved for "no viewer". */
  | "elsewhere"
  /** This console has no action that would check it. Said out loud. */
  | "unchecked";

export function checkCost(
  mediaType: string,
  name: string,
  abilities: SurfaceAbilities,
): CheckCost {
  const kind = previewKind(mediaType);
  if (kind === "image" || kind === "html") return "free";
  // A frame that paints is the whole document, scrollable -- the same standing
  // as text, and not `free`: page one of a forty-page report is not a check.
  // A browser that will not paint one shows a flat empty rectangle with no
  // error and no event, which is worse than saying so.
  if (kind === "pdf") {
    return abilities.showsPdfInline !== false ? "reader" : "unchecked";
  }
  if (kind === "docx") return abilities.canConvert ? "one-action" : "elsewhere";
  // Before the text arm that would otherwise swallow it, in the same order
  // `FilePreview` asks the two questions -- a .py is `text` everywhere.
  if (kind === "text" && isRunnablePython(mediaType, name)) {
    return abilities.canRun ? "one-action" : "elsewhere";
  }
  if (kind === "text") return "reader";
  return "unchecked";
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
 * What kind of file this is, in the words a card can show.
 *
 * Coarser than `previewKind` and for a different reader. `previewKind` answers
 * "which viewer", so `text/markdown` and `application/json` are both `text`;
 * a person looking at a list of produced files wants to know which of them is
 * the report and which is the data, and those two are not the same thing.
 *
 * Falls back to the media type itself rather than to a generic "文件". A type
 * nobody has named yet is a gap in this table, and printing `application/zip`
 * says that honestly while telling the reader more than the generic word does.
 */
export function mediaLabel(mediaType: string): string {
  const base = mediaType.split(";")[0]?.trim().toLowerCase() ?? "";
  if (base === "text/markdown" || base === "text/x-markdown") return "Markdown";
  if (base === "text/html" || base === "application/xhtml+xml") return "HTML";
  if (base === "text/csv") return "CSV";
  if (base === "text/plain") return "纯文本";
  if (base === "application/json" || base.endsWith("+json")) return "JSON";
  if (base === "application/pdf") return "PDF";
  if (base === DOCX_MEDIA_TYPE) return "Word";
  if (base.startsWith("image/")) return "图片";
  if (base.startsWith("text/")) return "纯文本";
  return mediaType;
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
