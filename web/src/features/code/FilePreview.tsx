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

import { useQueryClient } from "@tanstack/react-query";
import {
  getCodeWorkspaceFileBlob,
  getCodeWorkspaceFileText,
  runCodeWorkspaceFile,
} from "../../api/client";
import type { PrincipalIdentity } from "../../api/types";
import { BlobPreview } from "../../components/BlobPreview";
import { HtmlPreview } from "../../components/HtmlPreview";
import { PythonPreview } from "../../components/PythonPreview";
import { TextPreview } from "../../components/TextPreview";
import { isRunnablePython, previewKind } from "../../components/media";

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
  onWrote,
  viewing,
}: {
  identity: PrincipalIdentity;
  /**
   * Told when a run put files into the working set, so the page can re-read a
   * listing that is now out of date. Optional because only the Python viewer
   * can cause it, and a caller that never shows one has nothing to hear.
   */
  onWrote?: (names: string[]) => void;
  viewing: OpenedFile;
}) {
  const queries = useQueryClient();
  const kind = previewKind(viewing.mediaType);
  // Before the text branch that would otherwise swallow it, and asked as a
  // second question rather than as a sixth `PreviewKind`: a `.py` is text
  // everywhere else in this console, and running one is a thing only a session
  // with a working set behind it can offer (`media.ts`, ADR-065).
  if (kind === "text" && isRunnablePython(viewing.mediaType, viewing.name)) {
    const text = (
      <TextPreview
        load={() =>
          getCodeWorkspaceFileText(identity, viewing.sessionId, viewing.name)
        }
        queryKey={["code-file-text", viewing.sessionId, viewing.name]}
      />
    );
    return (
      <PythonPreview
        // Keyed, and this is not decoration. The panel keeps one tree position
        // and swaps `viewing` under it, so React reuses the component instance
        // -- and a `useMutation`'s result outlives a prop change. Observed:
        // clicking `maker.py`, running it, then clicking `broken.py` showed
        // 运行结束，退出码 0 and `maker.py`'s stdout under the heading
        // `broken.py`. There is no more misleading thing this viewer could
        // do than attribute one file's output to another.
        key={`${viewing.sessionId}/${viewing.name}`}
        name={viewing.name}
        onRan={(result) => {
          // A run can write files, and the two things that go stale are the
          // ones the reader is looking straight at: the listing that says what
          // the workspace holds, and any preview body cached under a name the
          // script rewrote. Every preview caches with `staleTime: Infinity`,
          // so without this a rewritten file shows its previous bytes forever.
          if (result.written.length === 0) return;
          onWrote?.(result.written);
          for (const written of result.written) {
            for (const prefix of [
              "code-file-text",
              "code-file-html",
              "code-file-blob",
            ]) {
              void queries.invalidateQueries({
                queryKey: [prefix, viewing.sessionId, written],
              });
            }
          }
        }}
        run={() =>
          runCodeWorkspaceFile(identity, viewing.sessionId, viewing.name)
        }
        source={text}
      />
    );
  }
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
