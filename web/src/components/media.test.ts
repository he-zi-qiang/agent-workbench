import { describe, expect, it } from "vitest";
import {
  DOCX_MEDIA_TYPE,
  checkCost,
  effectiveMediaType,
  isPreviewable,
  isReadableMedia,
  isRunnablePython,
  previewKind,
} from "./media";

// The whole point of one vocabulary is that every surface answers the same
// way, so the matrix is pinned here once instead of implicitly in each page's
// tests. A new kind lands as a new row, not as a new boolean.
describe("previewKind", () => {
  it.each([
    ["text/plain", "text"],
    ["text/markdown", "text"],
    ["application/json", "text"],
    ["application/x-ndjson+json", "text"],
    [DOCX_MEDIA_TYPE, "docx"],
    ["image/png", "image"],
    ["image/jpeg", "image"],
    // Safe because the viewer is `<img>`: an image element rasterises, so an
    // SVG's scripts never run there.
    ["image/svg+xml", "image"],
    ["application/pdf", "pdf"],
    // Before the readable check, which would otherwise claim them as text:
    // a page renders in the sandbox frame, with its source behind a toggle.
    ["text/html", "html"],
    ["application/xhtml+xml", "html"],
    ["application/zip", "none"],
    ["application/octet-stream", "none"],
    // Excel is real and unhandled: no viewer exists for it, so it must say so
    // rather than fall into a nearby bucket.
    [
      "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
      "none",
    ],
  ])("%s → %s", (mediaType, expected) => {
    expect(previewKind(mediaType)).toBe(expected);
  });

  it("keeps isPreviewable as exactly previewKind !== none", () => {
    for (const mediaType of [
      "text/plain",
      DOCX_MEDIA_TYPE,
      "image/png",
      "application/pdf",
      "text/html",
      "application/zip",
    ]) {
      expect(isPreviewable(mediaType)).toBe(previewKind(mediaType) !== "none");
    }
  });

  it("keeps docx out of the fetch-as-text set", () => {
    // A .docx is readable only through the extraction endpoint; admitting it
    // here would send a plain blob fetch at a zip.
    expect(isReadableMedia(DOCX_MEDIA_TYPE)).toBe(false);
  });
});

// Not a `PreviewKind` on purpose, and the first test here is what that costs
// and buys: a `.py` stays `text` for every surface that shows a stored file --
// Work's artifact panel has no working set to run one in -- and the Code
// viewer asks this second question on top (ADR-065).
describe("isRunnablePython", () => {
  it("leaves a .py as text for every surface that only shows files", () => {
    expect(previewKind("text/x-python")).toBe("text");
  });

  it.each([
    ["text/x-python", "sq.py", true],
    ["text/x-python-script", "sq.py", true],
    // What a person's browser guessed when they attached one. An uploaded
    // script is exactly as runnable as a written one.
    ["text/plain", "sq.py", true],
    ["application/octet-stream", "sq.py", true],
    // Typed by this project's own writer, whatever the name says.
    ["text/x-python; charset=utf-8", "script", true],
    ["text/markdown", "notes.md", false],
    ["text/html", "page.html", false],
    // Not a suffix match anywhere in the name: `.py` has to end it.
    ["text/plain", "py.notes", false],
    ["text/plain", "spy.txt", false],
  ])("%s / %s → %s", (mediaType, name, expected) => {
    expect(isRunnablePython(mediaType, name)).toBe(expected);
  });
});

// The second vocabulary, pinned as a matrix for the same reason the first one
// is: three surfaces read it, and a divergence between them is exactly the
// bug it exists to stop.
describe("checkCost", () => {
  // What each surface knows about itself. Written out rather than imported
  // from the pages, because the point of the test is that these three rows are
  // the *only* difference between them.
  const CODE = { canRun: true, canConvert: false };
  const WORK = { canRun: false, canConvert: true };
  const CHAT = { canRun: false, canConvert: false };

  it.each([
    // Showing is checking: one paint, one decode, done.
    ["image/png", "plot.png", CODE, "free"],
    ["image/svg+xml", "diagram.svg", WORK, "free"],
    ["text/html", "report.html", CODE, "free"],
    ["text/html", "report.html", WORK, "free"],
    // Every byte on screen, and only a person knows if they are right.
    ["text/markdown", "report.md", CODE, "reader"],
    ["application/json", "data.json", WORK, "reader"],
    ["text/csv", "rows.csv", CODE, "reader"],
    ["application/pdf", "paper.pdf", WORK, "reader"],
    // The asymmetry ADR-065 left behind, now stated by one table instead of
    // by two unrelated comments in two files: a .py is checked by running it
    // and only Code can run one; a .docx is checked by laying it out and only
    // Work can convert one.
    ["text/x-python", "sq.py", CODE, "one-action"],
    ["text/x-python", "sq.py", WORK, "elsewhere"],
    ["text/plain", "uploaded.py", CODE, "one-action"],
    [DOCX_MEDIA_TYPE, "report.docx", WORK, "one-action"],
    [DOCX_MEDIA_TYPE, "report.docx", CODE, "elsewhere"],
    // Nothing here checks these, and that is a sentence the console can say.
    ["application/zip", "bundle.zip", CODE, "unchecked"],
    [
      "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
      "sheet.xlsx",
      WORK,
      "unchecked",
    ],
    ["application/octet-stream", "thing.bin", CHAT, "unchecked"],
  ])("%s / %s → %s", (mediaType, name, abilities, expected) => {
    expect(checkCost(mediaType, name, abilities)).toBe(expected);
  });

  it("calls a PDF unchecked in a browser that will not paint one", () => {
    // Not `reader`: that frame shows a flat empty rectangle, no error and no
    // event. Claiming the bytes are on screen would be the console's own lie.
    expect(
      checkCost("application/pdf", "paper.pdf", { ...WORK, showsPdfInline: false }),
    ).toBe("unchecked");
    // Absent means "this browser was never asked", and the honest default is
    // to try -- the same polarity `browserShowsPdfInline` chose.
    expect(checkCost("application/pdf", "paper.pdf", WORK)).toBe("reader");
  });

  it("never promises an action the surface cannot perform", () => {
    // The one invariant worth stating as a law rather than as rows: a surface
    // that can neither run nor convert must never be told a click would help.
    for (const [mediaType, name] of [
      ["text/x-python", "sq.py"],
      [DOCX_MEDIA_TYPE, "report.docx"],
      ["application/pdf", "paper.pdf"],
      ["image/png", "plot.png"],
      ["text/plain", "notes.txt"],
      ["application/zip", "bundle.zip"],
    ] as const) {
      expect(checkCost(mediaType, name, CHAT)).not.toBe("one-action");
    }
  });

  it("leaves previewKind answering only its own question", () => {
    // The pair that made this vocabulary necessary: same viewer, opposite
    // costs. Both are `text`; one is read, the other has to be run.
    expect(previewKind("text/x-python")).toBe(previewKind("text/markdown"));
    expect(checkCost("text/x-python", "sq.py", CODE)).not.toBe(
      checkCost("text/markdown", "notes.md", CODE),
    );
  });
});

describe("effectiveMediaType", () => {
  it.each([
    // The case this exists for: a browser sends an empty `file.type` for a
    // suffix it does not know, the upload route honestly records
    // `application/octet-stream`, and the reader's own note became an
    // unviewable blob whose only offer was 下载.
    ["application/octet-stream", "notes.md", "text/markdown"],
    ["", "notes.md", "text/markdown"],
    ["application/octet-stream", "chart.png", "image/png"],
    ["application/octet-stream", "chart.jpg", "image/jpeg"],
    ["application/octet-stream", "sq.py", "text/x-python"],
    ["application/octet-stream", "page.html", "text/html"],
  ])("reads %s / %s as %s", (mediaType, name, expected) => {
    expect(effectiveMediaType(mediaType, name)).toBe(expected);
  });

  it.each([
    // A writer that said `text/plain` made a claim, and a suffix table is not
    // entitled to overrule it -- this only ever fills a silence.
    ["text/plain", "chart.png"],
    ["text/markdown", "notes.txt"],
    ["image/png", "thing.bin"],
    // Nothing known about the name either: the honest answer stands.
    ["application/octet-stream", "archive.rar"],
    ["application/octet-stream", "noext"],
  ])("leaves %s alone for %s", (mediaType, name) => {
    expect(effectiveMediaType(mediaType, name)).toBe(mediaType);
  });

  it("puts an uploaded note within reach of a viewer", () => {
    // The whole point, stated as the two vocabularies together: the guess is
    // worthless unless it changes what the console can do with the file.
    const raw = "application/octet-stream";
    expect(previewKind(raw)).toBe("none");
    expect(previewKind(effectiveMediaType(raw, "notes.md"))).toBe("text");
  });
});
