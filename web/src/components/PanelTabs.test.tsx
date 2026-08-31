/**
 * 右栏那排标签页。
 *
 * 钉住的是三件不能走样的事：**消失的格子不能把整栏带空**（标签的数量随任务的进
 * 展在变）、**空的格子不画**、以及**方向键能走**（一排 `role="tab"` 不带键盘契约
 * 是在对读屏用户说谎）。
 */

import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { PanelTabs, type PanelTabEntry } from "./PanelTabs";

function entries(overrides: Partial<PanelTabEntry>[] = []): PanelTabEntry[] {
  const base: PanelTabEntry[] = [
    { id: "a", label: "进度", body: <p>进度正文</p> },
    { id: "b", label: "子代理", body: <p>子代理正文</p> },
    { id: "c", label: "产物", body: <p>产物正文</p> },
  ];
  return base.map((entry, index) => ({ ...entry, ...overrides[index] }));
}

function mount(props: Partial<Parameters<typeof PanelTabs>[0]> = {}) {
  const onSelect = vi.fn();
  render(
    <PanelTabs
      active="a"
      entries={entries()}
      label="这个任务的几栏"
      onSelect={onSelect}
      {...props}
    />,
  );
  return onSelect;
}

describe("选中项", () => {
  it("只画选中那一格的正文", () => {
    mount();
    expect(screen.getByText("进度正文")).toBeInTheDocument();
    expect(screen.queryByText("子代理正文")).toBeNull();
  });

  it("选中的 id 不在列表里时落回第一格，而不是留下一栏空白", () => {
    // 这一条是这个零件存在的主要风险：格子会消失（最后一个子代理回来之后那一枚
    // 就没了），而调用方存着的 id 在那一刻指向一个不存在的东西。
    mount({ active: "已经没有这一格了" });
    expect(screen.getByText("进度正文")).toBeInTheDocument();
  });

  it("整个列表都空时什么都不画", () => {
    const { container } = render(
      <PanelTabs
        active="a"
        entries={[{ id: "a", label: "进度", available: false, body: <p /> }]}
        label="空的"
        onSelect={vi.fn()}
      />,
    );
    expect(container).toBeEmptyDOMElement();
  });
});

describe("空的格子", () => {
  it("available 为 false 的那一枚整个不画", () => {
    render(
      <PanelTabs
        active="a"
        entries={entries([{}, { available: false }])}
        label="这个任务的几栏"
        onSelect={vi.fn()}
      />,
    );
    expect(screen.queryByRole("tab", { name: /子代理/ })).toBeNull();
    expect(screen.getByRole("tab", { name: /产物/ })).toBeInTheDocument();
  });

  it("选中的那一枚变空之后，落到还在的第一枚上", () => {
    render(
      <PanelTabs
        active="b"
        entries={entries([{}, { available: false }])}
        label="这个任务的几栏"
        onSelect={vi.fn()}
      />,
    );
    expect(screen.getByText("进度正文")).toBeInTheDocument();
  });
});

describe("键盘", () => {
  it("方向键在标签之间走", async () => {
    const user = userEvent.setup();
    const onSelect = mount();

    await user.tab();
    expect(screen.getByRole("tab", { name: /进度/ })).toHaveFocus();

    await user.keyboard("{ArrowRight}");
    expect(onSelect).toHaveBeenCalledWith("b");
  });

  it("按在 trailing 的控件上时不换标签，也不抢焦点", async () => {
    // `trailing` 渲染在这条 strip 里面（预览栏把「收起预览栏」放在那儿），而方向
    // 键的 handler 挂在外层。没有来源判断时，在那颗按钮上按左右键会冒泡上来：
    // 换掉选中项，再把焦点从按钮抢到标签上——读者想在控件之间移动，得到的却是
    // 右栏内容在脚下换了一张。
    const onSelect = vi.fn();
    const onCollapse = vi.fn();
    render(
      <PanelTabs
        active="a"
        entries={entries()}
        label="右栏的几张"
        onSelect={onSelect}
        trailing={
          <button onClick={onCollapse} type="button">
            收起预览栏
          </button>
        }
      />,
    );
    const collapse = screen.getByRole("button", { name: "收起预览栏" });
    // 结构本身：ARIA 只允许 `tab` 做 tablist 的子孙。留在里面的话，读屏会把这颗
    // 按钮当成这排标签的一员念出来，而漫游 tabindex 又不管它。
    expect(screen.getByRole("tablist")).not.toContainElement(collapse);
    collapse.focus();
    await userEvent.keyboard("{ArrowRight}");
    expect(onSelect).not.toHaveBeenCalled();
    expect(collapse).toHaveFocus();
    // 而它本身还是一颗好用的按钮。
    await userEvent.click(collapse);
    expect(onCollapse).toHaveBeenCalledTimes(1);
  });

  it("从最后一枚往右回到第一枚", async () => {
    const user = userEvent.setup();
    const onSelect = mount({ active: "c" });

    await user.tab();
    await user.keyboard("{ArrowRight}");
    expect(onSelect).toHaveBeenCalledWith("a");
  });

  it("只有选中那一枚在 Tab 序列里——一排标签是一站，不是三站", async () => {
    const user = userEvent.setup();
    mount();

    await user.tab();
    expect(screen.getByRole("tab", { name: /进度/ })).toHaveFocus();
    await user.tab();
    // 下一站是内容区，不是第二枚标签。
    expect(screen.getByRole("tabpanel")).toHaveFocus();
  });
});

describe("无障碍的接线", () => {
  it("标签指着自己那块内容，内容也指回标签", () => {
    mount();
    const tab = screen.getByRole("tab", { name: /进度/ });
    const body = screen.getByRole("tabpanel");
    expect(tab).toHaveAttribute("aria-controls", body.id);
    expect(body).toHaveAttribute("aria-labelledby", tab.id);
  });

  it("同一页上两排标签的 id 不撞车", () => {
    render(
      <>
        <PanelTabs
          active="a"
          entries={entries()}
          label="第一排"
          onSelect={vi.fn()}
        />
        <PanelTabs
          active="a"
          entries={entries()}
          label="第二排"
          onSelect={vi.fn()}
        />
      </>,
    );
    const [first, second] = screen.getAllByRole("tabpanel");
    expect(first?.id).not.toEqual(second?.id);
  });
});

describe("放不下的时候", () => {
  /**
   * jsdom 没有排版，`scrollWidth`/`clientWidth`/`scrollLeft` 一律是 0——也就是
   * 「永远不溢出」。这三个量正是这段逻辑唯一读的东西，所以直接换掉它们的 getter，
   * 换出来的是这个零件在真浏览器里会遇到的三种位置。
   */
  function layout(scrollWidth: number, clientWidth: number, scrollLeft: number) {
    vi.spyOn(Element.prototype, "scrollWidth", "get").mockReturnValue(
      scrollWidth,
    );
    vi.spyOn(Element.prototype, "clientWidth", "get").mockReturnValue(
      clientWidth,
    );
    vi.spyOn(Element.prototype, "scrollLeft", "get").mockReturnValue(scrollLeft);
  }

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("放得下的时候什么都不标", () => {
    layout(280, 280, 0);
    mount();
    expect(screen.getByRole("tablist")).not.toHaveAttribute("data-overflow");
  });

  it("右边还有的时候标 end", () => {
    // 实测的那一组数：300px 的右栏里，五枚标签 306px 对 286px 的可见宽度，最后
    // 那一枚的计数徽章被切掉一半——而滚动条是藏起来的，屏幕上没有任何东西说这里
    // 还能往右。
    layout(306, 286, 0);
    mount();
    expect(screen.getByRole("tablist")).toHaveAttribute("data-overflow", "end");
  });

  it("滚到底之后改标 start——该淡的换成左边那一头", () => {
    layout(306, 286, 20);
    mount();
    expect(screen.getByRole("tablist")).toHaveAttribute(
      "data-overflow",
      "start",
    );
  });

  it("两头都还有的时候标 both", () => {
    layout(400, 286, 50);
    mount();
    expect(screen.getByRole("tablist")).toHaveAttribute("data-overflow", "both");
  });

  it("差一个像素不算溢出", () => {
    // 小数宽度和页面缩放会让一条并没有溢出的标签条稳定量出半个像素的差。没有这
    // 条容差，最后一枚标签会常年蒙着一层淡出——那层灰于是不再是「这边还有」，而
    // 是一圈没人看得懂的装饰。
    layout(287, 286, 0);
    mount();
    expect(screen.getByRole("tablist")).not.toHaveAttribute("data-overflow");
  });
});
