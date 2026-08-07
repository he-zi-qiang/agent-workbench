import type { LocalChatSession, PrincipalIdentity } from "../../api/types";

export interface StoredChatCursor {
  id: string;
  sequence: number;
}

const SESSION_PREFIX = "aw.chat.sessions.v1";
const CURSOR_PREFIX = "aw.chat.cursor.v1";

export function identityStorageKey(identity: PrincipalIdentity): string {
  return encodeURIComponent(
    JSON.stringify([
      identity.tenantId,
      identity.principalId,
      [...identity.scopes].sort(),
    ]),
  );
}

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

export function clearChatCursor(identity: PrincipalIdentity, sessionId: string): void {
  try {
    window.localStorage.removeItem(cursorKey(identity, sessionId));
  } catch {
    // Storage is a resilience aid. A privacy mode that refuses it must not make
    // the authenticated stream unusable for the lifetime of this page.
  }
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

function readStorage(key: string): string | null {
  try {
    return window.localStorage.getItem(key);
  } catch {
    return null;
  }
}

function writeStorage(key: string, value: string): void {
  try {
    window.localStorage.setItem(key, value);
  } catch {
    // See clearChatCursor: in-memory runtime state remains fully functional.
  }
}
