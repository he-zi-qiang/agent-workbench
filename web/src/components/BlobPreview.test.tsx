import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { BlobPreview } from "./BlobPreview";

/**
 * The mirror of `HtmlPreview.test.tsx`'s sandbox assertion, and the reason
 * both exist: two frames in this app answer the sandbox question in opposite
 * directions, and each answer is load-bearing.
 *
 * Here the attribute must be **absent**. These bytes were typed
 * `application/pdf` by the client before the URL existed, so the frame can
 * only be the browser's own PDF viewer -- and a sandbox strict enough to
 * matter also stops that viewer, silently, leaving an empty panel with
 * nothing saying why. Adding one here is a change that no other test would
 * catch: the page still renders, the frame is still there, and only a human
 * opening a .docx layout would find out.
 */
function renderPdf() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  // jsdom has no PDF viewer property; the component reads
  // `navigator.pdfViewerEnabled !== false`, so an absent one means "try".
  return render(
    <QueryClientProvider client={client}>
      <BlobPreview
        kind="pdf"
        load={() => Promise.resolve(new Blob(["%PDF-1.4"], { type: "application/pdf" }))}
        name="report.pdf"
        queryKey={["test-pdf", "report.pdf"]}
        sizeBytes={64}
      />
    </QueryClientProvider>,
  );
}

describe("BlobPreview", () => {
  it("leaves the PDF frame unsandboxed, which the browser viewer requires", async () => {
    renderPdf();

    const frame = await screen.findByTitle("report.pdf 预览");
    expect(frame.hasAttribute("sandbox")).toBe(false);
  });

  it("declines an oversized image before fetching anything", () => {
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    let fetched = false;
    render(
      <QueryClientProvider client={client}>
        <BlobPreview
          kind="image"
          load={() => {
            fetched = true;
            return Promise.resolve(new Blob([""], { type: "image/png" }));
          }}
          name="huge.png"
          queryKey={["test-image", "huge.png"]}
          sizeBytes={21 * 1024 * 1024}
        />
      </QueryClientProvider>,
    );

    expect(
      screen.getByText("这个文件太大，页面里不展开；请下载后查看。"),
    ).toBeInTheDocument();
    // The size is judged from the listing's own count, so a refusal costs no
    // transfer at all.
    expect(fetched).toBe(false);
  });

  it("carries no download control of its own", async () => {
    renderPdf();

    await screen.findByTitle("report.pdf 预览");
    expect(screen.queryByRole("button", { name: "下载" })).not.toBeInTheDocument();
  });
});
