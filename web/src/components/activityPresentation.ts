import type { EventEnvelope } from "../api/types";
import type { StepGroup } from "./stepGroups";

/**
 * The readable parts of one grouped action.
 *
 * This is deliberately a projection over the event stream rather than a new
 * execution model. Chat, Task and Code do not receive the same live events,
 * but a recorded tool call still answers the same three questions everywhere:
 * why did it happen, what was executed, and what came back.
 */
export interface ActivityPresentation {
  toolName: string | null;
  reasoning: string | null;
  command: CommandPresentation | null;
}

export interface CommandPresentation {
  /** A compact line that remains visible while the body is folded. */
  summary: string;
  /** The recorded command or script, never reconstructed from a digest. */
  text: string;
  /** The recorded output, when this deployment opted into step bodies. */
  output: string | null;
}

const COMMAND_TOOLS = new Set([
  "sandbox_run",
  "exec_command",
  "run_command",
  "shell",
  "bash",
]);

const COMMAND_KEYS = ["command", "cmd", "script"] as const;
const SUMMARY_MAX = 112;

export function presentActivity(group: StepGroup): ActivityPresentation {
  const toolName = firstPayloadText(group.events, "tool_name");
  return {
    toolName,
    reasoning: lastPayloadText(group.events, "thinking_preview"),
    command: commandOf(group.events, toolName),
  };
}

function commandOf(
  events: readonly EventEnvelope[],
  toolName: string | null,
): CommandPresentation | null {
  if (toolName === null || !COMMAND_TOOLS.has(toolName)) return null;
  const preview = firstPayloadText(events, "argument_preview");
  if (preview === null) return null;

  let parsed: unknown;
  try {
    parsed = JSON.parse(preview);
  } catch {
    // A bounded argument preview can end in the middle of a large script. It is
    // not safe to guess a command from invalid JSON: the digest is still in the
    // raw event for diagnostics, and this readable projection simply stays out.
    return null;
  }
  if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
    return null;
  }
  const fields = parsed as Record<string, unknown>;
  let text: string | null = null;
  for (const key of COMMAND_KEYS) {
    text = clean(fields[key]);
    if (text !== null) break;
  }
  if (text === null) return null;

  const firstLine = text.split(/\r?\n/, 1)[0]?.replace(/\s+/g, " ").trim() ?? "";
  const summary =
    firstLine.length <= SUMMARY_MAX
      ? firstLine
      : `${firstLine.slice(0, SUMMARY_MAX - 1)}…`;
  return {
    summary,
    text,
    output: lastPayloadText(events, "output_preview"),
  };
}

function firstPayloadText(
  events: readonly EventEnvelope[],
  key: string,
): string | null {
  for (const event of events) {
    const value = clean(event.payload[key]);
    if (value !== null) return value;
  }
  return null;
}

function lastPayloadText(
  events: readonly EventEnvelope[],
  key: string,
): string | null {
  for (let index = events.length - 1; index >= 0; index -= 1) {
    const value = clean(events[index]?.payload[key]);
    if (value !== null) return value;
  }
  return null;
}

function clean(value: unknown): string | null {
  if (typeof value !== "string") return null;
  const trimmed = value.trim();
  return trimmed === "" || trimmed === "—" ? null : trimmed;
}
