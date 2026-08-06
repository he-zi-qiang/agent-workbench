import {
  createContext,
  type PropsWithChildren,
  useContext,
  useMemo,
  useState,
} from "react";
import type { PrincipalIdentity } from "../api/types";
import { useStoredState } from "../hooks/useStoredState";

// `external:search` is here because the Task graph's research step proposes
// `external_search`, whose spec requires that scope. Without it the tool is
// denied with `missing_permission_scope` even on a deployment that enabled web
// search -- the authorization envelope and the principal's scopes are two
// separate gates, and passing the first one is not passing the second.
const DEFAULT_IDENTITY: PrincipalIdentity = {
  tenantId: "tenant_local",
  principalId: "user_local",
  scopes: ["artifact:export", "external:search"],
};

interface IdentityContextValue {
  identity: PrincipalIdentity;
  updateIdentity: (identity: PrincipalIdentity) => void;
  editorOpen: boolean;
  setEditorOpen: (open: boolean) => void;
}

const IdentityContext = createContext<IdentityContextValue | null>(null);

export function IdentityProvider({ children }: PropsWithChildren) {
  const [identity, setIdentity] = useStoredState(
    "aw.identity.v1",
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
