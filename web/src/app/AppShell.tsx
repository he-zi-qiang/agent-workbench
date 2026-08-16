import {
  Activity,
  Code2,
  FlaskConical,
  Library,
  MoreHorizontal,
  MessageSquare,
  Settings2,
  X,
} from "lucide-react";
import { useState } from "react";
import { Link, NavLink, Outlet, useLocation } from "react-router-dom";
import { EnvironmentDialog } from "./EnvironmentDialog";
import { useIdentity } from "./IdentityContext";

const NAVIGATION = [
  // One entry for two routes. `covers` is what marks it current on `/work`
  // too -- a `NavLink to="/chat"` is not, and a rail item that goes dark the
  // moment you open the 任务 tab is a rail that disagrees with the page.
  {
    to: "/chat",
    label: "工作台",
    icon: MessageSquare,
    primary: true,
    covers: ["/chat", "/work"],
  },
  { to: "/code", label: "Code", icon: Code2, primary: true, covers: ["/code"] },
  {
    to: "/knowledge",
    label: "知识库",
    icon: Library,
    primary: false,
    covers: ["/knowledge"],
  },
  {
    to: "/evaluation",
    label: "效果评测",
    icon: FlaskConical,
    primary: false,
    covers: ["/evaluation"],
  },
  {
    to: "/system",
    label: "运行状态",
    icon: Activity,
    primary: false,
    covers: ["/system"],
  },
] as const;

export function AppShell() {
  const { identity, setEditorOpen } = useIdentity();
  const location = useLocation();
  const [mobileMoreOpen, setMobileMoreOpen] = useState(false);
  const secondaryNavigation = NAVIGATION.filter(
    (item) => item.to === "/evaluation" || item.to === "/system",
  );
  const secondaryActive = secondaryNavigation.some((item) =>
    location.pathname.startsWith(item.to),
  );
  const identityKey = JSON.stringify([
    identity.tenantId,
    identity.principalId,
    [...identity.scopes].sort(),
  ]);
  return (
    <div className="aw-app-shell">
      <nav className="aw-global-rail" aria-label="主导航">
        <NavLink aria-label="Agent Workbench" className="aw-logo" to="/chat">
          A
        </NavLink>
        {NAVIGATION.map((item, index) => {
          const Icon = item.icon;
          const current = item.covers.some((prefix) =>
            location.pathname.startsWith(prefix),
          );
          return (
            <div className={index === 1 ? "aw-nav-divider" : ""} key={item.to}>
              {/* `Link`, not `NavLink`: this entry stands for a set of
                  prefixes, and `NavLink` overwrites `aria-current` with its own
                  single-path match -- which reads "not here" on /work. */}
              <Link
                aria-current={current ? "page" : undefined}
                className={`aw-global-link ${current ? "active" : ""}`}
                title={item.label}
                to={item.to}
              >
                <Icon aria-hidden="true" size={18} />
                <span>{item.label}</span>
              </Link>
            </div>
          );
        })}
        <div className="aw-rail-spacer" />
        <button
          className="aw-global-link aw-env-button"
          onClick={() => setEditorOpen(true)}
          title="本地环境"
          type="button"
        >
          <Settings2 aria-hidden="true" size={18} />
          <span>环境</span>
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
              location.pathname.startsWith(prefix),
            );
            return (
              <Link
                aria-current={current ? "page" : undefined}
                className={`aw-mobile-link ${current ? "active" : ""}`}
                key={item.to}
                to={item.to}
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
                    className="aw-mobile-more-link"
                    key={item.to}
                    onClick={() => setMobileMoreOpen(false)}
                    to={item.to}
                  >
                    <Icon aria-hidden="true" size={19} />
                    <span>{item.label}</span>
                  </NavLink>
                );
              })}
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
      <EnvironmentDialog />
    </div>
  );
}
