/**
 * Which coding sessions this browser has opened.
 *
 * In the browser and not on the server, because the server has no way to
 * answer it: nothing in `ConversationStore` lists a principal's sessions, and
 * chat has the same hole and fills it the same way. Adding the query is a
 * bigger change than this surface needs -- and one that would have to decide
 * what a session is *called*, which nothing records today.
 *
 * What it costs is honest and worth saying: clear the browser's storage and
 * the sessions are still there, still owned, and no longer reachable. Only the
 * link is lost, not the work. F-06 in docs/known-gaps.md is where that stays
 * written down rather than being rediscovered.
 */

import { readStorage, writeStorage, identityStorageKey } from "../../api/localStore";
import type { PrincipalIdentity } from "../../api/types";

const SESSION_PREFIX = "aw.code.sessions.v1";

/** How many to keep. Enough to find last week's, few enough to read. */
const KEPT_SESSIONS = 20;

export interface LocalCodeSession {
  sessionId: string;
  /**
   * When this browser last had the session open -- not when it was created.
   *
   * The distinction is the ordering: a list sorted by creation puts a session
   * you have not touched in a month above the one you were in five minutes
   * ago, which is the wrong way round for a list whose whole job is getting
   * you back to where you were.
   */
  seenAt: string;
}

export function loadCodeSessions(identity: PrincipalIdentity): LocalCodeSession[] {
  const raw = readStorage(sessionKey(identity));
  if (raw === null) return [];
  try {
    const value: unknown = JSON.parse(raw);
    if (!Array.isArray(value)) return [];
    return value.flatMap((item) => {
      const session = parseSession(item);
      return session === null ? [] : [session];
    });
  } catch {
    // Unparseable is treated as absent rather than repaired. Whatever wrote it
    // was a different version of this file, and guessing at half of it would
    // produce links this one cannot open.
    return [];
  }
}

/**
 * Put a session at the front, without duplicating one already there.
 *
 * Returns the new list rather than only writing it, so a caller can render
 * what it just stored without a second read -- the read would race the write
 * on a storage that silently refused it.
 */
export function rememberCodeSession(
  identity: PrincipalIdentity,
  session: LocalCodeSession,
): LocalCodeSession[] {
  const rest = loadCodeSessions(identity).filter(
    (held) => held.sessionId !== session.sessionId,
  );
  const next = [session, ...rest].slice(0, KEPT_SESSIONS);
  writeStorage(sessionKey(identity), JSON.stringify(next));
  return next;
}

function sessionKey(identity: PrincipalIdentity): string {
  return `${SESSION_PREFIX}:${identityStorageKey(identity)}`;
}

function parseSession(value: unknown): LocalCodeSession | null {
  if (typeof value !== "object" || value === null || Array.isArray(value)) return null;
  const item = value as Record<string, unknown>;
  if (
    typeof item.sessionId !== "string" ||
    item.sessionId.length === 0 ||
    typeof item.seenAt !== "string"
  ) {
    return null;
  }
  return { sessionId: item.sessionId, seenAt: item.seenAt };
}
