/**
 * One workspace file's body, by what its type allows.
 *
 * Lifted out of `CodePage` because it now has two callers that must not
 * disagree: the card inside a turn (`CodeTurn`) and the panel on the right
 * (`PreviewPanel`). Two copies would drift on the question this component
 * exists to answer -- what a .docx does, whether an oversized file is fetched
 * before it is refused -- and the drift would show as the same file rendering
 * differently depending on where it was clicked.
 *
 * **Every kind fetches for itself.** Text used to be the exception: the page
 * read it when the file was opened and passed the string in, which is why the
 * inline card -- where nothing had prefetched anything -- could show an image
 * and an HTML page and not a `.py`. On a coding console that is the wrong file
 * to leave out. Now `TextPreview` owns that fetch the way `BlobPreview` owns
 * the blob one and `HtmlPreview` its own, all three keyed so that previewing a
 * file inline and then opening it in the panel transfers it once.
 *
 * A .docx lands on the download-only sentence on purpose: the conversion
 * endpoints are artifact-addressed and a workspace file has no artifact id a
 * client may hold (known-gaps F-11).
 */

import {
  getCodeWorkspaceFileBlob,
  getCodeWorkspaceFileText,
} from "../../api/client";
import type { PrincipalIdentity } from "../../api/types";
import { BlobPreview } from "../../components/BlobPreview";
import { HtmlPreview } from "../../components/HtmlPreview";
import { TextPreview } from "../../components/TextPreview";
import { previewKind } from "../../components/media";

/**
 * The file a reader has open.
 *
 * `sessionId` is carried rather than read from the URL at download time: the
 * two are the same until the reader switches sessions with the viewer open,
 * and then they are not -- which would download one session's name against
 * another's working set and answer 404 for a file the reader can see.
 */
export interface OpenedFile {
  sessionId: string;
  name: string;
  mediaType: string;
  sizeBytes: number;
}

export function FilePreview({
  identity,
  viewing,
}: {
  identity: PrincipalIdentity;
  viewing: OpenedFile;
}) {
  const kind = previewKind(viewing.mediaType);
  if (kind === "image" || kind === "pdf") {
    return (
      <BlobPreview
        kind={kind}
        load={() =>
          getCodeWorkspaceFileBlob(identity, viewing.sessionId, viewing.name)
        }
        name={viewing.name}
        queryKey={["code-file-blob", viewing.sessionId, viewing.name]}
        sizeBytes={viewing.sizeBytes}
      />
    );
  }
  if (kind === "html") {
    // Runs in HtmlPreview's sandbox frame, with the source behind its 源码
    // toggle -- one fetch serving both views.
    return (
      <HtmlPreview
        load={() =>
          getCodeWorkspaceFileText(identity, viewing.sessionId, viewing.name)
        }
        name={viewing.name}
        queryKey={["code-file-html", viewing.sessionId, viewing.name]}
        sizeBytes={viewing.sizeBytes}
      />
    );
  }
  if (kind === "text") {
    return (
      <TextPreview
        load={() =>
          getCodeWorkspaceFileText(identity, viewing.sessionId, viewing.name)
        }
        queryKey={["code-file-text", viewing.sessionId, viewing.name]}
      />
    );
  }
  // Reached by `docx` and `none`. Not fetched at all: reading a zip as text
  // renders mojibake, and transferring bytes only to decide not to show them
  // spends the transfer for nothing.
  return <p className="aw-code-value">这个类型只能下载。</p>;
}
