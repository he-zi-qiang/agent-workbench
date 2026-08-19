/**
 * The two halves of the workbench, switched by navigating.
 *
 * Links and `aria-current`, not `role="tab"`. The distinction is not pedantry:
 *
 * * ARIA tabs describe panels inside one document. These two have their own
 *   URLs, so adopting the role would promise arrow-key panel switching that
 *   does not exist -- and would put the browser's back button in contradiction
 *   with the widget, since going back would change the "panel" without the
 *   tablist knowing.
 * * Being links is what makes middle-click, copy-link-address, back, forward
 *   and a screen reader's link list all work. A button calling `navigate()`
 *   looks identical and does none of those.
 * * `aria-current="page"` makes "which one am I on" a fact the browser reports
 *   rather than a CSS class somebody has to keep in sync.
 *
 * `Link` plus an explicit prefix check rather than `NavLink`, for the reason
 * the rail has and this file shares: an entry stands for a subtree, and
 * `NavLink` overwrites `aria-current` with its own match against a single `to`.
 *
 * Here the two happen to agree -- the active tab's href is always the current
 * path, because `chatTo`/`workTo` resolve to `pathname` while active -- so this
 * choice buys nothing today on its own. It is made anyway so that the two
 * adjacent navigation surfaces mark "where am I" by one rule instead of two;
 * the rail genuinely needs it (one href, two prefixes), and a tab strip beside
 * it computing currency differently is a difference somebody has to rediscover.
 * An earlier version of this comment claimed a case where `NavLink` got it
 * wrong. The sabotage run for that claim stayed green, which is what proved
 * there was no such case.
 *
 * The visual style borrows `.aw-segmented`'s radius and active pill so the two
 * read as one design system, but it deliberately does not reuse the class:
 * `.aw-segmented` is a radio group over one view's content (a status filter, a
 * preview mode), and that is a control, not navigation.
 */

import { Link, useLocation } from "react-router-dom";

interface WorkbenchTabsProps {
  /** Where 对话 goes -- the last chat path seen, so a session is not lost. */
  chatTo: string;
  /** Where 任务 goes, for the same reason. */
  workTo: string;
}

const TABS = [
  { prefix: "/chat", label: "对话" },
  { prefix: "/work", label: "任务" },
] as const;

export function WorkbenchTabs({ chatTo, workTo }: WorkbenchTabsProps) {
  const { pathname } = useLocation();
  const destinations: Record<string, string> = { "/chat": chatTo, "/work": workTo };
  return (
    <div className="aw-workbench-head">
      {/* 屏名，写出来。
          此前它只存在于 `aria-label` 里：读屏软件听得到「工作台」，看得见的
          人只看到两个没有归属的标签。而这两个标签和左边窄栏上的六个图标是
          两层导航——不说出上一层叫什么，「对话」和「Code」在视觉上就是平级的
          东西，尽管一个是工作台的两半、另一个是另一个工作台。
          `aria-hidden`：nav 已经以同一个词命名，读出来会是「工作台 工作台
          导航」。 */}
      <span aria-hidden="true" className="aw-workbench-name">
        工作台
      </span>
      <nav aria-label="工作台" className="aw-workbench-tabs">
        {TABS.map(({ prefix, label }) => (
          <Link
            aria-current={pathname.startsWith(prefix) ? "page" : undefined}
            className="aw-workbench-tab"
            key={prefix}
            to={destinations[prefix] ?? prefix}
          >
            {label}
          </Link>
        ))}
      </nav>
    </div>
  );
}
