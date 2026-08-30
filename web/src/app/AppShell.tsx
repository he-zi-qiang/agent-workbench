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
import { isPathWithin, NAVIGATION } from "./navigation";
import { QuickSwitcher } from "./QuickSwitcher";
import { THEME_LABEL, useTheme } from "./ThemeContext";
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

export function AppShell() {
  const { identity, editorOpen, setEditorOpen } = useIdentity();
  const { mode: themeMode, cycleMode: cycleTheme } = useTheme();
  const ThemeIcon = THEME_LABEL[themeMode].icon;
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
        <span className="aw-sidebar-section-label">工作空间</span>
        {PRIMARY_NAVIGATION.map((item) => {
          const Icon = item.icon;
          const current = item.covers.some((prefix) =>
            isPathWithin(location.pathname, prefix),
          );
          const collapsed = collapsedWorkspaces[item.to] === true;
          return (
            <div
              className={`aw-sidebar-workspace ${current ? "is-active" : ""} ${
                current && collapsed ? "is-collapsed" : ""
              }`}
              key={item.to}
            >
              {/* 导航项这一行**就是**这一组的标题：名字、折叠开关和这个
                  工作区自己的动作都在它上面。此前名字下面还另起一行
                  「最近对话 / 最近任务 / 最近编码」——同一件事在同一栏里被
                  命名了两次，而第二次占掉一整行高度。 */}
              <div className="aw-workspace-row">
                {current ? (
                  /* 当前这一项已经在它自己的页面上了，所以这一行的工作不再
                     是「去哪里」，而是「这一组展开不展开」——于是它是一个
                     真的 disclosure 按钮，整行可点，不是一个链接旁边再挂
                     一颗只有 20px 的箭头。
                     上一版就是那样：折叠只能点箭头，而那颗箭头在一行 36px
                     的行里是一个 20px 的靶子，还紧挨着两个别的图标。
                     回到这一栏根部（新对话 / 新任务 / 新会话）的路没有丢，
                     它是右边那颗 + —— 那本来就是这一栏里除了「回到哪一段」
                     之外唯一的另一件事。 */
                  <button
                    aria-controls="workspace-sidebar-context"
                    aria-current="page"
                    aria-expanded={!collapsed}
                    className="aw-global-link active"
                    onClick={() => {
                      setCollapsedWorkspaces((held) => ({
                        ...held,
                        [item.to]: !collapsed,
                      }));
                    }}
                    title={`${collapsed ? "展开" : "收起"}${item.label}列表`}
                    type="button"
                  >
                    <span className="aw-global-link-icon">
                      <Icon aria-hidden="true" size={18} />
                    </span>
                    <span className="aw-global-link-copy">{item.label}</span>
                    <ChevronDown
                      aria-hidden="true"
                      className="aw-workspace-fold-icon"
                      size={13}
                    />
                  </button>
                ) : (
                  /* `Link`, not `NavLink`: this entry stands for a set of
                     prefixes, and `NavLink` overwrites `aria-current` with its
                     own single-path match -- which reads "not here" on /work. */
                  <Link
                    aria-label={item.label}
                    className="aw-global-link"
                    title={`${item.label} · ${item.description}`}
                    to={destinationFor(item)}
                  >
                    <span className="aw-global-link-icon">
                      <Icon aria-hidden="true" size={18} />
                    </span>
                    <span className="aw-global-link-copy">{item.label}</span>
                  </Link>
                )}
                {current ? (
                  <>
                    <div
                      className="aw-workspace-actions"
                      ref={setSidebarActionsHost}
                    />
                  </>
                ) : null}
              </div>
              {current ? (
                // 不带 aria-label：`aria-label` 在没有 role 的 <div> 上
                // 是被禁止的，辅助技术会直接丢掉它（在无障碍树里确认过
                // 它不在）。里面每个 feature 自己的 <aside>/<nav> 都带着
                // 真名字，一个读不出来的标签只会让人以为这里有名字。
                <div
                  className="aw-sidebar-context-slot"
                  id="workspace-sidebar-context"
                  ref={setSidebarHost}
                />
              ) : null}
            </div>
          );
        })}
        <span className="aw-sidebar-section-label">资源</span>
        {KNOWLEDGE_NAVIGATION === undefined ? null : (
          <div
            className={`aw-sidebar-workspace aw-sidebar-knowledge ${
              knowledgeCurrent ? "is-active" : ""
            }`}
          >
            <Link
              aria-label={KNOWLEDGE_NAVIGATION.label}
              aria-current={knowledgeCurrent ? "page" : undefined}
              className={`aw-global-link ${knowledgeCurrent ? "active" : ""}`}
              title={`${KNOWLEDGE_NAVIGATION.label} · ${KNOWLEDGE_NAVIGATION.description}`}
              to={KNOWLEDGE_NAVIGATION.to}
            >
              <span className="aw-global-link-icon">
                <KNOWLEDGE_NAVIGATION.icon aria-hidden="true" size={18} />
              </span>
              <span className="aw-global-link-copy">
                {KNOWLEDGE_NAVIGATION.label}
              </span>
            </Link>
            {knowledgeCurrent ? (
              <div
                className="aw-sidebar-context-slot"
                id="workspace-sidebar-context"
                ref={setSidebarHost}
              />
            ) : null}
          </div>
        )}
        {currentPrimaryFlow === undefined && !knowledgeCurrent ? (
          <div className="aw-rail-spacer" />
        ) : null}
        <div className="aw-sidebar-footer-nav">
          <button
            aria-label="更多"
            aria-expanded={mobileMoreOpen}
            aria-haspopup="dialog"
            className={`aw-global-link aw-more-trigger ${secondaryActive ? "active" : ""}`}
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
                    className="aw-mobile-more-link"
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
              {/* 主题也在这里出现一次：rail 在 760px 以下是隐藏的，而它是
                  主题按钮唯一的入口，只放在 rail 上等于移动端没有主题开关。
                  这一项不关闭面板——连点三下看三档，比每点一次都要重新打开
                  「更多」要合理。 */}
              <button
                className="aw-mobile-more-link"
                onClick={cycleTheme}
                type="button"
              >
                <ThemeIcon aria-hidden="true" size={19} />
                <span>主题：{THEME_LABEL[themeMode].text}</span>
              </button>
              <button
                className="aw-mobile-more-link"
                onClick={openSettingsFromMore}
                type="button"
              >
                <Settings2 aria-hidden="true" size={19} />
                <span>设置</span>
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
