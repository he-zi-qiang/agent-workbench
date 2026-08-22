import type { KeyboardEvent, ReactNode } from "react";

export interface ModeStarter {
  prompt: string;
  title: string;
}

export function submitTextareaOnEnter(
  event: KeyboardEvent<HTMLTextAreaElement>,
) {
  if (
    event.key !== "Enter" ||
    event.shiftKey ||
    event.repeat ||
    event.nativeEvent.isComposing ||
    event.nativeEvent.keyCode === 229
  ) {
    return;
  }

  event.preventDefault();
  event.currentTarget.form?.requestSubmit();
}

export function ModeStartHeader({
  action,
  description,
  title,
}: {
  action?: ReactNode;
  description: string;
  title: string;
}) {
  return (
    <header className="aw-mode-start-head">
      {action}
      <h1>{title}</h1>
      <p>{description}</p>
    </header>
  );
}

export function ModeStarterPrompts({
  disabled = false,
  items,
  label,
  onChoose,
}: {
  disabled?: boolean;
  items: readonly ModeStarter[];
  label: string;
  onChoose: (prompt: string) => void;
}) {
  return (
    <div className="aw-mode-starters" aria-label={label} role="group">
      {items.map((starter) => (
        <button
          aria-label={starter.title}
          disabled={disabled}
          key={starter.title}
          onClick={() => onChoose(starter.prompt)}
          type="button"
        >
          <span>{starter.title}</span>
        </button>
      ))}
    </div>
  );
}
