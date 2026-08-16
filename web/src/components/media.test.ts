import { describe, expect, it } from "vitest";
import {
  DOCX_MEDIA_TYPE,
  isPreviewable,
  isReadableMedia,
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
