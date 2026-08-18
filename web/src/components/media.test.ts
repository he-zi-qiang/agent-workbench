import { describe, expect, it } from "vitest";
import {
  DOCX_MEDIA_TYPE,
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
