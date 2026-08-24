import {
  createContext,
  type PropsWithChildren,
  useContext,
  useMemo,
  useState,
} from "react";
import type { PrincipalIdentity } from "../api/types";
import { useStoredState } from "../hooks/useStoredState";

// One scope per tool this console's own pages can cause a Task to reach. The
// authorization envelope and the principal's scopes are two separate gates, so
// a tool inside the envelope is still denied with `missing_permission_scope`
// when the submitter does not hold its scope -- and the submitter here is the
// console, which sends whatever is in this list as `x-principal-scopes`.
//
// The list was two entries long, and the two missing ones were the ones the
// Work page needs most:
//
// * `workspace:write` -- `WorkspaceWriteTool` / `WorkspaceEditTool`
//   (`adapters/tools/workspace.py`). v2's `work` node writes the document into
//   the workspace before the reviewer reads it back, so without this scope the
//   node cannot produce anything. `docs/word-mcp-local.md:163` calls this out
//   in the words the failure deserves: 四个 scope，不是两个.
// * `mcp:*` -- every MCP binding takes `mcp:<alias>`
//   (`adapters/mcp/registry_source.py:124`). The two local profiles use `word`
//   (render_document) and `web` (fetch_page / download_document), and the
//   console cannot know which one the Worker was started with.
//
// Observed before this was fixed, on a real run submitted from this console: a
// Task asked for a Word document, the model was denied
// `missing_permission_scope`, and it fell back to pasting the report into the
// chat while the Task settled as 已完成. A capability the console offers has to
// arrive with the scope that makes it work; the alternative is a console whose
// headline feature fails in a way that reads like the model's fault.
//
// Holding a scope for a tool the deployment never registered costs nothing:
// the envelope is widened from what the Worker discovered (ADR-025), so an
// unused scope authorises no tool that exists. Identity here is self-declared
// and unvalidated by design (ADR-044 §1.1) -- this list is what the console
// asks for, not what anything grants it.
const DEFAULT_IDENTITY: PrincipalIdentity = {
  tenantId: "tenant_local",
  principalId: "user_local",
  scopes: [
    "artifact:export",
    "external:search",
    "workspace:write",
    "mcp:web",
    "mcp:word",
    // `sandbox_run` (ADR-057). Costs nothing where the deployment did not
    // grant the tool -- the envelope is widened from what the process managed
    // to register, so an unused scope authorises nothing that exists.
    "sandbox:run",
    // `project_run` (ADR-077), and its own scope rather than `sandbox:run`
    // precisely so that asking for it is a separate act. It costs nothing
    // where it was not granted, same as the line above:
    // `policy.shell_tools_enabled` is false in every profile but `code-local`,
    // and where it is false this name authorises a tool no turn is offered.
    // Where it is true, what stands between the scope and a command running is
    // still a person reading that command.
    //
    // No straight double quotes in this comment, and that is not style: the
    // scope list is parsed out of this file by
    // `tests/config/test_smoke_walkthrough.py`, which reads every quoted string
    // between the brackets. A quoted phrase in a comment becomes a scope.
    "project:run",
  ],
};

interface IdentityContextValue {
  identity: PrincipalIdentity;
  updateIdentity: (identity: PrincipalIdentity) => void;
  editorOpen: boolean;
  setEditorOpen: (open: boolean) => void;
}

const IdentityContext = createContext<IdentityContextValue | null>(null);

export function IdentityProvider({ children }: PropsWithChildren) {
  // The key carries the version of the default above, and has to: `useStoredState`
  // returns a stored value as it stands rather than merging today's default into
  // it, so every console that had been opened once keeps the scope set from
  // whenever it was first opened and goes on being denied by it -- and the
  // browsers most likely to hit this are the ones already used to try. Merging
  // instead would quietly restore a scope someone deleted on purpose in the
  // identity editor, which is the opposite of what that editor is for.
  //
  // `v1` predates `external:search`; `v2` predates the workspace and MCP
  // scopes above; `v3` predates `sandbox:run`; `v4` predates `project:run`.
  // All left unread rather than migrated: an identity is seven short strings,
  // and re-deriving it costs a reader less than a merge rule that can
  // resurrect a scope they removed.
  const [identity, setIdentity] = useStoredState(
    "aw.identity.v5",
    DEFAULT_IDENTITY,
  );
  const [editorOpen, setEditorOpen] = useState(false);
  const value = useMemo(
    () => ({ identity, updateIdentity: setIdentity, editorOpen, setEditorOpen }),
    [identity, setIdentity, editorOpen],
  );
  return <IdentityContext.Provider value={value}>{children}</IdentityContext.Provider>;
}

export function useIdentity(): IdentityContextValue {
  const value = useContext(IdentityContext);
  if (value === null) throw new Error("useIdentity must be used inside IdentityProvider");
  return value;
}
