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

/** Whether opening this file shows it, or can only save it. */
export function isPreviewable(mediaType: string): boolean {
  return mediaType === DOCX_MEDIA_TYPE || isReadableMedia(mediaType);
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
