import {
  Activity,
  FlaskConical,
  Library,
  MoreHorizontal,
  MessageSquare,
  Settings2,
  ShieldCheck,
  SquareTerminal,
  X,
} from "lucide-react";
import { useState } from "react";
import { NavLink, Outlet, useLocation } from "react-router-dom";
import { EnvironmentDialog } from "./EnvironmentDialog";
import { useIdentity } from "./IdentityContext";

const NAVIGATION = [
  { to: "/chat", label: "Chat", icon: MessageSquare, primary: true },
  { to: "/work", label: "Work", icon: SquareTerminal, primary: true },
  { to: "/knowledge", label: "知识库", icon: Library, primary: false },
  { to: "/approvals", label: "待我确认", icon: ShieldCheck, primary: false },
  { to: "/evaluation", label: "效果评测", icon: FlaskConical, primary: false },
  { to: "/system", label: "运行状态", icon: Activity, primary: false },
] as const;

export function AppShell() {
  const { identity, setEditorOpen } = useIdentity();
  const location = useLocation();
  const [mobileMoreOpen, setMobileMoreOpen] = useState(false);
  const secondaryNavigation = NAVIGATION.filter(
    (item) => item.to === "/approvals" || item.to === "/evaluation" || item.to === "/system",
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
          return (
            <div className={index === 2 ? "aw-nav-divider" : ""} key={item.to}>
              <NavLink className="aw-global-link" title={item.label} to={item.to}>
                <Icon aria-hidden="true" size={18} />
                <span>{item.label}</span>
              </NavLink>
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
            return (
              <NavLink className="aw-mobile-link" key={item.to} to={item.to}>
                <Icon aria-hidden="true" size={19} />
                <span>{item.label}</span>
              </NavLink>
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
