/**
 * One page holding the two conversational flows, switched by a tab strip.
 *
 * A **pathless layout route**, which is the whole design. Chat and Work keep
 * their own URLs (`/chat/:sessionId`, `/work/:taskId`), so every `navigate()`
 * inside those two pages, every bookmark, and Knowledge's `?kb=` deep link go
 * on working untouched -- and neither page's internals were opened to do this.
 * A pathless layout contributes no segment, so `useParams` is unaffected too.
 *
 * The one piece of state here is the pair of remembered paths. The layout
 * element does not unmount when the child route changes, so a ref written
 * during a chat session survives a trip to 任务 and back -- which is what makes
 * "come back to where I was" true rather than "come back to a blank composer".
 *
 * What this deliberately does **not** hold is either page's runtime. Switching
 * tabs unmounts the other page and drops its SSE connection, exactly as any
 * navigation does today. Hoisting `useChatRuntime` up here would fix that and
 * would also make this layout own chat state -- rebuilding the component merge
 * that keeping the two pages closed was the point of avoiding.
 */

import { useEffect, useRef } from "react";
import { Outlet, useLocation } from "react-router-dom";
import { WorkbenchTabs } from "../components/WorkbenchTabs";

export function WorkbenchLayout() {
  const { pathname } = useLocation();
  const lastChat = useRef("/chat");
  const lastWork = useRef("/work");

  useEffect(() => {
    if (pathname.startsWith("/chat")) lastChat.current = pathname;
    else if (pathname.startsWith("/work")) lastWork.current = pathname;
  }, [pathname]);

  // Read during render rather than from the effect's copy: on the very first
  // render of a deep link the effect has not run yet, and a tab that pointed at
  // bare `/chat` for one frame would send a reader who clicked quickly to the
  // wrong place.
  const chatTo = pathname.startsWith("/chat") ? pathname : lastChat.current;
  const workTo = pathname.startsWith("/work") ? pathname : lastWork.current;

  return (
    <div className="aw-workbench">
      <WorkbenchTabs chatTo={chatTo} workTo={workTo} />
      <Outlet />
    </div>
  );
}
