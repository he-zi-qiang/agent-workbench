import type { PropsWithChildren } from "react";
import { createPortal } from "react-dom";
import { useOutletContext } from "react-router-dom";

/**
 * The shell owns the one desktop sidebar, while each feature still owns the
 * data and interactions rendered inside it. Keeping that seam as an Outlet
 * context avoids lifting three unrelated query models into AppShell.
 */
export interface WorkspaceSidebarContextValue {
  managed: true;
  host: HTMLElement | null;
  /**
   * 第二个宿主：工作区那一行右端的动作位（新建、搜索、刷新）。
   *
   * 分开而不是让 feature 在列表顶上自己画一行：导航项本身就是这一组的标题，
   * 而「新建一段对话」属于那个标题，不属于它底下的列表。此前它们中间还夹着
   * 一行「最近对话」——同一件事在同一栏里被命名了两次。
   */
  actionsHost: HTMLElement | null;
  drawerOpen: boolean;
  open: () => void;
  close: () => void;
}

function useOptionalWorkspaceSidebar(): WorkspaceSidebarContextValue | null {
  return useOutletContext<WorkspaceSidebarContextValue | null>();
}

/**
 * Move a feature-owned list into the shell's sidebar. Feature unit tests render
 * pages without AppShell, so the children stay inline when no managed shell is
 * present; the real app suppresses the first inline frame until the host mounts.
 */
export function WorkspaceSidebarPortal({ children }: PropsWithChildren) {
  const sidebar = useOptionalWorkspaceSidebar();
  if (sidebar?.managed !== true) return children;
  if (sidebar.host === null) return null;
  return createPortal(children, sidebar.host);
}

/**
 * Move a feature's own controls into the workspace row that names it.
 *
 * Same escape hatch as the list portal: feature unit tests render pages
 * without AppShell, so the children stay inline when no managed shell is
 * present.
 */
export function WorkspaceSidebarActions({ children }: PropsWithChildren) {
  const sidebar = useOptionalWorkspaceSidebar();
  if (sidebar?.managed !== true) return children;
  if (sidebar.actionsHost === null) return null;
  return createPortal(children, sidebar.actionsHost);
}

export function useWorkspaceSidebar(): Pick<
  WorkspaceSidebarContextValue,
  "drawerOpen" | "open" | "close"
> {
  const sidebar = useOptionalWorkspaceSidebar();
  return sidebar?.managed === true
    ? sidebar
    : {
        drawerOpen: false,
        open: () => undefined,
        close: () => undefined,
      };
}
