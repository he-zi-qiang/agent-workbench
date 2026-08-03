import {
  Activity,
  FlaskConical,
  Library,
  MessageSquare,
  Settings2,
  ShieldCheck,
  SquareTerminal,
} from "lucide-react";
import { NavLink, Outlet } from "react-router-dom";
import { EnvironmentDialog } from "./EnvironmentDialog";
import { useIdentity } from "./IdentityContext";

const NAVIGATION = [
  { to: "/chat", label: "Chat", icon: MessageSquare, primary: true },
  { to: "/work", label: "Work", icon: SquareTerminal, primary: true },
  { to: "/knowledge", label: "知识库", icon: Library, primary: false },
  { to: "/approvals", label: "审批", icon: ShieldCheck, primary: false },
  { to: "/evaluation", label: "评测", icon: FlaskConical, primary: false },
  { to: "/system", label: "系统", icon: Activity, primary: false },
] as const;

export function AppShell() {
  const { identity, setEditorOpen } = useIdentity();
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
        <button className="aw-mobile-link" onClick={() => setEditorOpen(true)} type="button">
          <Settings2 aria-hidden="true" size={19} />
          <span>更多</span>
        </button>
      </nav>
      <span className="aw-sr-only">
        当前本地身份：{identity.tenantId} / {identity.principalId}
      </span>
      <EnvironmentDialog />
    </div>
  );
}
