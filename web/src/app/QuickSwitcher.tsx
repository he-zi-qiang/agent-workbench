import { ArrowRight, Search, X } from "lucide-react";
import {
  type KeyboardEvent,
  useMemo,
  useRef,
  useState,
} from "react";
import { useNavigate } from "react-router-dom";
import {
  isPathWithin,
  QUICK_DESTINATIONS,
  type QuickDestination,
} from "./navigation";

interface QuickSwitcherProps {
  currentPath: string;
  onClose: () => void;
}

export function QuickSwitcher({ currentPath, onClose }: QuickSwitcherProps) {
  const navigate = useNavigate();
  const dialogRef = useRef<HTMLElement>(null);
  const [query, setQuery] = useState("");
  const [activeIndex, setActiveIndex] = useState(0);
  const destinations = useMemo(() => filterDestinations(query), [query]);

  const choose = (destination: QuickDestination) => {
    void navigate(destination.to);
    onClose();
  };

  const handleSearchKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    if (destinations.length === 0) return;
    if (event.key === "ArrowDown") {
      event.preventDefault();
      setActiveIndex((current) => (current + 1) % destinations.length);
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      setActiveIndex(
        (current) => (current - 1 + destinations.length) % destinations.length,
      );
    } else if (event.key === "Enter") {
      event.preventDefault();
      const destination = destinations[activeIndex] ?? destinations[0];
      if (destination !== undefined) choose(destination);
    }
  };

  const handleDialogKeyDown = (event: KeyboardEvent<HTMLElement>) => {
    if (event.key === "Escape") {
      event.preventDefault();
      onClose();
      return;
    }
    if (event.key !== "Tab" || dialogRef.current === null) return;

    const focusable = Array.from(
      dialogRef.current.querySelectorAll<HTMLElement>(
        'input:not([disabled]), button:not([disabled])',
      ),
    );
    const first = focusable[0];
    const last = focusable.at(-1);
    if (first === undefined || last === undefined) return;
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  };

  return (
    <div
      className="aw-command-backdrop"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
      role="presentation"
    >
      <section
        aria-labelledby="aw-command-title"
        aria-modal="true"
        className="aw-command-dialog"
        onKeyDown={handleDialogKeyDown}
        ref={dialogRef}
        role="dialog"
      >
        <h2 className="aw-sr-only" id="aw-command-title">
          快速跳转
        </h2>
        <div className="aw-command-search">
          <Search aria-hidden="true" size={19} />
          <input
            aria-activedescendant={
              destinations.length > 0 ? `aw-command-${activeIndex}` : undefined
            }
            aria-autocomplete="list"
            aria-controls="aw-command-results"
            aria-expanded="true"
            aria-label="搜索页面"
            autoComplete="off"
            autoFocus
            onChange={(event) => {
              setQuery(event.target.value);
              setActiveIndex(0);
            }}
            onKeyDown={handleSearchKeyDown}
            placeholder="输入页面名称或用途…"
            role="combobox"
            value={query}
          />
          <kbd>Esc</kbd>
          <button
            aria-label="关闭快速跳转"
            className="aw-command-close"
            onClick={onClose}
            type="button"
          >
            <X aria-hidden="true" size={17} />
          </button>
        </div>
        <div className="aw-command-results" id="aw-command-results" role="listbox">
          {destinations.length === 0 ? (
            // Announced: the reader is typing into a box whose whole output is
            // this list, and "the list is now empty" was the one result that
            // arrived in silence.
            <div className="aw-command-empty" role="status">
              <Search aria-hidden="true" size={20} />
              <strong>没有找到这个页面</strong>
              <span>试试“任务”“知识库”或“状态”。</span>
            </div>
          ) : (
            destinations.map((destination, index) => {
              const Icon = destination.icon;
              const current = isPathWithin(currentPath, destination.to);
              return (
                <button
                  aria-selected={index === activeIndex}
                  className={`aw-command-item ${index === activeIndex ? "is-active" : ""}`}
                  id={`aw-command-${index}`}
                  key={destination.to}
                  onClick={() => choose(destination)}
                  onFocus={() => setActiveIndex(index)}
                  onMouseEnter={() => setActiveIndex(index)}
                  role="option"
                  // Out of the tab order. The combobox above owns the
                  // selection through `aria-activedescendant`, so a focusable
                  // option is announced twice -- once as the focused button,
                  // once as the active descendant -- and Tab duplicates what
                  // the arrow keys already do. Verified before this: the cycle
                  // was input → 关闭 → all seven results → input.
                  tabIndex={-1}
                  type="button"
                >
                  <span className="aw-command-icon">
                    <Icon aria-hidden="true" size={18} />
                  </span>
                  <span className="aw-command-copy">
                    <span>
                      <strong>{destination.label}</strong>
                      <small>{destination.group}</small>
                    </span>
                    <span>{destination.description}</span>
                  </span>
                  {current ? (
                    <span className="aw-command-current">当前</span>
                  ) : (
                    <ArrowRight aria-hidden="true" size={16} />
                  )}
                </button>
              );
            })
          )}
        </div>
        <footer className="aw-command-footer">
          <span><kbd>↑</kbd><kbd>↓</kbd> 选择</span>
          <span><kbd>↵</kbd> 前往</span>
          <span><kbd>Esc</kbd> 关闭</span>
        </footer>
      </section>
    </div>
  );
}

function filterDestinations(query: string): QuickDestination[] {
  const terms = query.trim().toLocaleLowerCase().split(/\s+/).filter(Boolean);
  if (terms.length === 0) return [...QUICK_DESTINATIONS];
  return QUICK_DESTINATIONS.filter((destination) => {
    const haystack = [
      destination.label,
      destination.group,
      destination.description,
      destination.keywords,
    ]
      .join(" ")
      .toLocaleLowerCase();
    return terms.every((term) => haystack.includes(term));
  });
}
