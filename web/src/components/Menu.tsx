/**
 * 一层菜单，以及它下面的一层。
 *
 * 这个仓库此前没有菜单。浮起来的东西有四种——`QuickSwitcher` 的命令面板、
 * `EnvironmentDialog` 的表单、移动端的「更多」抽屉、`FolderPicker`——**四个都是
 * 对话框**：铺满一层、拿走焦点、只有一条退路。而输入框旁边那颗「+」要的不是对话框，
 * 是一份挨着触发它的按钮长出来的清单：读者按它是为了往当前这句话里加点什么，中途
 * 被拽进一个模态里，回来时输入框已经滚走了。
 *
 * 所以这里是一个真正的 `role="menu"`，而不是又一个 `role="dialog"`：
 *
 * * **不 portal 到 body。** 它长在触发它的按钮旁边（`.aw-menu-anchor` 是
 *   `position: relative`），于是主菜单的位置不需要任何测量代码——没有 popper，也就
 *   没有滚动时跟不上的那一类缺陷。代价是它受祖先的 `overflow` 管；
 *   `.aw-code-composer` 那一条没有裁切，`.aw-code-header` 也没有。
 *
 *   子菜单是这条规矩唯一的例外，而它是量出来的：一份往右开的子菜单在 800px 宽的
 *   窗口里直接超出右边缘（实测，输入框那颗「+」的子菜单被切掉约 120px）。所以它
 *   开之前量一次，放不下就翻到左边。**一次**，在 `useLayoutEffect` 里、在这一帧
 *   画出来之前——不是跟着滚动重算，因为菜单在滚动时是关着的。
 * * **不做焦点陷阱。** 菜单不是模态：Tab 应该把人送出去（并顺手关掉它），而不是
 *   在六个菜单项里绕圈。这一条和 `AppShell` 那三个对话框的做法**故意相反**，因为
 *   那三个是模态而这个不是。
 * * **焦点用 DOM 查询移动，不用下标记账。** 子菜单的项在 DOM 上嵌在父菜单里面，
 *   一份扁平下标要么把两层算成一层（ArrowDown 从最后一项跳进子菜单），要么就得
 *   为每一层各记一份。`closest('[role="menu"]') === 这一层` 一句话就把层分开了，
 *   而且 disabled 的项由同一句过滤掉——它们仍然渲染（一个只在指针下存在的控件是
 *   键盘够不着的），只是不接焦点。
 *
 * `note` 项是这份清单里唯一不可选的东西，而它是刻意留的口子：这个控制台经常需要在
 * 一栏可选项旁边说一句「这一类东西在这里拿不到，以及为什么」（ADR-096 §5 就是这么
 * 一句）。把那句话做成一个 disabled 的菜单项，读者会以为它是个坏掉的开关。
 */

import type { ReactNode } from "react";
import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";

interface MenuEntryBase {
  id: string;
  label: string;
  icon?: ReactNode;
  /** 第二行的小字。名字说不完的时候才给，不是每一项都要有。 */
  hint?: string;
  disabled?: boolean;
}

export interface MenuActionEntry extends MenuEntryBase {
  kind: "action";
  /** 行尾那一小块灰字：快捷键，或者一个数。 */
  trailing?: string;
  /** 危险动作画成危险的样子（删除）。 */
  danger?: boolean;
  onSelect: () => void;
}

export interface MenuCheckboxEntry extends MenuEntryBase {
  kind: "checkbox";
  checked: boolean;
  trailing?: string;
  onSelect: () => void;
}

export interface MenuSubmenuEntry extends MenuEntryBase {
  kind: "submenu";
  trailing?: string;
  entries: readonly MenuEntry[];
}

export interface MenuNoteEntry {
  kind: "note";
  id: string;
  text: string;
}

export interface MenuSeparatorEntry {
  kind: "separator";
  id: string;
}

export type MenuEntry =
  | MenuActionEntry
  | MenuCheckboxEntry
  | MenuSubmenuEntry
  | MenuNoteEntry
  | MenuSeparatorEntry;

const ITEM_SELECTOR = '[role="menuitem"],[role="menuitemcheckbox"]';

/** 这一层里能接焦点的项，不含子菜单里的。 */
function itemsOf(level: Element): HTMLElement[] {
  return Array.from(level.querySelectorAll<HTMLElement>(ITEM_SELECTOR)).filter(
    (item) => item.closest('[role="menu"]') === level && !item.hasAttribute("disabled"),
  );
}

function focusIn(level: Element | null, where: "first" | "last"): void {
  if (level === null) return;
  const items = itemsOf(level);
  const target = where === "first" ? items[0] : items.at(-1);
  target?.focus();
}

/** 从当前这一项挪一格，到头了绕回去。 */
function step(from: HTMLElement, delta: 1 | -1): void {
  const level = from.closest('[role="menu"]');
  if (level === null) return;
  const items = itemsOf(level);
  const at = items.indexOf(from);
  if (at === -1) return;
  // 取模而不是钳位：一份六项的清单，从最后一项按 ↓ 回到第一项是菜单的通用行为，
  // 而停在原地读起来像按键没生效。
  items[(at + delta + items.length) % items.length]?.focus();
}

export function Menu({
  align = "start",
  entries,
  label,
  /** 受控开合。不给就自己管——绝大多数用法不需要外面知道它开着没有。 */
  open: controlledOpen,
  onOpenChange,
  /** 打开时顺带展开哪个子菜单（输入框里打一个 `/` 走的就是这条路）。 */
  openSubmenu = null,
  placement = "top",
  trigger,
  triggerClassName = "",
  triggerLabel,
}: {
  align?: "start" | "end";
  entries: readonly MenuEntry[];
  label: string;
  open?: boolean;
  onOpenChange?: (open: boolean) => void;
  openSubmenu?: string | null;
  placement?: "top" | "bottom";
  trigger: ReactNode;
  triggerClassName?: string;
  triggerLabel: string;
}) {
  const submenuRef = useRef<HTMLDivElement | null>(null);
  const [uncontrolledOpen, setUncontrolledOpen] = useState(false);
  const open = controlledOpen ?? uncontrolledOpen;
  const [submenu, setSubmenu] = useState<string | null>(null);
  const anchorRef = useRef<HTMLDivElement | null>(null);
  const triggerRef = useRef<HTMLButtonElement | null>(null);
  const menuRef = useRef<HTMLDivElement | null>(null);
  //: 下一次子菜单打开时，焦点要不要跟进去。只有 ArrowRight 会置上它。
  const enterSubmenu = useRef(false);

  const setOpen = useCallback(
    (next: boolean) => {
      if (controlledOpen === undefined) setUncontrolledOpen(next);
      onOpenChange?.(next);
    },
    [controlledOpen, onOpenChange],
  );

  // 关掉的时候把焦点还给触发它的按钮。不还的话焦点掉到 <body>，键盘读者在一份
  // 长长的输入框里丢掉了自己的位置——而他刚刚做的动作正是「回到这里」。
  const close = useCallback(
    (restoreFocus = true) => {
      setOpen(false);
      setSubmenu(null);
      if (restoreFocus) triggerRef.current?.focus();
    },
    [setOpen],
  );

  // 跟着 props 调整，在**渲染里**调整，不在 effect 里。
  //
  // 打开这个菜单的两条路给的是不同的初始状态：按「+」是收着的清单，在输入框里打一个
  // `/` 是「快捷指令」那一栏已经展开的清单。用 effect 同步的话，中间会有一帧画的是
  // 收着的那一版——而那一帧正好是焦点被送进去的那一帧。
  //
  // 这是 React 自己推荐的那个写法（"adjusting state when props change"），`AppShell`
  // 的导航记忆也是这么写的：比较上一次的 props，不一样就在渲染中 setState，React 会
  // 立刻重跑这次渲染而不提交那一帧。
  const [seen, setSeen] = useState({ open, openSubmenu });
  if (seen.open !== open || seen.openSubmenu !== openSubmenu) {
    setSeen({ open, openSubmenu });
    setSubmenu(open ? openSubmenu : null);
  }

  // 子菜单放不放得下，在它被画出来之前量一次，然后**直接改那个节点的类**。
  //
  // `useLayoutEffect` 而不是 `useEffect`：后者跑在浏览器画完之后，读者会看到一帧
  // 超出屏幕的子菜单再跳回来。
  //
  // 改 DOM 而不是 setState：这正是 effect 该做的那件事——把量到的外部事实同步回
  // 去——而一次 setState 会多跑一整轮渲染，只为改一个类名。React 不会把它覆盖掉：
  // 这个 div 的 `className` 是一个不变的字符串，React 只在自己那份值变了的时候才
  // 写 DOM；而子菜单一换，节点重建，这个 effect 也跟着重跑。
  useLayoutEffect(() => {
    const node = submenuRef.current;
    if (submenu === null || node === null) return;
    // 8px 的余量，和 `.aw-menu` 的 `max-width` 用的是同一个数：贴着边缘的清单
    // 在有滚动条的窗口里仍然会被压到。
    const box = node.getBoundingClientRect();
    node.classList.toggle("is-flipped", box.right > window.innerWidth - 8);
    // 竖着也会掉出去，而且比横着更容易：工具那一栏有七项加一段说明，而它的锚点
    // 是主菜单里靠下的一行——主菜单本身又是从输入框往上开的。掉出去的部分够不着，
    // 因为可滚的是这个子菜单自己，而它的下沿已经在窗口外面了。
    // 翻过来是把它的**下沿**对齐到那一行，往上长；`max-height` 仍然在，所以真的
    // 上下都放不下时它变成一个可滚的框，而不是继续溢出。
    node.classList.toggle("is-raised", box.bottom > window.innerHeight - 8);
  }, [submenu]);

  // 打开就把焦点送进去。
  //
  // 直接 focus，不套 rAF：effect 跑在 DOM 更新之后，这时 `menuRef.current` 已经
  // 是真的节点了。此前这里套了一层 rAF——多余，而且在 jsdom 里根本不触发，于是
  // 十条断言里有七条量到的是「焦点还在触发按钮上」。
  useEffect(() => {
    if (submenu === null || !enterSubmenu.current) return;
    enterSubmenu.current = false;
    focusIn(menuRef.current?.querySelector(`[data-submenu="${submenu}"]`) ?? null, "first");
  }, [submenu]);

  useEffect(() => {
    if (!open) return;
    const level =
      openSubmenu === null
        ? menuRef.current
        : (menuRef.current?.querySelector(`[data-submenu="${openSubmenu}"]`) ??
          menuRef.current);
    focusIn(level, "first");
  }, [open, openSubmenu]);

  useEffect(() => {
    if (!open) return;
    const onPointerDown = (event: PointerEvent) => {
      const target = event.target;
      if (target instanceof Node && anchorRef.current?.contains(target) === true) {
        return;
      }
      // 点在外面不把焦点抢回按钮上：读者已经点到别的地方去了，把他拽回来是第二次
      // 打断。这和 Escape 不一样——Escape 说的是「我不去别的地方，我要回来」。
      close(false);
    };
    document.addEventListener("pointerdown", onPointerDown);
    return () => document.removeEventListener("pointerdown", onPointerDown);
  }, [close, open]);

  const onKeyDown = (event: React.KeyboardEvent<HTMLDivElement>) => {
    const active = document.activeElement;
    if (!(active instanceof HTMLElement)) return;
    const level = active.closest('[role="menu"]');
    const inSubmenu = level !== null && level !== menuRef.current;

    if (event.key === "Escape") {
      event.preventDefault();
      event.stopPropagation();
      // 一层一层退。Escape 在子菜单里关掉整个菜单，会让「我只是想看看另一栏」
      // 的代价变成重新打开一次。
      if (inSubmenu) {
        const opener = menuRef.current?.querySelector<HTMLElement>(
          `[aria-controls="${level.id}"]`,
        );
        setSubmenu(null);
        opener?.focus();
        return;
      }
      close();
      return;
    }
    if (event.key === "Tab") {
      // 关掉，但**不拦**：Tab 的意思是「去下一个控件」，而菜单不是模态，没有资格
      // 把人留下。三个对话框在这里做的是相反的事，因为它们是模态。
      close(false);
      return;
    }
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      step(active, event.key === "ArrowDown" ? 1 : -1);
      return;
    }
    if (event.key === "Home" || event.key === "End") {
      event.preventDefault();
      focusIn(level, event.key === "Home" ? "first" : "last");
      return;
    }
    if (event.key === "ArrowRight") {
      const owns = active.getAttribute("aria-controls");
      if (owns === null) return;
      event.preventDefault();
      // 焦点不能在这里直接送：子菜单是这次 setState 的**结果**，此刻还没进 DOM。
      // 记一个标记，交给下面那个 effect ——它跑在提交之后，节点已经在了。
      //
      // 用标记而不是「只要 submenu 变了就送焦点」：用指针点开那一栏时，焦点该留在
      // 被点的那一行上（读者的手在鼠标上，抢焦点只会让下一次 Tab 从别处开始）。
      // 只有键盘走进去的时候，焦点才是这次动作的全部内容。
      enterSubmenu.current = true;
      setSubmenu(active.dataset.entry ?? null);
      return;
    }
    if (event.key === "ArrowLeft" && inSubmenu) {
      event.preventDefault();
      const opener = menuRef.current?.querySelector<HTMLElement>(
        `[aria-controls="${level.id}"]`,
      );
      setSubmenu(null);
      opener?.focus();
    }
  };

  const renderEntry = (entry: MenuEntry): ReactNode => {
    if (entry.kind === "separator") {
      return <hr className="aw-menu-rule" key={entry.id} />;
    }
    if (entry.kind === "note") {
      // `role="none"` 而不是一个 disabled 的项：它不是一个用不了的开关，它是一句话。
      return (
        <p className="aw-menu-note" key={entry.id} role="none">
          {entry.text}
        </p>
      );
    }
    if (entry.kind === "submenu") {
      const listId = `aw-menu-${entry.id}`;
      const expanded = submenu === entry.id;
      return (
        <div className="aw-menu-nest" key={entry.id}>
          <button
            aria-controls={listId}
            aria-expanded={expanded}
            aria-haspopup="menu"
            className="aw-menu-item is-parent"
            data-entry={entry.id}
            disabled={entry.disabled}
            onClick={() => setSubmenu(expanded ? null : entry.id)}
            role="menuitem"
            tabIndex={-1}
            type="button"
          >
            {entry.icon === undefined ? null : (
              <span className="aw-menu-icon">{entry.icon}</span>
            )}
            <span className="aw-menu-copy">
              <span>{entry.label}</span>
              {entry.hint === undefined ? null : <small>{entry.hint}</small>}
            </span>
            {entry.trailing === undefined ? null : (
              <span className="aw-menu-trailing">{entry.trailing}</span>
            )}
            <span aria-hidden="true" className="aw-menu-chevron">
              ›
            </span>
          </button>
          {expanded ? (
            <div
              aria-label={entry.label}
              className="aw-menu aw-menu-sub"
              data-submenu={entry.id}
              id={listId}
              ref={submenuRef}
              role="menu"
            >
              {entry.entries.map(renderEntry)}
            </div>
          ) : null}
        </div>
      );
    }
    const checkbox = entry.kind === "checkbox";
    return (
      <button
        aria-checked={checkbox ? entry.checked : undefined}
        className={`aw-menu-item ${
          entry.kind === "action" && entry.danger === true ? "is-danger" : ""
        }`}
        disabled={entry.disabled}
        key={entry.id}
        onClick={() => {
          entry.onSelect();
          // 勾选框留在原地：读者多半要连勾好几个，而每勾一次都要重新打开菜单，
          // 是把一次多选做成 N 次单选。别的项做完就收——它们各自都是一件事。
          if (!checkbox) close(false);
        }}
        role={checkbox ? "menuitemcheckbox" : "menuitem"}
        tabIndex={-1}
        type="button"
      >
        {/* 勾选项**永远**画这个格子，哪怕它是空的：勾是画在这一格的 `::before`
            上的，没有格子就没有勾。而且打了勾和没打勾的两行必须一样宽——用
            「打勾时多插一个元素」的写法，勾上的那一行会整体右移。 */}
        {entry.icon === undefined && !checkbox ? null : (
          <span className="aw-menu-icon">{entry.icon}</span>
        )}
        <span className="aw-menu-copy">
          <span>{entry.label}</span>
          {entry.hint === undefined ? null : <small>{entry.hint}</small>}
        </span>
        {entry.trailing === undefined ? null : (
          <span className="aw-menu-trailing">{entry.trailing}</span>
        )}
      </button>
    );
  };

  return (
    <div className="aw-menu-anchor" ref={anchorRef}>
      <button
        aria-expanded={open}
        aria-haspopup="menu"
        aria-label={triggerLabel}
        className={`aw-menu-trigger ${triggerClassName}`}
        onClick={() => (open ? close() : setOpen(true))}
        ref={triggerRef}
        title={triggerLabel}
        type="button"
      >
        {trigger}
      </button>
      {open ? (
        <div
          aria-label={label}
          className={`aw-menu is-${placement} is-${align}`}
          onKeyDown={onKeyDown}
          ref={menuRef}
          role="menu"
        >
          {entries.map(renderEntry)}
        </div>
      ) : null}
    </div>
  );
}
