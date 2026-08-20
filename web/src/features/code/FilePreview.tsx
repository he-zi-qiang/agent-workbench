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
 * A .docx lands on a sentence of its own rather than on the generic
 * download-only one: the conversion endpoints are artifact-addressed and a
 * workspace file has no artifact id a client may hold (known-gaps F-11), so
 * the viewer exists and is merely out of reach here -- which is a different
 * thing to tell a reader than "nothing can show this".
 */

import { useQueryClient } from "@tanstack/react-query";
import {
  getCodeWorkspaceFileBlob,
  getCodeWorkspaceFileText,
  runCodeWorkspaceFile,
} from "../../api/client";
import type { PrincipalIdentity, WorkspaceEntryView } from "../../api/types";
import { BlobPreview } from "../../components/BlobPreview";
import { HtmlPreview } from "../../components/HtmlPreview";
import { PythonPreview } from "../../components/PythonPreview";
import { TextPreview } from "../../components/TextPreview";
import { isRunnablePython, previewKind } from "../../components/media";
import { FileCard } from "./FileCard";

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

/**
 * One file body's cache key, and the identity is not optional in it.
 *
 * These caches are `staleTime: Infinity`, and the `QueryClient` is created
 * once for the app (`Providers.tsx`) rather than per identity -- so the
 * `Outlet key={identityKey}` remount that protects every routed surface does
 * not touch them. Without the principal in the key, narrowing your own scopes
 * in the identity dialog leaves a file you had already read rendering straight
 * from cache, with no second trip past the server's authorization.
 */
function fileKey(
  prefix: string,
  identity: PrincipalIdentity,
  file: Pick<OpenedFile, "sessionId" | "name">,
): readonly unknown[] {
  return [
    prefix,
    identity.tenantId,
    identity.principalId,
    [...identity.scopes].sort(),
    file.sessionId,
    file.name,
  ];
}

/**
 * How large a file a run just wrote may be and still paint itself unasked.
 *
 * Smaller than a turn card's 64 KB, and for a different reason: this one only
 * ever admits images, and an image is decoded whole before anything appears.
 * 4 MB is far above any chart a script draws (a matplotlib PNG is tens of KB)
 * and far below the point where decoding it stalls the panel the reader is
 * reading. Past it the card is still there, still one click, still says its
 * size -- what it stops is spending a multi-megabyte decode nobody asked for.
 */
const AUTO_PREVIEW_WRITTEN_MAX_BYTES = 4 * 1024 * 1024;

export function FilePreview({
  files,
  identity,
  onOpen,
  onWrote,
  viewing,
}: {
  /**
   * The current listing, so a run's output can be shown as cards rather than
   * as a line of names. Optional: only the Python viewer needs it, and a
   * caller that never shows one has nothing to supply.
   */
  files?: readonly WorkspaceEntryView[];
  identity: PrincipalIdentity;
  /** Routes a produced file into the panel. Same optionality as `files`. */
  onOpen?: (name: string) => void;
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
        queryKey={fileKey("code-file-text", identity, viewing)}
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
                queryKey: fileKey(prefix, identity, {
                  ...viewing,
                  name: written,
                }),
              });
            }
          }
        }}
        renderWritten={(names, listing) =>
          producedCards({
            // The run's own listing first, the page's as the fallback. They
            // answer the same question, but the run's arrived *with* the names
            // -- so the card is drawable on the first render, where the page's
            // is still one refresh behind (known-gaps F-15). The fallback is
            // not dead code: a server older than the field sends no listing.
            files: listing ?? files,
            identity,
            names,
            onOpen,
            onWrote,
            sessionId: viewing.sessionId,
          })
        }
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
        queryKey={fileKey("code-file-blob", identity, viewing)}
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
        queryKey={fileKey("code-file-html", identity, viewing)}
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
        queryKey={fileKey("code-file-text", identity, viewing)}
      />
    );
  }
  // Reached by `docx` and `none`. Not fetched at all: reading a zip as text
  // renders mojibake, and transferring bytes only to decide not to show them
  // spends the transfer for nothing.
  //
  // Two sentences, not one, and the split is the point. "这个类型只能下载。"
  // covered a .docx and a .zip identically, and they are not the same
  // situation: this console *has* a layout viewer for a Word document, it just
  // cannot address a workspace file with it (known-gaps F-11), so a reader who
  // walks away from a .docx believing no viewer exists has been told something
  // false. A .zip has no viewer anywhere and saying so is the whole answer.
  return (
    <p className="aw-code-value">
      {kind === "docx"
        ? "Word 的版面预览目前只在任务产出里有——那两个转换端点按 artifact id 寻址，而工作区文件没有 id 可以交给浏览器。这里可以下载后用 Word 打开。"
        : "这个控制台没有能显示它的查看器，下载后用别的程序打开。"}
    </p>
  );
}

/**
 * The files one run wrote, as the cards a turn would have shown for them.
 *
 * Returns `null` -- and the caller falls back to naming them in a sentence --
 * whenever the listing cannot account for every name. The run's response and
 * the workspace listing arrive on two independent paths (`onRan` asks the page
 * to re-read the listing; the response is already here), so for one render
 * after a run the names exist and the entries do not. A card built then would
 * be a button that opens nothing and a size that reads "已不在工作区" about a
 * file written one second ago. All-or-nothing rather than per-file, because a
 * list where two names are cards and one is not reads as the third having
 * failed.
 */
function producedCards({
  files,
  identity,
  names,
  onOpen,
  onWrote,
  sessionId,
}: {
  files: readonly WorkspaceEntryView[] | undefined;
  identity: PrincipalIdentity;
  names: readonly string[];
  onOpen: ((name: string) => void) | undefined;
  onWrote: ((names: string[]) => void) | undefined;
  sessionId: string;
}): React.ReactNode {
  if (files === undefined || onOpen === undefined) return null;
  const resolved = names.map((name) => ({
    name,
    entry: files.find((held) => held.name === name),
  }));
  if (resolved.some((one) => one.entry === undefined)) return null;

  return (
    <ul aria-label="这次运行写出的文件" className="aw-code-outputs">
      {resolved.map(({ entry, name }) => (
        <FileCard
          // The loop gate (see `FileCard`'s own note): a `.py` a script just
          // wrote gets no 运行 button inside the output of the run that wrote
          // it. It is still one click away in the panel.
          abilities={{ canRun: false, canConvert: false }}
          // `free` opens itself, which is the entire reader-facing win here:
          // the chart a script drew is on screen without a click. `reader` and
          // the rest stay folded, same ceiling logic as a turn's cards.
          autoPreview={
            entry !== undefined &&
            previewKind(entry.media_type) === "image" &&
            entry.size_bytes <= AUTO_PREVIEW_WRITTEN_MAX_BYTES
          }
          entry={entry}
          file={{
            name,
            // What the call did, in the vocabulary the card already speaks.
            // A reader-started run is still "运行时写出" -- the verb describes
            // the write, and this write happened during a run.
            action: "run",
            // Neither claim is supportable here: this response says which
            // names landed, not whether they existed before or whether a later
            // turn will rewrite them.
            overwrote: false,
            supersededByTurn: null,
            toolCallId: `run:${sessionId}`,
          }}
          key={name}
          onOpen={onOpen}
          opened={false}
          renderPreview={() => (
            <FilePreview
              // Deliberately without `files`/`onOpen`: a nested viewer must not
              // grow cards of its own. One level is a preview; two is a tree.
              identity={identity}
              {...(onWrote === undefined ? {} : { onWrote })}
              viewing={{
                sessionId,
                name,
                mediaType: entry?.media_type ?? "application/octet-stream",
                sizeBytes: entry?.size_bytes ?? 0,
              }}
            />
          )}
        />
      ))}
    </ul>
  );
}
