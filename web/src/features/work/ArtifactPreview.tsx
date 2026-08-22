import { useQuery } from "@tanstack/react-query";
import { useCallback, useState } from "react";
import {
  getArtifactBlob,
  getArtifactText,
  getDocumentPdf,
  getDocumentPreview,
} from "../../api/client";
import type { ArtifactRef, PrincipalIdentity } from "../../api/types";
import { BlobPreview } from "../../components/BlobPreview";
import { HtmlPreview } from "../../components/HtmlPreview";
import { MarkdownContent } from "../../components/MarkdownContent";
import {
  browserShowsPdfInline,
  mediaLabel,
  previewKind,
} from "../../components/media";
import { ErrorNotice, LoadingLine, errorMessage } from "../../components/ui";
import { workIdentityQueryKey } from "./workQueryKeys";
import {
  formatBytes,
  isMarkdown,
  layoutDeclineNote,
  PreviewGaps,
  type PanelLayoutDecline,
} from "./preview";

/**
 * 一份产出的正文，脱离它的框。
 *
 * 这段代码此前长在 `TaskResult` 里，而 `TaskResult` 同时是「这次任务怎么样」
 * 的叙述：运行失败、复核提醒、下载按钮、返回按钮。两件事挤在一个组件里的
 * 结果是它们只能出现在同一个地方——于是从产出文件栏里点开一个文件，就把
 * 整个阅读栏换掉，任务结果本身被顶走。
 *
 * 拆出来之后，同一份正文能同时长在两处：任务自己的产物仍然在阅读栏里（它是
 * 这次任务的答案），而从文件栏点开的文件长在右侧的抽屉里——和编码页是同一个
 * 形状。
 *
 * 只要两个 prop。`artifact` 收窄成非空：以前它可空是因为 `TaskResult` 还要
 * 处理「还没有产物」的那一整套状态，那部分留在原处了。
 */
export function ArtifactPreview({
  artifact,
  identity,
}: {
  artifact: ArtifactRef;
  identity: PrincipalIdentity;
}) {
  const kind = previewKind(artifact.media_type);
  const readable = kind === "text";
  const isDocument = kind === "docx";
  const preview = useQuery({
    // 带上身份。这一族键此前是这个页面上唯一不带身份的，而它们都是
    // `staleTime: Infinity`——QueryClient 是应用级的，切身份不会重建它，
    // 于是同一个 principal 收窄 scope 之后，先前读过的产物内容会直接从
    // 缓存里渲染出来，不再经过一次授权。
    queryKey: [
      "work",
      "artifact-text",
      ...workIdentityQueryKey(identity),
      artifact.artifact_id,
    ],
    enabled: readable,
    staleTime: Number.POSITIVE_INFINITY,
    queryFn: () => getArtifactText(identity, artifact.artifact_id),
  });
  // A separate query rather than a branch inside the one above: this one hits a
  // different endpoint, returns a different shape, and is the only one that can
  // fail because a *stored file* will not parse. Sharing a key would also share
  // a cache entry between two unrelated payloads.
  const document = useQuery({
    queryKey: [
      "work",
      "artifact-document",
      ...workIdentityQueryKey(identity),
      artifact.artifact_id,
    ],
    enabled: isDocument,
    staleTime: Number.POSITIVE_INFINITY,
    queryFn: () => getDocumentPreview(identity, artifact.artifact_id),
  });
  // Which file the reader sent back to text, rather than which one they asked
  // to lay out. The polarity is the point: a Word document opens on 版面 now,
  // because the document is what the Task was asked for, and opening it onto
  // extracted text read as "the task produced plain text" -- the rendered page
  // sat behind a control nothing pointed at. Still an artifact id rather than
  // a boolean: this component stays mounted while the reading column moves
  // from one artifact to the next, and a 文字 chosen for one document must not
  // decide the view for the next one. Narrowed on read, the same way the page
  // decides which artifact is open at all.
  const [textFor, setTextFor] = useState<string | null>(null);
  // Asked before the conversion is, because a browser that will not paint a
  // PDF makes the whole layout half moot: the server would start an external
  // converter, hold a document in memory and send it, for a frame that shows
  // the reader nothing. Declining here costs one property read.
  const viewerShowsPdf = browserShowsPdfInline();
  const wantsLayout =
    isDocument && textFor !== artifact.artifact_id && viewerShowsPdf;
  // The third query on one artifact, and it earns the same answer the second
  // one did: a different endpoint, a different shape, and a failure that means
  // something else again. This is the only one that can come back "this
  // deployment has no converter", which is a fact about the server rather than
  // about the document -- and the reason it resolves rather than throws.
  const layout = useQuery({
    queryKey: [
      "work",
      "artifact-layout",
      ...workIdentityQueryKey(identity),
      artifact.artifact_id,
    ],
    enabled: wantsLayout,
    staleTime: Number.POSITIVE_INFINITY,
    queryFn: () => getDocumentPdf(identity, artifact.artifact_id),
  });
  const layoutBlob = layout.data?.available === true ? layout.data.blob : null;
  // The first object URL on this page that has to outlive the render that made
  // it: a frame keeps reading its source, so the revoke `downloadArtifact` does
  // one line after the click would blank the panel here.
  //
  // Tied to the frame element instead of to a render, through a ref callback
  // and React 19's ref cleanup. The URL is created when the node appears and
  // revoked when it goes -- unmount, a switch back to the text view, a move to
  // another artifact -- which is the leak this has to not be: one URL per
  // preview, held for the life of the tab. Memoized on the blob because an
  // inline callback is a new function every render, and React would detach and
  // re-attach it each time, revoking a source the frame is still displaying.
  const attachLayoutFrame = useCallback(
    (frame: HTMLIFrameElement | null) => {
      if (frame === null || layoutBlob === null) return;
      const url = URL.createObjectURL(layoutBlob);
      frame.src = url;
      return () => {
        URL.revokeObjectURL(url);
      };
    },
    [layoutBlob],
  );
  // A decline is not an error and is deliberately not read off one. A network
  // failure is the only thing that reaches `isError` here, and it lands on the
  // same fallback as every declared refusal: there is no layout, the text
  // preview is unaffected, and the reader is told which of those is true.
  const layoutDeclined: PanelLayoutDecline | null = !viewerShowsPdf
    ? "viewer_unavailable"
    : layout.data?.available === false
      ? layout.data.reason
      : layout.isError
        ? "unavailable"
        : null;
  // What the panel shows, not what was asked for. A declined layout snaps the
  // control to 文字 rather than leaving 版面 lit over text -- the reader would
  // have no way to tell the view named from the one they got. And because 版面
  // is where a document now opens, this snap is also how a deployment without
  // a converter degrades: onto the text view with the note below saying why,
  // never silently.
  const showingLayout = wantsLayout && layoutDeclined === null;

  return (
    <>
      {isDocument ? (
        document.isPending ? (
          <LoadingLine label="正在读取文档内容" />
        ) : document.isError ? (
          <>
            <ErrorNotice
              message={errorMessage(document.error, "无法预览这个文档")}
            />
            {/* The preview is the convenience; the file is the deliverable.
                Saying so keeps a failed extraction from reading as a lost
                document. */}
            <p className="aw-page-note">文件本身没有问题，可以直接下载打开。</p>
          </>
        ) : (
          <>
            {/* The same control the approvals filter uses. Two views of one
                file, so the reader picks rather than scrolls past the wrong
                one; 版面 goes flat once this deployment has said it cannot,
                because a button that has already refused should not keep
                offering. */}
            <div
              className="aw-segmented aw-preview-views"
              aria-label="预览方式"
            >
              <button
                aria-pressed={showingLayout}
                className={showingLayout ? "is-active" : ""}
                disabled={layoutDeclined !== null}
                onClick={() => setTextFor(null)}
                type="button"
              >
                版面
              </button>
              <button
                aria-pressed={!showingLayout}
                className={showingLayout ? "" : "is-active"}
                onClick={() => setTextFor(artifact.artifact_id)}
                type="button"
              >
                文字
              </button>
            </div>
            {/* Above the text it is explaining, and a note rather than an
                `ErrorNotice`: nothing here failed for the reader. The text
                below is intact and the file downloads unchanged, so painting
                this red would report the shape of a deployment as a fault and
                cast doubt on a preview that is fine. */}
            {layoutDeclined === null ? null : (
              <p className="aw-page-note">
                {layoutDeclineNote(layoutDeclined)}
              </p>
            )}
            {showingLayout ? (
              layoutBlob === null ? (
                <LoadingLine label="正在生成版面预览" />
              ) : (
                <>
                  <div className="aw-preview-frame">
                    {/* No `sandbox`. These bytes were typed `application/pdf`
                        by the client before the URL existed, so the frame can
                        only be the browser's own PDF viewer -- and a sandbox
                        strict enough to matter also stops that viewer, which
                        shows an empty panel with nothing saying why. */}
                    <iframe ref={attachLayoutFrame} title="版面预览" />
                  </div>
                  {/* The second sentence is for the frame above having shown
                      nothing. `browserShowsPdfInline` catches only browsers
                      that admit they have no viewer; a Chromium web view
                      reports one, paints its backdrop and raises nothing, so
                      there is no state this component could have entered
                      instead. What is left is to name the thing the reader is
                      looking at -- a flat dark rectangle -- and point at the
                      two ways out, rather than let it read as a broken
                      document. */}
                  <p className="aw-page-note">
                    这是转换出来的版面预览，和 Word
                    打开可能有细微差别；需要原样查看请下载。
                    这里若是一片空白或纯黑，是这个浏览器不显示内嵌
                    PDF——文档没问题，点「文字」看内容，或下载后用 Word 打开。
                  </p>
                </>
              )
            ) : (
              <>
                <MarkdownContent text={document.data.text} />
                {/* The cut used to be a note of its own, right here. It is a
                    row *in* the list now: an empty list is how this page says
                    the preview is faithful, and a note standing beside that
                    emptiness does not stop it being said. */}
                <PreviewGaps preview={document.data} />
                <p className="aw-page-note">
                  这是文档的文字预览，不含排版；需要原样查看请下载。
                </p>
              </>
            )}
          </>
        )
      ) : kind === "image" || kind === "pdf" ? (
        <BlobPreview
          kind={kind}
          load={() => getArtifactBlob(identity, artifact.artifact_id)}
          name={artifact.filename ?? artifact.kind}
          queryKey={[
            "work",
            "artifact-blob",
            ...workIdentityQueryKey(identity),
            artifact.artifact_id,
          ]}
          sizeBytes={artifact.size_bytes}
        />
      ) : kind === "html" ? (
        // Rendered live in HtmlPreview's sandbox frame, not fed to the
        // Markdown path below -- which used to happen and answered a page
        // with its own sanitised remains: no source, no rendering, nothing.
        <HtmlPreview
          load={() => getArtifactText(identity, artifact.artifact_id)}
          name={artifact.filename ?? artifact.kind}
          queryKey={[
            "work",
            "artifact-html",
            ...workIdentityQueryKey(identity),
            artifact.artifact_id,
          ]}
          sizeBytes={artifact.size_bytes}
        />
      ) : !readable ? (
        <p className="aw-page-note">
          {mediaLabel(artifact.media_type)} · {formatBytes(artifact.size_bytes)}
          ，这个类型只能下载后查看。
        </p>
      ) : preview.isPending ? (
        <LoadingLine label="正在读取产出内容" />
      ) : preview.isError ? (
        <ErrorNotice
          message={errorMessage(preview.error, "读取产出内容失败")}
        />
      ) : (
        <>
          {/* Markdown only for Markdown. Every `text` artifact used to go
              through the renderer, and a Task that produced a `.py` had it
              formatted as prose: indentation collapsed, `# 注释` promoted to a
              heading, `*args` eaten as emphasis. The code was still downloadable
              and the page was still calling it the deliverable, which is the
              worst combination -- the reader is looking straight at the thing
              and what they are looking at is wrong.

              A `<pre>` for everything else, matching what the Code console
              shows for the same bytes. That a Task cannot *run* its .py is a
              recorded trade (ADR-065 §4: no working set here); rendering it as
              a document was never a trade, just a default nobody had split. */}
          {previewKind(artifact.media_type) === "text" &&
          isMarkdown(artifact.media_type) ? (
            <MarkdownContent text={preview.data.text} />
          ) : (
            <pre className="aw-code-file-body">{preview.data.text}</pre>
          )}
          {preview.data.truncated ? (
            <p className="aw-page-note">
              内容较长，这里只显示开头；完整内容请下载。
            </p>
          ) : null}
        </>
      )}
    </>
  );
}
