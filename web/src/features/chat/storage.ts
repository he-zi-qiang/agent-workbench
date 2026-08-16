import {
  identityStorageKey,
  readStorage,
  removeStorage,
  writeStorage,
} from "../../api/localStore";
import type { StreamCursor } from "../../api/sse";
import type { LocalChatSession, PrincipalIdentity } from "../../api/types";

export type StoredChatCursor = StreamCursor;

const SESSION_PREFIX = "aw.chat.sessions.v1";
const CURSOR_PREFIX = "aw.chat.cursor.v1";

// Re-exported rather than moved out of this file's callers: the name reads as
// chat's own, and the second feature that needed the same key told us where
// the implementation belongs -- not what this feature should call it.
export { identityStorageKey };

export function loadLocalSessions(identity: PrincipalIdentity): LocalChatSession[] {
  const raw = readStorage(sessionKey(identity));
  if (raw === null) return [];
  try {
    const value: unknown = JSON.parse(raw);
    if (!Array.isArray(value)) return [];
    return value.flatMap((item) => {
      const session = parseLocalSession(item);
      return session === null ? [] : [session];
    });
  } catch {
    return [];
  }
}

export function saveLocalSessions(
  identity: PrincipalIdentity,
  sessions: LocalChatSession[],
): void {
  writeStorage(sessionKey(identity), JSON.stringify(sessions));
}

export function loadChatCursor(
  identity: PrincipalIdentity,
  sessionId: string,
): StoredChatCursor | null {
  const raw = readStorage(cursorKey(identity, sessionId));
  if (raw === null) return null;
  try {
    const value: unknown = JSON.parse(raw);
    if (typeof value !== "object" || value === null || Array.isArray(value)) return null;
    const record = value as Record<string, unknown>;
    return typeof record.id === "string" &&
      record.id.length > 0 &&
      typeof record.sequence === "number" &&
      Number.isInteger(record.sequence) &&
      record.sequence >= 1
      ? { id: record.id, sequence: record.sequence }
      : null;
  } catch {
    return null;
  }
}

export function saveChatCursor(
  identity: PrincipalIdentity,
  sessionId: string,
  cursor: StoredChatCursor,
): void {
  writeStorage(cursorKey(identity, sessionId), JSON.stringify(cursor));
}

export function forgetChatCursor(
  identity: PrincipalIdentity,
  sessionId: string,
): void {
  // The cursor is keyed separately from the session list, so removing a
  // session without this leaves a row that nothing will ever read and nothing
  // will ever clean up -- and that would come back if the same id were ever
  // reused, resuming a new conversation from a dead conversation's position.
  removeStorage(cursorKey(identity, sessionId));
}

function sessionKey(identity: PrincipalIdentity): string {
  return `${SESSION_PREFIX}:${identityStorageKey(identity)}`;
}

function cursorKey(identity: PrincipalIdentity, sessionId: string): string {
  return `${CURSOR_PREFIX}:${identityStorageKey(identity)}:${encodeURIComponent(sessionId)}`;
}

function parseLocalSession(value: unknown): LocalChatSession | null {
  if (typeof value !== "object" || value === null || Array.isArray(value)) return null;
  const item = value as Record<string, unknown>;
  if (
    typeof item.sessionId !== "string" ||
    item.sessionId.length === 0 ||
    typeof item.title !== "string" ||
    typeof item.createdAt !== "string" ||
    typeof item.updatedAt !== "string"
  ) {
    return null;
  }
  const knowledgeBaseId =
    typeof item.knowledgeBaseId === "string" && item.knowledgeBaseId.length > 0
      ? item.knowledgeBaseId
      : null;
  const answerMode =
    item.answerMode === "direct" || item.answerMode === "rag"
      ? item.answerMode
      : knowledgeBaseId === null
        ? "direct"
        : "rag";
  if (answerMode === "rag" && knowledgeBaseId === null) return null;
  return {
    sessionId: item.sessionId,
    title: item.title,
    answerMode,
    knowledgeBaseId: answerMode === "direct" ? null : knowledgeBaseId,
    createdAt: item.createdAt,
    updatedAt: item.updatedAt,
  };
}
