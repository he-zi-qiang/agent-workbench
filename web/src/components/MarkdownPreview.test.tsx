import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { MarkdownPreview } from "./MarkdownPreview";

/**
 * 这个查看器要钉住的三件事，两件是「它不做什么」。
 *
 * 渲染是默认的——和 `PythonPreview` 相反，和 `HtmlPreview` 一致，理由是代价：
 * 跑一个脚本要服务端起一个容器，画一份 Markdown 要一次绘制。而**被截断的文档
 * 不渲染**，理由和 `HtmlPreview` 一样：半份文档画出来是它从来不是的样子，却被
 * 当成产物摆在读者面前。
 */

const SOURCE = "# 标题\n\n- ship it\n";

function mount(load: () => Promise<{ text: string; truncated: boolean }>) {
  const queries = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queries}>
      <MarkdownPreview load={load} queryKey={["md", "one"]} />
    </QueryClientProvider>,
  );
}

describe("MarkdownPreview", () => {
  it("renders by default, because painting a document costs a paint", async () => {
    mount(() => Promise.resolve({ text: SOURCE, truncated: false }));

    // 标题是 `<h1>`，不是一行 `# 标题`。断言的是元素而不是字符串：一个装着源码
    // 的 `<pre>` 同样含有「标题」这两个字，只有标签能把两者分开。
    const heading = await screen.findByText("标题");
    expect(heading.tagName.toLowerCase()).toBe("h1");
    expect(screen.getByText("ship it").closest("li")).not.toBeNull();
  });

  it("gives the bytes back under 源码, and fetches once for both views", async () => {
    // 源码那一半不是为了对称。这个查看器的主要调用方是一个**编码**控制台，
    // 在那里一份 `.md` 既可能是要读的文档，也可能是刚被写出来、正要被检查的
    // 文件——而渲染视图唯一答不出的问题就是「里面到底写了什么」。
    const user = userEvent.setup();
    const load = vi.fn(() =>
      Promise.resolve({ text: SOURCE, truncated: false }),
    );
    mount(load);
    await screen.findByText("标题");

    await user.click(screen.getByRole("button", { name: "源码" }));
    expect(screen.getByText(/# 标题/)).toBeInTheDocument();
    // 一次传输服务两个视图：切换不该再花一次往返，也不该在两个键下存两份。
    expect(load).toHaveBeenCalledTimes(1);

    await user.click(screen.getByRole("button", { name: "渲染" }));
    expect(screen.getByText("标题").tagName.toLowerCase()).toBe("h1");
    expect(load).toHaveBeenCalledTimes(1);
  });

  it("refuses to render a cut file, and says why the control is off", async () => {
    // 半份文档不是这份文档：一个没闭合的围栏会把后面所有内容吞进代码块，一张
    // 没写完的表格会丢掉剩下的行——而它被当作产物摆在读者面前。
    mount(() =>
      Promise.resolve({ text: "# 标题\n\n```py\nprint(1)", truncated: true }),
    );

    expect(await screen.findByText(/# 标题/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "渲染" })).toBeDisabled();
    // 一个被禁用、又不说为什么的控件读起来像是坏了。
    expect(screen.getByText(/半份文档渲染出来/)).toBeInTheDocument();
  });

  it("offers the file rather than a dead end when the fetch fails", async () => {
    mount(() => Promise.reject(new Error("boom")));

    expect(await screen.findByText("读取文件失败")).toBeInTheDocument();
    // 预览是便利，文件才是交付物。
    expect(screen.getByText(/可以直接下载查看/)).toBeInTheDocument();
  });
});
