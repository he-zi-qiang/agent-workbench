import { useEffect } from "react";

/**
 * Escape 关掉一层浮起来的东西。
 *
 * 抽屉此前只有两个关法：点背景，或者点它自己的「关闭」。两个都要用鼠标，而
 * 一个盖住半屏内容的东西不该只有鼠标能收起来——键盘用户唯一的办法是 Tab 到
 * 那颗按钮上，而它在抽屉的另一头。
 *
 * `keydown` 挂在 document 上而不是抽屉上：焦点这时可能在抽屉里的任何一个
 * 控件上，也可能因为刚点过背景而落在 body 上。挂在容器上就要先假设焦点在
 * 容器内，而那正是最需要这条快捷键的时候最不成立的假设。
 *
 * 只在 `open` 为真时挂：一个常驻的全局 keydown 会在每一次按键上跑一次，也会
 * 让「谁在处理 Escape」这件事变成一个要按注册顺序推理的问题。
 */
export function useDismissOnEscape(open: boolean, dismiss: () => void): void {
  useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      event.stopPropagation();
      dismiss();
    };
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("keydown", onKey);
    };
  }, [open, dismiss]);
}
