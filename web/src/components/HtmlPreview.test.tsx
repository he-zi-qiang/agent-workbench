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

  it("does not mistake a <header> for the document head", () => {
    // `<head[^>]*>` matches `<header>` too, and a generated fragment opening
    // with one is ordinary. Planted after it, the meta sits in the implicit
    // body -- where a browser discards the policy outright, so the whole page
    // would run ahead of a CSP that never applied.
    const page = withPreviewCsp("<header>标题</header><p>x</p>");

    expect(page.startsWith("<meta http-equiv")).toBe(true);
    expect(page).not.toMatch(/<header><meta/);
  });

  it("does not mistake a <htmlish> element for the root either", () => {
    // The same class of bug one branch down.
    const page = withPreviewCsp("<htmlwidget>x</htmlwidget>");

    expect(page.startsWith("<meta http-equiv")).toBe(true);
  });

  it("still finds a head that carries attributes", () => {
    // The control for both: narrowing the pattern must not lose the real one.
    const page = withPreviewCsp('<html><head profile="x"><title>t</title></head></html>');

    expect(page).toMatch(/<head profile="x"><meta http-equiv/);
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
  it("puts the caution above the frame, not under it", async () => {
    renderPreview();

    const frame = await screen.findByTitle("report.html 预览");
    const note = screen.getByText(/来源不明的页面请谨慎打开/);
    // An HTML artifact paints itself without being asked -- showing it *is*
    // checking it -- so a caution rendered underneath arrives after the thing
    // it cautions about has already loaded. DOCUMENT_POSITION_FOLLOWING means
    // the frame comes after the note.
    expect(
      note.compareDocumentPosition(frame) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
  });

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

describe("HtmlPreview 显示比例", () => {
  it("offers the two scales only while it is rendering", async () => {
    renderPreview();
    expect(await screen.findByRole("button", { name: "适应宽度" })).toBeVisible();
    expect(screen.getByRole("button", { name: "实际大小" })).toBeVisible();

    // The source view is text: a scale control over a `<pre>` would be a
    // control that does nothing, offered next to one that does.
    fireEvent.click(screen.getByRole("button", { name: "源码" }));
    expect(screen.queryByRole("button", { name: "适应宽度" })).toBeNull();
  });

  it("starts on 适应宽度, because a preview shows the page as laid out", async () => {
    renderPreview();
    const fit = await screen.findByRole("button", { name: "适应宽度" });

    expect(fit).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("button", { name: "实际大小" })).toHaveAttribute(
      "aria-pressed",
      "false",
    );
  });

  it("switches to 实际大小 and marks the frame as unscaled", async () => {
    const { container } = renderPreview();
    await screen.findByRole("button", { name: "实际大小" });

    fireEvent.click(screen.getByRole("button", { name: "实际大小" }));

    expect(screen.getByRole("button", { name: "实际大小" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    expect(
      container.querySelector(".aw-preview-frame")?.getAttribute("data-scale"),
    ).toBe("actual");
  });

  it("renders unscaled when the box has no measured size", async () => {
    // jsdom has no layout, so every rect is zero and `useBoxSize` stays null.
    // The fallback matters beyond tests: it is also the first paint in a real
    // browser, before the observer has fired. Dividing by a zero width would
    // give a scale of zero, and a frame scaled to nothing is indistinguishable
    // from a page that failed to load.
    const { container } = renderPreview();
    await screen.findByRole("button", { name: "适应宽度" });

    const frame = container.querySelector(".aw-preview-frame");
    expect(frame?.getAttribute("data-scale")).toBe("actual");
    expect(container.querySelector("iframe")?.getAttribute("style")).toBeNull();
  });
});
