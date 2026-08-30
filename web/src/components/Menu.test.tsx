/**
 * 菜单的键盘与焦点契约。
 *
 * 这些断言不是「组件渲染了」，是**它不是一个对话框**：Tab 把人送出去而不是绕圈，
 * Escape 一层一层退，箭头在自己这一层里走而不会掉进子菜单。这三条是这个仓库里
 * 另外四个浮层**故意做相反的事**的地方（`AppShell` 的三个模态、`FolderPicker`），
 * 所以它们值得被钉住——下一个读那些模态代码的人很容易把它们的做法抄过来。
 */

import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { Menu, type MenuEntry } from "./Menu";

function open(entries: MenuEntry[]) {
  return {
    user: userEvent.setup(),
    ...render(
      <div>
        <Menu
          entries={entries}
          label="给这一轮加点什么"
          trigger={<span>+</span>}
          triggerLabel="打开菜单"
        />
        <button type="button">外面那颗</button>
      </div>,
    ),
  };
}

const SIMPLE: MenuEntry[] = [
  { kind: "action", id: "a", label: "第一项", onSelect: vi.fn() },
  { kind: "separator", id: "r" },
  { kind: "action", id: "b", label: "第二项", onSelect: vi.fn() },
];

describe("Menu", () => {
  it("opens on the trigger and puts focus on the first item", async () => {
    const { user } = open(SIMPLE);

    await user.click(screen.getByRole("button", { name: "打开菜单" }));

    expect(screen.getByRole("menu", { name: "给这一轮加点什么" })).toBeInTheDocument();
    // 打开一份清单而不把焦点放进去，等于要求键盘读者再按一次 Tab 才能开始读它，
    // 而在那之前他没有任何提示说清单在哪。
    expect(screen.getByRole("menuitem", { name: "第一项" })).toHaveFocus();
  });

  it("walks its own level with the arrows and wraps at the ends", async () => {
    const { user } = open(SIMPLE);
    await user.click(screen.getByRole("button", { name: "打开菜单" }));

    await user.keyboard("{ArrowDown}");
    expect(screen.getByRole("menuitem", { name: "第二项" })).toHaveFocus();
    // 绕回去而不是停在原地：一份两项的清单，从最后一项按 ↓ 回到第一项是菜单的
    // 通用行为，而停住读起来像按键没生效。分隔线不接焦点。
    await user.keyboard("{ArrowDown}");
    expect(screen.getByRole("menuitem", { name: "第一项" })).toHaveFocus();
    await user.keyboard("{ArrowUp}");
    expect(screen.getByRole("menuitem", { name: "第二项" })).toHaveFocus();
  });

  it("closes on Escape and hands focus back to the trigger", async () => {
    const { user } = open(SIMPLE);
    const trigger = screen.getByRole("button", { name: "打开菜单" });
    await user.click(trigger);

    await user.keyboard("{Escape}");

    expect(screen.queryByRole("menu")).not.toBeInTheDocument();
    // Escape 说的是「我不去别的地方，我要回来」，所以焦点回到它出发的地方。
    expect(trigger).toHaveFocus();
  });

  it("lets Tab leave, because a menu is not a modal", async () => {
    const { user } = open(SIMPLE);
    await user.click(screen.getByRole("button", { name: "打开菜单" }));

    await user.tab();

    // 关掉，但没有把 Tab 拦下来：这一条和 `AppShell` 那三个对话框**故意相反**，
    // 因为那三个是模态而这个不是。焦点也不被抢回触发按钮——读者说的是「去下一个」。
    expect(screen.queryByRole("menu")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "打开菜单" })).not.toHaveFocus();
  });

  it("runs an action once and closes, and leaves a checkbox open", async () => {
    const act = vi.fn();
    const tick = vi.fn();
    const { user } = open([
      { kind: "action", id: "a", label: "做一件事", onSelect: act },
      { kind: "checkbox", id: "c", label: "一个开关", checked: true, onSelect: tick },
    ]);
    await user.click(screen.getByRole("button", { name: "打开菜单" }));

    await user.click(screen.getByRole("menuitemcheckbox", { name: /一个开关/ }));
    // 勾选框留在原地：读者多半要连勾好几个，而每勾一次都要重新打开菜单，是把一次
    // 多选做成 N 次单选。
    expect(tick).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("menu")).toBeInTheDocument();

    await user.click(screen.getByRole("menuitem", { name: "做一件事" }));
    expect(act).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("menu")).not.toBeInTheDocument();
  });

  it("opens a submenu with ArrowRight and steps back out with ArrowLeft", async () => {
    const { user } = open([
      {
        kind: "submenu",
        id: "sub",
        label: "一栏东西",
        entries: [{ kind: "action", id: "x", label: "里面那项", onSelect: vi.fn() }],
      },
    ]);
    await user.click(screen.getByRole("button", { name: "打开菜单" }));

    await user.keyboard("{ArrowRight}");
    expect(screen.getByRole("menuitem", { name: "里面那项" })).toHaveFocus();

    await user.keyboard("{ArrowLeft}");
    expect(screen.queryByRole("menuitem", { name: "里面那项" })).not.toBeInTheDocument();
    expect(screen.getByRole("menuitem", { name: /一栏东西/ })).toHaveFocus();
  });

  it("retreats one level at a time on Escape, not all the way out", async () => {
    const { user } = open([
      {
        kind: "submenu",
        id: "sub",
        label: "一栏东西",
        entries: [{ kind: "action", id: "x", label: "里面那项", onSelect: vi.fn() }],
      },
    ]);
    await user.click(screen.getByRole("button", { name: "打开菜单" }));
    await user.keyboard("{ArrowRight}");

    await user.keyboard("{Escape}");

    // 第一次 Escape 只收起子菜单。收起整个菜单会让「我只是想看看另一栏」的代价
    // 变成重新打开一次。
    expect(screen.getByRole("menu", { name: "给这一轮加点什么" })).toBeInTheDocument();
    expect(screen.queryByRole("menu", { name: "一栏东西" })).not.toBeInTheDocument();

    await user.keyboard("{Escape}");
    expect(screen.queryByRole("menu")).not.toBeInTheDocument();
  });

  it("skips a disabled item when the arrows walk past it", async () => {
    const { user } = open([
      { kind: "action", id: "a", label: "第一项", onSelect: vi.fn() },
      { kind: "action", id: "b", label: "点不动", disabled: true, onSelect: vi.fn() },
      { kind: "action", id: "c", label: "第三项", onSelect: vi.fn() },
    ]);
    await user.click(screen.getByRole("button", { name: "打开菜单" }));

    await user.keyboard("{ArrowDown}");

    // 仍然渲染——一个只在指针下存在的控件是键盘够不着的，而这一项要说的正是
    // 「它在这里，只是现在用不了」——只是不接焦点。
    expect(screen.getByRole("menuitem", { name: "点不动" })).toBeInTheDocument();
    expect(screen.getByRole("menuitem", { name: "第三项" })).toHaveFocus();
  });

  it("renders a note as prose rather than as a broken switch", async () => {
    const { user } = open([
      { kind: "note", id: "n", text: "这一类东西在这里拿不到。" },
      { kind: "action", id: "a", label: "第一项", onSelect: vi.fn() },
    ]);
    await user.click(screen.getByRole("button", { name: "打开菜单" }));

    expect(screen.getByText("这一类东西在这里拿不到。")).toBeInTheDocument();
    // 一句话不是一个用不了的开关。做成 disabled 的菜单项，读者会以为它坏了。
    expect(screen.getAllByRole("menuitem")).toHaveLength(1);
  });

  it("closes when a click lands outside, without stealing focus back", async () => {
    const { user } = open(SIMPLE);
    await user.click(screen.getByRole("button", { name: "打开菜单" }));

    await user.click(screen.getByRole("button", { name: "外面那颗" }));

    expect(screen.queryByRole("menu")).not.toBeInTheDocument();
    // 读者已经点到别的地方去了；把他拽回触发按钮是第二次打断。
    expect(screen.getByRole("button", { name: "外面那颗" })).toHaveFocus();
  });
});
