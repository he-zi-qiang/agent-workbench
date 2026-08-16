/**
 * Reading and writing this browser's own notes about a principal.
 *
 * Both halves are swallowing on purpose. Storage is a resilience aid, not a
 * source of truth: a privacy mode that refuses it must leave the console fully
 * usable, so a failed read is "nothing remembered" and a failed write is
 * "nothing will be remembered", never an error a surface has to render.
 *
 * The key is scoped by identity because two principals share a browser more
 * often than they share a machine, and a session list that leaked across them
 * would offer one person a link the other opened.
 */

import type { PrincipalIdentity } from "./types";

export function identityStorageKey(identity: PrincipalIdentity): string {
  return encodeURIComponent(
    JSON.stringify([
      identity.tenantId,
      identity.principalId,
      [...identity.scopes].sort(),
    ]),
  );
}

export function readStorage(key: string): string | null {
  try {
    return window.localStorage.getItem(key);
  } catch {
    return null;
  }
}

export function writeStorage(key: string, value: string): void {
  try {
    window.localStorage.setItem(key, value);
  } catch {
    // See above: in-memory state stays fully functional without this.
  }
}

export function removeStorage(key: string): void {
  try {
    window.localStorage.removeItem(key);
  } catch {
    // Same bargain as the two above. A key that could not be removed is a
    // stale row nothing reads any more, not a failure worth surfacing.
  }
}
