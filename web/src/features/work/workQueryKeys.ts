import type { PrincipalIdentity } from "../../api/types";

export function workIdentityQueryKey(
  identity: PrincipalIdentity,
): readonly [string, string, readonly string[]] {
  return [
    identity.tenantId,
    identity.principalId,
    [...identity.scopes].sort(),
  ] as const;
}
