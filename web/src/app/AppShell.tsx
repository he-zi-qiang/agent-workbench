import {
  MonitorCog,
  Moon,
  MoreHorizontal,
  PanelLeftClose,
  PanelLeftOpen,
  Search,
  Settings2,
  Sun,
  X,
} from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
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
import { EnvironmentDialog } from "./EnvironmentDialog";
import { useIdentity } from "./IdentityContext";
import { isPathWithin, NAVIGATION } from "./navigation";
import { QuickSwitcher } from "./QuickSwitcher";
import { NEXT_MODE, type ThemeMode, useTheme } from "./ThemeContext";

/**
 * Where the rail's one dividing line goes: before the first entry that is not
 * a primary flow.
 *
 * Derived rather than written as an index. 对话、任务与 Code are primary
 * flows; everything after them is a resource or diagnostic surface. Deriving
 * the boundary from `primary` keeps that grouping true when navigation moves.
 */
const FIRST_SECONDARY_INDEX = NAVIGATION.findIndex((item) => !item.primary);

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
 * 主题按钮显示的是**当前这一档**，不是"点了会变成什么"。
 *
 * 两种写法都常见，但只有一种在三档下不会说谎：显示"下一档"的按钮在 system 档上
 * 必须画一个太阳（下一档是浅色），而此刻屏幕可能正是深的——图标于是和眼前的
 * 界面相反。显示当前档就没有这个问题，代价是要有 title 说清点下去会怎样。
 */
const THEME_LABEL: Record<ThemeMode, { icon: typeof Sun; text: string }> = {
  system: { icon: MonitorCog, text: "跟随系统" },
  light: { icon: Sun, text: "浅色" },
  dark: { icon: Moon, text: "深色" },
};

function ThemeControl() {
  const { mode, resolved, cycleMode } = useTheme();
  const { icon: Icon, text } = THEME_LABEL[mode];
  // `system` 档下补一句当前解析成了什么。这一档的按钮文字说的是"跟随"，而跟随
  // 的结果是屏幕现在的样子——不说出来的话，唯一能回答"现在到底是深还是浅"的
  // 东西就是眼睛，而这正是有人来点这个按钮的原因。
  const current = mode === "system" ? `（当前${resolved === "dark" ? "深色" : "浅色"}）` : "";
  return (
    <button
      aria-label={text}
      className="aw-global-link aw-theme-button"
      onClick={cycleMode}
      title={`主题：${text}${current} · 点击切换到${THEME_LABEL[NEXT_MODE[mode]].text}`}
      type="button"
    >
      <Icon aria-hidden="true" size={18} />
      <span className="aw-global-link-copy">{text}</span>
    </button>
  );
}

export function AppShell() {
  const { identity, setEditorOpen } = useIdentity();
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
    "agent-workbench:rail-collapsed",
    true,
  );
  const [mobileMoreOpen, setMobileMoreOpen] = useState(false);
  const [quickSwitcherOpen, setQuickSwitcherOpen] = useState(false);
  const focusBeforeQuickSwitcher = useRef<HTMLElement | null>(null);
  const openQuickSwitcher = useCallback(() => {
    focusBeforeQuickSwitcher.current =
      document.activeElement instanceof HTMLElement ? document.activeElement : null;
    setQuickSwitcherOpen(true);
  }, []);
  const closeQuickSwitcher = useCallback(() => {
    setQuickSwitcherOpen(false);
    window.requestAnimationFrame(() => focusBeforeQuickSwitcher.current?.focus());
  }, []);
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
        setMobileMoreOpen(false);
        openQuickSwitcher();
      } else if (event.key === "Escape" && mobileMoreOpen) {
        setMobileMoreOpen(false);
      }
    };
    window.addEventListener("keydown", handleGlobalKey);
    return () => window.removeEventListener("keydown", handleGlobalKey);
  }, [mobileMoreOpen, openQuickSwitcher, quickSwitcherOpen]);
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
  const secondaryActive = secondaryNavigation.some((item) =>
    isPathWithin(location.pathname, item.to),
  );
  return (
    <div className={`aw-app-shell ${railCollapsed ? "is-rail-collapsed" : ""}`}>
      <nav className="aw-global-rail" aria-label="主导航">
        <div className="aw-rail-brand-row">
          <NavLink
            aria-label="Agent Workbench"
            className="aw-logo"
            to={primaryHistory["/chat"] ?? "/chat"}
          >
            <span className="aw-logo-mark" aria-hidden="true">A</span>
            <span className="aw-logo-copy">
              <strong>Agent</strong>
              <small>Workbench</small>
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
        <span className="aw-nav-group-label">工作</span>
        {NAVIGATION.map((item, index) => {
          const Icon = item.icon;
          const current = item.covers.some((prefix) =>
            isPathWithin(location.pathname, prefix),
          );
          return (
            <div
              className={index === FIRST_SECONDARY_INDEX ? "aw-nav-divider" : ""}
              key={item.to}
            >
              {index === FIRST_SECONDARY_INDEX ? (
                <span className="aw-nav-group-label">资源与工具</span>
              ) : null}
              {/* `Link`, not `NavLink`: this entry stands for a set of
                  prefixes, and `NavLink` overwrites `aria-current` with its own
                  single-path match -- which reads "not here" on /work. */}
              <Link
                aria-label={item.label}
                aria-current={current ? "page" : undefined}
                className={`aw-global-link ${current ? "active" : ""}`}
                title={`${item.label} · ${item.description}`}
                to={destinationFor(item)}
              >
                <span className="aw-global-link-icon">
                  <Icon aria-hidden="true" size={18} />
                </span>
                <span className="aw-global-link-copy">{item.label}</span>
              </Link>
            </div>
          );
        })}
        <div className="aw-rail-spacer" />
        <button
          aria-label="快速跳转"
          className="aw-global-link aw-quick-switcher-trigger"
          onClick={openQuickSwitcher}
          title="快速跳转 · ⌘K / Ctrl K"
          type="button"
        >
          <span className="aw-global-link-icon">
            <Search aria-hidden="true" size={18} />
          </span>
          <span className="aw-global-link-copy">快速跳转</span>
          <kbd>⌘K</kbd>
        </button>
        <ThemeControl />
        <button
          aria-label="环境"
          className="aw-global-link aw-env-button"
          onClick={() => setEditorOpen(true)}
          title="本地环境"
          type="button"
        >
          <span className="aw-global-link-icon">
            <Settings2 aria-hidden="true" size={18} />
          </span>
          <span className="aw-global-link-copy">环境与权限</span>
        </button>
        <button
          aria-label={`本地身份：${identity.tenantId} / ${identity.principalId}`}
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
            <small>{identity.tenantId} · {identity.scopes.length} scope</small>
          </span>
        </button>
      </nav>
      <section className="aw-app-content">
        {/* Remount every routed surface at an identity boundary so an old
            principal's authorized projection cannot remain on screen. */}
        <Outlet key={identityKey} />
      </section>
      <nav className="aw-mobile-nav" aria-label="移动端导航">
        {NAVIGATION.filter((item) => item.primary || item.to === "/knowledge").map(
          (item) => {
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
          },
        )}
        <button
          aria-expanded={mobileMoreOpen}
          aria-haspopup="dialog"
          className={`aw-mobile-link ${secondaryActive ? "active" : ""}`}
          onClick={() => setMobileMoreOpen(true)}
          type="button"
        >
          <MoreHorizontal aria-hidden="true" size={19} />
          <span>更多</span>
        </button>
      </nav>
      {mobileMoreOpen ? (
        <div
          className="aw-mobile-more-backdrop"
          onClick={() => setMobileMoreOpen(false)}
          role="presentation"
        >
          <section
            aria-label="更多页面"
            aria-modal="true"
            className="aw-mobile-more-sheet"
            onClick={(event) => event.stopPropagation()}
            role="dialog"
          >
            <header>
              <div>
                <span className="aw-eyebrow">更多</span>
                <h2>项目工具</h2>
              </div>
              <button
                aria-label="关闭更多页面"
                className="aw-icon-button"
                onClick={() => setMobileMoreOpen(false)}
                type="button"
              >
                <X aria-hidden="true" size={18} />
              </button>
            </header>
            <nav aria-label="更多项目页面" className="aw-mobile-more-list">
              {secondaryNavigation.map((item) => {
                const Icon = item.icon;
                return (
                  <NavLink
                    aria-label={item.label}
                    className="aw-mobile-more-link"
                    key={item.to}
                    onClick={() => setMobileMoreOpen(false)}
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
                onClick={() => {
                  setMobileMoreOpen(false);
                  openQuickSwitcher();
                }}
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
                onClick={() => {
                  setMobileMoreOpen(false);
                  setEditorOpen(true);
                }}
                type="button"
              >
                <Settings2 aria-hidden="true" size={19} />
                <span>本地环境与身份</span>
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
      <EnvironmentDialog />
    </div>
  );
}
