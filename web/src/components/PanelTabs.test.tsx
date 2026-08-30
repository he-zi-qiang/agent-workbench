/**
 * 右栏那排标签页。
 *
 * 钉住的是三件不能走样的事：**消失的格子不能把整栏带空**（标签的数量随任务的进
 * 展在变）、**空的格子不画**、以及**方向键能走**（一排 `role="tab"` 不带键盘契约
 * 是在对读屏用户说谎）。
 */

import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

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
