import { ChevronRight, TerminalSquare } from "lucide-react";
import { useState } from "react";
import type { CommandPresentation } from "./activityPresentation";

/** Recorded command text and output, one level below the readable action row. */
export function CommandTrace({
  command,
  running,
}: {
  command: CommandPresentation;
  running: boolean;
}) {
  // A command that was already moving when it mounted starts open. Once the
  // reader changes it, or once the call settles, the disclosure keeps that
  // local choice instead of being reset by every stream frame.
  const [open, setOpen] = useState(running);
  return (
    <details
      className={`aw-command-trace ${running ? "is-running" : ""}`}
      onToggle={(event) => setOpen(event.currentTarget.open)}
      open={open}
    >
      <summary>
        <ChevronRight aria-hidden="true" className="aw-step-caret" size={13} />
        <TerminalSquare aria-hidden="true" size={14} />
        <code>
          <span aria-hidden="true">$</span> {command.summary}
        </code>
        <small>{running ? "执行中" : command.output === null ? "查看命令" : "命令与输出"}</small>
      </summary>
      <div className="aw-command-trace-body">
        <figure>
          <figcaption>命令</figcaption>
          <pre>{command.text}</pre>
        </figure>
        {command.output === null ? null : (
          <figure>
            <figcaption>输出</figcaption>
            <pre>{command.output}</pre>
          </figure>
        )}
      </div>
    </details>
  );
}
