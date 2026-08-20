import { useEffect, useRef, useState, type KeyboardEvent } from "react";
import { AlertTriangle, X } from "lucide-react";
import type { PrincipalIdentity } from "../api/types";
import { IconButton } from "../components/ui";
import { useIdentity } from "./IdentityContext";

export function EnvironmentDialog() {
  const { identity, updateIdentity, editorOpen, setEditorOpen } = useIdentity();
  if (!editorOpen) return null;
  return (
    <EnvironmentDialogContent
      identity={identity}
      key={JSON.stringify(identity)}
      onClose={() => setEditorOpen(false)}
      onSave={(next) => {
        updateIdentity(next);
        setEditorOpen(false);
      }}
    />
  );
}

function EnvironmentDialogContent({
  identity,
  onClose,
  onSave,
}: {
  identity: PrincipalIdentity;
  onClose: () => void;
  onSave: (identity: PrincipalIdentity) => void;
}) {
  const [draft, setDraft] = useState(identity);
  const dialogRef = useRef<HTMLElement>(null);
  const returnFocus = useRef<HTMLElement | null>(
    document.activeElement instanceof HTMLElement ? document.activeElement : null,
  );

  useEffect(() => {
    const focusTarget = returnFocus.current;
    const firstField = dialogRef.current?.querySelector<HTMLElement>("input");
    firstField?.focus();
    return () => {
      window.requestAnimationFrame(() => focusTarget?.focus());
    };
  }, []);

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

  const save = () => {
    onSave({
      tenantId: draft.tenantId.trim(),
      principalId: draft.principalId.trim(),
      scopes: draft.scopes.map((scope) => scope.trim()).filter(Boolean),
    });
  };

  return (
    <div className="aw-dialog-backdrop" role="presentation" onMouseDown={onClose}>
      <section
        aria-labelledby="environment-title"
        aria-modal="true"
        className="aw-dialog"
        onKeyDown={handleDialogKeyDown}
        onMouseDown={(event) => event.stopPropagation()}
        ref={dialogRef}
        role="dialog"
      >
        <header>
          <div>
            <h2 id="environment-title">本地身份模拟器</h2>
            <p>这些 Header 只用于 loopback 开发环境，不是生产登录。</p>
          </div>
          <IconButton label="关闭" onClick={onClose}>
            <X aria-hidden="true" size={17} />
          </IconButton>
        </header>
        <div className="aw-notice is-warning">
          <AlertTriangle aria-hidden="true" size={16} />
          <span>切换身份后，服务端会重新执行对象级授权；本地列表也会按身份隔离。</span>
        </div>
        <IdentityFields draft={draft} setDraft={setDraft} />
        <footer>
          <button className="aw-button is-ghost" onClick={onClose} type="button">
            取消
          </button>
          <button
            className="aw-button is-primary"
            disabled={!draft.tenantId.trim() || !draft.principalId.trim()}
            onClick={save}
            type="button"
          >
            应用身份
          </button>
        </footer>
      </section>
    </div>
  );
}

function IdentityFields({
  draft,
  setDraft,
}: {
  draft: PrincipalIdentity;
  setDraft: (value: PrincipalIdentity) => void;
}) {
  return (
    <div className="aw-form-stack">
      <label>
        <span>Tenant</span>
        <input
          value={draft.tenantId}
          onChange={(event) => setDraft({ ...draft, tenantId: event.target.value })}
        />
      </label>
      <label>
        <span>Principal</span>
        <input
          value={draft.principalId}
          onChange={(event) => setDraft({ ...draft, principalId: event.target.value })}
        />
      </label>
      <label>
        <span>Scopes（逗号分隔）</span>
        <input
          value={draft.scopes.join(", ")}
          onChange={(event) =>
            setDraft({ ...draft, scopes: event.target.value.split(",") })
          }
        />
      </label>
    </div>
  );
}
