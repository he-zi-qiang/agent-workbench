import {
  ChevronDown,
  MoreHorizontal,
  PanelLeftClose,
  PanelLeftOpen,
  Search,
  Settings2,
  X,
} from "lucide-react";
import {
  type KeyboardEvent as ReactKeyboardEvent,
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";
import {
  Link,
  NavigationType,
  NavLink,
  Outlet,
  useLocation,
  useNavigate,
  useNavigationType,
} from "react-router-dom";
import { useStoredState } from "../hooks/useStoredState";
import { SettingsDialog } from "./SettingsDialog";
import { useIdentity } from "./IdentityContext";
import { isPathWithin, NAVIGATION, type NavigationItem } from "./navigation";
import { QuickSwitcher } from "./QuickSwitcher";
import type { WorkspaceSidebarContextValue } from "./WorkspaceSidebar";

const PRIMARY_NAVIGATION = NAVIGATION.filter((item) => item.primary);
const KNOWLEDGE_NAVIGATION = NAVIGATION.find(
  (item) => item.to === "/knowledge",
);

const PRIMARY_FLOW_ROOTS = Object.fromEntries(
  NAVIGATION.filter((item) => item.primary).map((item) => [item.to, item.to]),
) as Record<string, string>;

interface PrimaryNavigationMemory {
  activeIdentityKey: string;
  historyByIdentity: Record<string, Record<string, string>>;
  ownerByLocationKey: Record<string, string>;
  pendingIdentityPath: string | null;
  identityBoundaryActive: boolean;
}

/**
 * 主导航里的一行。永远是 `Link`，当前那一项只多一个 `aria-current`。
 *
 * `Link` 而不是 `NavLink`：这一项代表一组前缀（/work 与 /work/:id 都是它），
 * 而 `NavLink` 会用自己那条单路径匹配覆盖 `aria-current`，在 /work/abc 上读成
 * 「不在这里」。
 */
function RailLink({
  current,
  item,
  to,
}: {
  current: boolean;
  item: NavigationItem;
  to: string;
}) {
  const Icon = item.icon;
  return (
    <Link
      aria-current={current ? "page" : undefined}
      aria-label={item.label}
      className={`aw-global-link ${current ? "active" : ""}`}
      title={`${item.label} · ${item.description}`}
      to={to}
    >
      <span className="aw-global-link-icon">
        <Icon aria-hidden="true" size={18} />
      </span>
      <span className="aw-global-link-copy">
        {item.label}
        {item.alias === null ? null : (
          <small aria-hidden="true" className="aw-global-link-alias">
            {item.alias}
          </small>
        )}
      </span>
    </Link>
  );
}

export function AppShell() {
  const { identity, editorOpen, setEditorOpen } = useIdentity();
  const location = useLocation();
  const navigate = useNavigate();
  const navigationType = useNavigationType();
  const identityKey = JSON.stringify([
    identity.tenantId,
    identity.principalId,
    [...identity.scopes].sort(),
  ]);
  const currentPrimaryFlow = NAVIGATION.find(
    (item) =>
      item.primary &&
      item.covers.some((prefix) => isPathWithin(location.pathname, prefix)),
  );
  const currentPrimaryPath = `${location.pathname}${location.search}${location.hash}`;
  const [primaryNavigation, setPrimaryNavigation] =
    useState<PrimaryNavigationMemory>(() => ({
      activeIdentityKey: identityKey,
      historyByIdentity: {},
      ownerByLocationKey: {},
      pendingIdentityPath: null,
      identityBoundaryActive: false,
    }));
  const [railCollapsed, setRailCollapsed] = useStoredState(
    "agent-workbench:workspace-sidebar-collapsed-v2",
    false,
  );
  const [sidebarHost, setSidebarHost] = useState<HTMLDivElement | null>(null);
  const [sidebarActionsHost, setSidebarActionsHost] =
    useState<HTMLDivElement | null>(null);
  // 折叠归壳层管，不归 feature：折叠的是「这一栏在导航里占不占高度」，而那
  // 是导航项自己的事——feature 是 portal 进来的，够不到包着它的两层容器。
  // 一份 map 而不是四个 key：新增一个工作区不该顺带要记得加一行 state。
  const [collapsedWorkspaces, setCollapsedWorkspaces] = useStoredState<
    Record<string, boolean>
  >("aw.sidebar.collapsed.v1", {});
  const [sidebarDrawerLocationKey, setSidebarDrawerLocationKey] = useState<
    string | null
  >(null);
  const sidebarDrawerOpen = sidebarDrawerLocationKey === location.key;
  const sidebarRef = useRef<HTMLElement | null>(null);
  const focusBeforeSidebar = useRef<HTMLElement | null>(null);
  const openSidebar = useCallback(() => {
    focusBeforeSidebar.current =
      document.activeElement instanceof HTMLElement
        ? document.activeElement
        : null;
    setSidebarDrawerLocationKey(location.key);
  }, [location.key]);
  // `isConnected`, not just a null check: these three all close over the
  // element that had focus when the surface opened, and by the time the rAF
  // runs that element may have been unmounted -- the quick switcher navigates
  // *before* it closes, so the control it came from is routinely gone.
  // `focus()` on a detached node is a silent no-op, which drops focus to
  // <body> and loses a keyboard reader's place after every jump. Knowledge's
  // own dialog already guards this way.
  const restoreFocusTo = useCallback((target: HTMLElement | null) => {
    window.requestAnimationFrame(() => {
      if (target?.isConnected) target.focus();
    });
  }, []);
  const closeSidebar = useCallback(() => {
    const returnTarget = focusBeforeSidebar.current;
    setSidebarDrawerLocationKey(null);
    restoreFocusTo(returnTarget);
  }, [restoreFocusTo]);
  const [mobileMoreOpen, setMobileMoreOpen] = useState(false);
  const moreDialogRef = useRef<HTMLElement | null>(null);
  const focusBeforeMore = useRef<HTMLElement | null>(null);
  const restoreMoreFocus = useRef(true);
  const openMore = useCallback(() => {
    focusBeforeMore.current =
      document.activeElement instanceof HTMLElement
        ? document.activeElement
        : null;
    restoreMoreFocus.current = true;
    setMobileMoreOpen(true);
  }, []);
  const closeMore = useCallback((restoreFocus = true) => {
    restoreMoreFocus.current = restoreFocus;
    setMobileMoreOpen(false);
  }, []);
  const [quickSwitcherOpen, setQuickSwitcherOpen] = useState(false);
  const focusBeforeQuickSwitcher = useRef<HTMLElement | null>(null);
  const openQuickSwitcher = useCallback(() => {
    focusBeforeQuickSwitcher.current =
      document.activeElement instanceof HTMLElement
        ? document.activeElement
        : null;
    setQuickSwitcherOpen(true);
  }, []);
  const closeQuickSwitcher = useCallback(() => {
    const returnTarget = focusBeforeQuickSwitcher.current;
    setQuickSwitcherOpen(false);
    restoreFocusTo(returnTarget);
  }, [restoreFocusTo]);
  const openQuickSwitcherFromMore = useCallback(() => {
    restoreMoreFocus.current = false;
    focusBeforeQuickSwitcher.current = focusBeforeMore.current;
    setMobileMoreOpen(false);
    setQuickSwitcherOpen(true);
  }, []);
  const openSettingsFromMore = useCallback(() => {
    // Let SettingsDialog capture a trigger that remains mounted after the
    // More sheet disappears, so closing the nested dialog restores focus to a
    // real control rather than to the removed sheet item.
    focusBeforeMore.current?.focus();
    restoreMoreFocus.current = false;
    setMobileMoreOpen(false);
    setEditorOpen(true);
  }, [setEditorOpen]);
  useEffect(() => {
    if (!mobileMoreOpen) return;
    const returnTarget = focusBeforeMore.current;
    moreDialogRef.current
      ?.querySelector<HTMLElement>(".aw-mobile-more-link")
      ?.focus();
    return () => {
      if (!restoreMoreFocus.current) return;
      restoreFocusTo(returnTarget);
    };
  }, [mobileMoreOpen, restoreFocusTo]);
  useEffect(() => {
    if (!sidebarDrawerOpen) return;
    window.requestAnimationFrame(() => {
      sidebarRef.current
        ?.querySelector<HTMLElement>(
          ".aw-chat-sessions-close, .aw-code-sessions-close, .aw-work-sessions-close, .aw-knowledge-sessions-close",
        )
        ?.focus();
    });
  }, [sidebarDrawerOpen]);
  useEffect(() => {
    const handleGlobalKey = (event: KeyboardEvent) => {
      if (
        event.key.toLocaleLowerCase() === "k" &&
        (event.metaKey || event.ctrlKey) &&
        !event.altKey
      ) {
        // A command palette over a form dialog creates two aria-modal surfaces
        // and can discard an unseen draft when the upper one starts handling
        // Escape. The mobile More sheet is navigation rather than a draft, so
        // it is the one dialog we intentionally replace in place.
        const anotherDialogOpen =
          document.querySelector('[role="dialog"]') !== null &&
          !mobileMoreOpen &&
          !quickSwitcherOpen;
        if (anotherDialogOpen) return;
        event.preventDefault();
        if (mobileMoreOpen) {
          openQuickSwitcherFromMore();
        } else {
          openQuickSwitcher();
        }
      }
    };
    window.addEventListener("keydown", handleGlobalKey);
    return () => window.removeEventListener("keydown", handleGlobalKey);
  }, [
    mobileMoreOpen,
    openQuickSwitcher,
    openQuickSwitcherFromMore,
    quickSwitcherOpen,
  ]);
  // Adjust this tiny navigation snapshot during render so a deep link's first
  // committed frame already points back to itself. Identity changes are a
  // special transition: the URL still belongs to the previous principal, so
  // do not record it. Redirect to the incoming identity's own remembered item
  // (or the flow root) before accepting another path into its history.
  let primaryHistory =
    primaryNavigation.historyByIdentity[identityKey] ?? PRIMARY_FLOW_ROOTS;
  if (primaryNavigation.activeIdentityKey !== identityKey) {
    setPrimaryNavigation({
      ...primaryNavigation,
      activeIdentityKey: identityKey,
      pendingIdentityPath:
        currentPrimaryFlow === undefined
          ? null
          : (primaryHistory[currentPrimaryFlow.to] ?? currentPrimaryFlow.to),
      // Every earlier POP is now suspect until its location key proves it was
      // created under this identity. This remains active across repeated Back
      // operations rather than guarding only the first old history entry.
      identityBoundaryActive: true,
    });
  } else if (primaryNavigation.pendingIdentityPath !== null) {
    if (currentPrimaryPath === primaryNavigation.pendingIdentityPath) {
      setPrimaryNavigation({
        ...primaryNavigation,
        ownerByLocationKey: {
          ...primaryNavigation.ownerByLocationKey,
          [location.key]: identityKey,
        },
        pendingIdentityPath: null,
      });
    }
  } else if (
    currentPrimaryFlow !== undefined &&
    navigationType === NavigationType.Pop &&
    primaryNavigation.identityBoundaryActive &&
    primaryNavigation.ownerByLocationKey[location.key] !== identityKey
  ) {
    setPrimaryNavigation({
      ...primaryNavigation,
      pendingIdentityPath:
        primaryHistory[currentPrimaryFlow.to] ?? currentPrimaryFlow.to,
    });
  } else if (currentPrimaryFlow !== undefined) {
    const pathChanged =
      primaryHistory[currentPrimaryFlow.to] !== currentPrimaryPath;
    if (pathChanged) {
      primaryHistory = {
        ...primaryHistory,
        [currentPrimaryFlow.to]: currentPrimaryPath,
      };
    }
    const ownerChanged =
      primaryNavigation.ownerByLocationKey[location.key] !== identityKey;
    if (pathChanged || ownerChanged) {
      setPrimaryNavigation({
        ...primaryNavigation,
        historyByIdentity: pathChanged
          ? {
              ...primaryNavigation.historyByIdentity,
              [identityKey]: primaryHistory,
            }
          : primaryNavigation.historyByIdentity,
        ownerByLocationKey: ownerChanged
          ? {
              ...primaryNavigation.ownerByLocationKey,
              [location.key]: identityKey,
            }
          : primaryNavigation.ownerByLocationKey,
      });
    }
  }
  useEffect(() => {
    const pendingPath = primaryNavigation.pendingIdentityPath;
    if (pendingPath !== null && currentPrimaryPath !== pendingPath) {
      void navigate(pendingPath, { replace: true });
    }
  }, [currentPrimaryPath, navigate, primaryNavigation.pendingIdentityPath]);
  const destinationFor = (item: (typeof NAVIGATION)[number]) =>
    item.primary
      ? currentPrimaryFlow?.to === item.to
        ? currentPrimaryPath
        : (primaryHistory[item.to] ?? item.to)
      : item.to;
  // Everything that is not a primary flow and not already on the mobile bar.
  // Derived rather than an explicit pair of paths: the hardcoded version left
  // a newly added secondary page reachable on desktop and nowhere on mobile.
  const secondaryNavigation = NAVIGATION.filter(
    (item) => !item.primary && item.to !== "/knowledge",
  );
  const mobileMoreNavigation = NAVIGATION.filter((item) => !item.primary);
  const secondaryActive = secondaryNavigation.some((item) =>
    isPathWithin(location.pathname, item.to),
  );
  const knowledgeCurrent =
    KNOWLEDGE_NAVIGATION?.covers.some((prefix) =>
      isPathWithin(location.pathname, prefix),
    ) ?? false;
  const mobileMoreActive = secondaryActive || knowledgeCurrent;
  const contextSidebarAvailable =
    currentPrimaryFlow !== undefined || knowledgeCurrent;
  // 哪一份列表挂在侧栏里，以及它此刻收没收起。先算成一个小对象再进 JSX：
  // 折叠按钮的 onClick 是个闭包，TypeScript 对 `contextItem` 的收窄不跟进去。
  const contextItem =
    currentPrimaryFlow ?? (knowledgeCurrent ? KNOWLEDGE_NAVIGATION : undefined);
  const records =
    contextItem === undefined || contextItem.records === null
      ? null
      : {
          key: contextItem.to,
          label: contextItem.records,
          collapsed: collapsedWorkspaces[contextItem.to] === true,
        };
  const handleMoreKeyDown = (event: ReactKeyboardEvent<HTMLElement>) => {
    if (event.key === "Escape") {
      event.preventDefault();
      event.stopPropagation();
      closeMore();
      return;
    }
    if (event.key !== "Tab" || moreDialogRef.current === null) return;
    const focusable = Array.from(
      moreDialogRef.current.querySelectorAll<HTMLElement>(
        "a[href], button:not([disabled])",
      ),
    );
    const first = focusable[0];
    const last = focusable.at(-1);
    if (first === undefined || last === undefined) return;
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  };
  useEffect(() => {
    if (!sidebarDrawerOpen) return;
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        closeSidebar();
        return;
      }
      if (event.key !== "Tab" || sidebarRef.current === null) return;
      const focusable = Array.from(
        sidebarRef.current.querySelectorAll<HTMLElement>(
          "a[href], button:not([disabled]), input:not([disabled])",
        ),
      ).filter((element) => element.offsetParent !== null);
      const first = focusable[0];
      const last = focusable.at(-1);
      if (first === undefined || last === undefined) return;
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [closeSidebar, sidebarDrawerOpen]);
  useEffect(() => {
    const desktop = window.matchMedia("(min-width: 761px)");
    const closeDrawerAtDesktop = () => {
      if (!desktop.matches) return;
      // Only when a drawer was actually open. This used to fire on every
      // crossing of 761px, drawer or no drawer, and its only effect on a
      // desktop reader was to take focus out of whatever they were typing in
      // and put it on the nav rail -- a maximise or un-maximise mid-sentence
      // moved the caret. Moving focus is right *after* closing a modal the
      // reader was inside; it is never right on its own.
      setSidebarDrawerLocationKey((current) => {
        if (current === null) return current;
        focusBeforeSidebar.current = null;
        window.requestAnimationFrame(() => {
          sidebarRef.current
            ?.querySelector<HTMLElement>('.aw-global-link[aria-current="page"]')
            ?.focus();
        });
        return null;
      });
    };
    desktop.addEventListener("change", closeDrawerAtDesktop);
    return () => desktop.removeEventListener("change", closeDrawerAtDesktop);
  }, []);
  // Four surfaces on this page are modal, and only one of them was making the
  // rest of the page unreachable. The drawer set `inert` on the content and the
  // mobile nav; the 更多 sheet, the quick switcher and the identity dialog all
  // relied on `aria-modal="true"` plus a Tab wrap bound to their own
  // `onKeyDown` -- which only fires once focus is already inside them. So the
  // page behind stayed hit-testable, findable by the browser's own find, and
  // reachable by a screen reader's virtual cursor.
  //
  // The rail is deliberately absent from the first list: on mobile the drawer
  // *is* the rail, so making it inert while it is open would disable the modal
  // itself.
  const railInert = mobileMoreOpen || quickSwitcherOpen || editorOpen;
  const behindModal = railInert || sidebarDrawerOpen;
  const sidebarContext: WorkspaceSidebarContextValue = {
    managed: true,
    host: sidebarHost,
    actionsHost: sidebarActionsHost,
    drawerOpen: sidebarDrawerOpen,
    open: openSidebar,
    close: closeSidebar,
  };
  return (
    <div
      className={`aw-app-shell ${railCollapsed ? "is-rail-collapsed" : ""} ${
        sidebarDrawerOpen ? "is-sidebar-drawer-open" : ""
      } ${contextSidebarAvailable ? "" : "is-context-free"}`}
    >
      <nav
        aria-hidden={railInert ? "true" : undefined}
        aria-modal={sidebarDrawerOpen ? "true" : undefined}
        className="aw-global-rail"
        aria-label="主导航"
        inert={railInert ? true : undefined}
        ref={sidebarRef}
        role={sidebarDrawerOpen ? "dialog" : undefined}
      >
        <div className="aw-rail-brand-row">
          <NavLink
            aria-label="Agent Workbench"
            className="aw-logo"
            to={primaryHistory["/chat"] ?? "/chat"}
          >
            <span className="aw-logo-mark" aria-hidden="true">
              A
            </span>
            <span className="aw-logo-copy">
              <strong>Agent Workbench</strong>
            </span>
          </NavLink>
          <button
            aria-label={railCollapsed ? "展开导航" : "收起导航"}
            className="aw-rail-collapse"
            onClick={() => setRailCollapsed((current) => !current)}
            title={railCollapsed ? "展开导航" : "收起导航"}
            type="button"
          >
            {railCollapsed ? (
              <PanelLeftOpen aria-hidden="true" size={17} />
            ) : (
              <PanelLeftClose aria-hidden="true" size={17} />
            )}
          </button>
        </div>
        {/* 主导航是一组固定的链接，位置、顺序和点击含义在八个页面上都一样。
            它下面另起一区放当前模式的最近记录（对话 / 任务 / 编码 / 知识库）。

            **这一版推翻了上一版**，上一版的做法是把列表嵌在它所属的那个导航项
            底下，导航项那一行同时充当列表的标题、折叠开关和动作位。省掉的是
            一行标题，付出的是三样东西，2026-09-04 那份评审逐条量出来了：Chat
            激活时它的会话列表把 Tasks 和 Code 推到栏底，切到 Tasks 之后 Code 又
            换了位置，进辅助页整栏还从 272px 收成 188px、品牌名折行——同一个
            去处在不同页面上出现在不同的高度，位置记忆建立不起来；而当前那一项
            未选中时是链接、选中后变成折叠按钮，同一行两种点击含义。

            所以现在：导航永远是链接（当前那一项只是多一个 aria-current），列表
            的名字回到列表自己头上，折叠是一颗看得见的箭头，新建和搜索仍在那一
            行的右端。上一版省掉的那行标题回来了——它就是「稳定的位置」的价格。 */}
        <span className="aw-sidebar-section-label">工作空间</span>
        {PRIMARY_NAVIGATION.map((item) => (
          <RailLink
            current={item.covers.some((prefix) =>
              isPathWithin(location.pathname, prefix),
            )}
            item={item}
            key={item.to}
            to={destinationFor(item)}
          />
        ))}
        <span className="aw-sidebar-section-label">资源</span>
        {KNOWLEDGE_NAVIGATION === undefined ? null : (
          <RailLink
            current={knowledgeCurrent}
            item={KNOWLEDGE_NAVIGATION}
            to={KNOWLEDGE_NAVIGATION.to}
          />
        )}
        {records === null ? (
          <div className="aw-rail-spacer" />
        ) : (
          <div
            className={`aw-sidebar-records ${
              records.collapsed ? "is-collapsed" : ""
            }`}
          >
            <div className="aw-sidebar-records-row">
              {/* 整行是开关，箭头长在行里：靶子是一整行 30px，不是一颗 13px
                  的图标。`aria-label` 把动作说全（「收起最近对话」），可见文字
                  只有名字——名字含在动作里，读屏念的和眼睛看的对得上。 */}
              <button
                aria-controls="workspace-sidebar-context"
                aria-expanded={!records.collapsed}
                aria-label={`${records.collapsed ? "展开" : "收起"}${records.label}`}
                className="aw-records-fold"
                onClick={() => {
                  setCollapsedWorkspaces((held) => ({
                    ...held,
                    [records.key]: !records.collapsed,
                  }));
                }}
                type="button"
              >
                <ChevronDown
                  aria-hidden="true"
                  className="aw-workspace-fold-icon"
                  size={13}
                />
                <span>{records.label}</span>
              </button>
              <div
                className="aw-workspace-actions"
                ref={setSidebarActionsHost}
              />
            </div>
            {/* 不带 aria-label：`aria-label` 在没有 role 的 <div> 上是被禁止的，
                辅助技术会直接丢掉它。里面每个 feature 自己的 <aside>/<nav> 都
                带着真名字。收起时只是 display: none，不卸载——窄屏抽屉打开的
                就是这一块，而抽屉里没有「收起」这件事。 */}
            <div
              className="aw-sidebar-context-slot"
              id="workspace-sidebar-context"
              ref={setSidebarHost}
            />
          </div>
        )}
        <div className="aw-sidebar-footer-nav">
          <button
            aria-label="更多"
            aria-expanded={mobileMoreOpen}
            aria-haspopup="dialog"
            className={`aw-global-link ${secondaryActive ? "active" : ""}`}
            onClick={openMore}
            type="button"
          >
            <span className="aw-global-link-icon">
              <MoreHorizontal aria-hidden="true" size={18} />
            </span>
            <span className="aw-global-link-copy">更多</span>
          </button>
        </div>
        <button
          aria-label={`环境与身份：${identity.tenantId} / ${identity.principalId}`}
          className="aw-rail-identity"
          onClick={() => setEditorOpen(true)}
          title={`点击编辑本地身份\n授权：${identity.scopes.join("、")}`}
          type="button"
        >
          <span className="aw-rail-avatar" aria-hidden="true">
            {identity.principalId.slice(0, 1).toLocaleUpperCase()}
          </span>
          <span className="aw-rail-identity-copy">
            <strong>{identity.principalId}</strong>
          </span>
        </button>
      </nav>
      <section
        aria-hidden={behindModal ? "true" : undefined}
        className="aw-app-content"
        inert={behindModal ? true : undefined}
      >
        {/* Remount every routed surface at an identity boundary so an old
            principal's authorized projection cannot remain on screen. */}
        <Outlet context={sidebarContext} key={identityKey} />
      </section>
      {sidebarDrawerOpen ? (
        <button
          aria-label="关闭侧边栏"
          className="aw-workspace-sidebar-backdrop"
          onClick={closeSidebar}
          type="button"
        />
      ) : null}
      <nav
        aria-hidden={behindModal ? "true" : undefined}
        aria-label="移动端导航"
        className="aw-mobile-nav"
        inert={behindModal ? true : undefined}
      >
        {NAVIGATION.filter((item) => item.primary).map((item) => {
          const Icon = item.icon;
          const current = item.covers.some((prefix) =>
            isPathWithin(location.pathname, prefix),
          );
          return (
            <Link
              aria-current={current ? "page" : undefined}
              className={`aw-mobile-link ${current ? "active" : ""}`}
              key={item.to}
              to={destinationFor(item)}
            >
              <Icon aria-hidden="true" size={19} />
              <span>{item.label}</span>
            </Link>
          );
        })}
        <button
          aria-expanded={mobileMoreOpen}
          aria-haspopup="dialog"
          className={`aw-mobile-link ${mobileMoreActive ? "active" : ""}`}
          onClick={openMore}
          type="button"
        >
          <MoreHorizontal aria-hidden="true" size={19} />
          <span>更多</span>
        </button>
      </nav>
      {mobileMoreOpen ? (
        <div
          className="aw-mobile-more-backdrop"
          onClick={() => closeMore()}
          role="presentation"
        >
          <section
            aria-label="更多页面"
            aria-modal="true"
            className="aw-mobile-more-sheet"
            onClick={(event) => event.stopPropagation()}
            onKeyDown={handleMoreKeyDown}
            ref={moreDialogRef}
            role="dialog"
          >
            <header>
              <h2>更多</h2>
              <button
                aria-label="关闭更多页面"
                className="aw-icon-button"
                onClick={() => closeMore()}
                type="button"
              >
                <X aria-hidden="true" size={18} />
              </button>
            </header>
            <nav aria-label="更多项目页面" className="aw-mobile-more-list">
              {mobileMoreNavigation.map((item) => {
                const Icon = item.icon;
                return (
                  <NavLink
                    aria-label={item.label}
                    // 知识库在桌面端的 rail 上已经有自己的一行（「资源」那一组），
                    // 这个面板在桌面端再列一次就是同一个去处两个入口。只在 rail
                    // 藏起来的窄屏上画它——那时它是唯一的入口。
                    className={`aw-mobile-more-link${
                      item.to === "/knowledge" ? " is-mobile-only" : ""
                    }`}
                    key={item.to}
                    onClick={() => closeMore()}
                    to={item.to}
                  >
                    <Icon aria-hidden="true" size={19} />
                    <span className="aw-mobile-more-copy">
                      <strong>{item.label}</strong>
                      <small>{item.description}</small>
                    </span>
                  </NavLink>
                );
              })}
              <button
                aria-label="快速跳转"
                className="aw-mobile-more-link"
                onClick={openQuickSwitcherFromMore}
                type="button"
              >
                <Search aria-hidden="true" size={19} />
                <span className="aw-mobile-more-copy">
                  <strong>快速跳转</strong>
                  <small>按名称或用途查找所有页面</small>
                </span>
              </button>
              {/* 主题不再在这里单列一行。它此前是 rail 上那颗循环三档的按钮
                  搬进来的，而设置面板的「外观」已经把三档并排画出来了——同一
                  个开关两个入口、两种形状，用户读成的是重复。「设置」这一行
                  留着，它是窄屏上唯一能到设置面板的路。 */}
              <button
                // 名字只有两个字。下面那行小字是说明，不是名字——和上面每一
                // 条 NavLink 的 `aria-label={item.label}` 同一条规矩。
                aria-label="设置"
                className="aw-mobile-more-link"
                onClick={openSettingsFromMore}
                type="button"
              >
                <Settings2 aria-hidden="true" size={19} />
                <span className="aw-mobile-more-copy">
                  <strong>设置</strong>
                  <small>本地身份、模型密钥、外观</small>
                </span>
              </button>
            </nav>
          </section>
        </div>
      ) : null}
      <span className="aw-sr-only">
        当前本地身份：{identity.tenantId} / {identity.principalId}
      </span>
      {quickSwitcherOpen ? (
        <QuickSwitcher
          currentPath={location.pathname}
          onClose={closeQuickSwitcher}
        />
      ) : null}
      <SettingsDialog />
    </div>
  );
}
