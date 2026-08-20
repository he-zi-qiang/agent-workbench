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
