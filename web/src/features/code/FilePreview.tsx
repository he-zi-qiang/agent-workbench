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
 * Text arrives already fetched through `viewing`; images and PDFs fetch on
 * render through `BlobPreview`, which owns the size cap and the object-URL
 * lifetime; HTML fetches inside `HtmlPreview`, which owns both the sandbox
 * frame and the 源码 toggle. A .docx lands on the download-only sentence on
 * purpose: the conversion endpoints are artifact-addressed and a workspace
 * file has no artifact id a client may hold (known-gaps F-11).
 */

import {
  getCodeWorkspaceFileBlob,
  getCodeWorkspaceFileText,
} from "../../api/client";
import type { PrincipalIdentity } from "../../api/types";
import { BlobPreview } from "../../components/BlobPreview";
import { HtmlPreview } from "../../components/HtmlPreview";
import { previewKind } from "../../components/media";

/**
 * The file a reader has open, and what could be shown of it.
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
  loading: boolean;
  text: string | null;
  truncated: boolean;
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
    // toggle -- so the caller does not prefetch it as text the way it does for
    // the text kind; the component owns its one fetch for both views.
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
  if (viewing.text === null) {
    return (
      <p className="aw-code-value">
        {viewing.loading ? "正在读取" : "这个类型只能下载。"}
      </p>
    );
  }
  return (
    <>
      <pre className="aw-code-file-body">{viewing.text}</pre>
      {viewing.truncated ? (
        <p className="aw-code-value">只显示了开头一部分，完整内容请下载。</p>
      ) : null}
    </>
  );
}
