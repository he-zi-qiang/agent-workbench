import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { HtmlPreview, withPreviewCsp } from "./HtmlPreview";

function renderPreview(overrides?: {
  sizeBytes?: number;
  body?: { text: string; truncated: boolean };
}) {
  const body = overrides?.body ?? {
    text: "<html><head></head><body><h1>你好</h1></body></html>",
    truncated: false,
  };
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <HtmlPreview
        load={() => Promise.resolve(body)}
        name="report.html"
        queryKey={["test-html", "report.html"]}
        sizeBytes={overrides?.sizeBytes ?? body.text.length}
      />
    </QueryClientProvider>,
  );
}

describe("withPreviewCsp", () => {
  it("injects the policy right after an explicit head", () => {
    const page = withPreviewCsp("<html><head><title>t</title></head></html>");
    expect(page).toMatch(/<head><meta http-equiv="Content-Security-Policy"/);
  });

  it("injects into the implicit head of a headless html element", () => {
    const page = withPreviewCsp('<html lang="zh"><body>x</body></html>');
    expect(page).toMatch(/<html lang="zh"><meta http-equiv/);
  });

  it("keeps a doctype first, because a meta ahead of it means quirks mode", () => {
    const page = withPreviewCsp("<!doctype html><div>x</div>");
    expect(page.startsWith("<!doctype html><meta http-equiv")).toBe(true);
  });

  it("prefixes a bare fragment", () => {
    expect(withPreviewCsp("<div>x</div>").startsWith("<meta http-equiv")).toBe(
      true,
    );
  });

  it("names every closed channel", () => {
    // The policy is the security statement; a test spelling it out is what
    // makes loosening it a visible decision rather than a drive-by edit.
    const page = withPreviewCsp("<div />");
    for (const clause of [
      "default-src 'none'",
      "connect-src 'none'",
      "form-action 'none'",
      "base-uri 'none'",
    ]) {
      expect(page).toContain(clause);
    }
  });
});

describe("HtmlPreview", () => {
  it("runs the page in a frame whose sandbox has no allow-same-origin", async () => {
    renderPreview();
    const frame = await screen.findByTitle("report.html 预览");
    // The whole design hangs on this attribute: scripts may run, but in an
    // opaque origin. `allow-same-origin` appearing here would hand the page
    // the console's own origin -- pinned the way BlobPreview pins that its
    // PDF frame has no sandbox at all.
    expect(frame.getAttribute("sandbox")).toBe("allow-scripts");
    expect(frame.getAttribute("srcdoc")).toContain("Content-Security-Policy");
  });

  it("flips to the source view and back without a second fetch", async () => {
    renderPreview();
    await screen.findByTitle("report.html 预览");
    fireEvent.click(screen.getByRole("button", { name: "源码" }));
    expect(screen.getByText(/<h1>你好<\/h1>/)).toBeInTheDocument();
    expect(screen.queryByTitle("report.html 预览")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "渲染" }));
    expect(screen.getByTitle("report.html 预览")).toBeInTheDocument();
  });

  it("declines an oversized file before fetching anything", () => {
    renderPreview({ sizeBytes: 600 * 1024 });
    expect(
      screen.getByText("这个文件太大，页面里不展开；请下载后查看。"),
    ).toBeInTheDocument();
  });

  it("refuses to render a truncated body and shows the source instead", async () => {
    // Half a page runs half its scripts and paints something that never
    // existed; the honest view of a cut body is the cut source.
    renderPreview({ body: { text: "<html><body>部分", truncated: true } });
    expect(
      await screen.findByText("只显示了开头一部分，完整内容请下载。"),
    ).toBeInTheDocument();
    expect(screen.queryByTitle("report.html 预览")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "渲染" })).toBeDisabled();
  });

  it("carries no download control of its own", async () => {
    renderPreview();
    await screen.findByTitle("report.html 预览");
    expect(screen.queryByRole("button", { name: "下载" })).not.toBeInTheDocument();
  });
});
