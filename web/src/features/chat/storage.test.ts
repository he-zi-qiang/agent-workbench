import type { LocalChatSession, PrincipalIdentity } from "../../api/types";
import { beforeEach, describe, expect, it } from "vitest";
import {
  identityStorageKey,
  loadChatCursor,
  loadLocalSessions,
  saveChatCursor,
  saveLocalSessions,
} from "./storage";

const ALICE: PrincipalIdentity = {
  tenantId: "tenant_a",
  principalId: "alice",
  scopes: ["artifact:export", "knowledge:read"],
};

const BOB: PrincipalIdentity = {
  tenantId: "tenant_a",
  principalId: "bob",
  scopes: ["knowledge:read"],
};

beforeEach(() => window.localStorage.clear());

describe("identity-scoped chat storage", () => {
  it("uses tenant, principal and the normalized scope set in the namespace", () => {
    expect(identityStorageKey(ALICE)).toBe(
      identityStorageKey({ ...ALICE, scopes: [...ALICE.scopes].reverse() }),
    );
    expect(identityStorageKey(ALICE)).not.toBe(identityStorageKey(BOB));
  });

  it("keeps cursors isolated by both identity and session", () => {
    saveChatCursor(ALICE, "ses_1", { id: "cursor_4", sequence: 4 });

    expect(loadChatCursor(ALICE, "ses_1")).toEqual({ id: "cursor_4", sequence: 4 });
    expect(loadChatCursor(ALICE, "ses_2")).toBeNull();
    expect(loadChatCursor(BOB, "ses_1")).toBeNull();
  });

  it("persists only local session metadata inside the identity namespace", () => {
    const session: LocalChatSession = {
      sessionId: "ses_1",
      title: "Local only",
      knowledgeBaseId: "kb_main",
      createdAt: "2026-08-02T12:00:00Z",
      updatedAt: "2026-08-02T12:01:00Z",
    };
    saveLocalSessions(ALICE, [session]);

    expect(loadLocalSessions(ALICE)).toEqual([session]);
    expect(loadLocalSessions(BOB)).toEqual([]);
  });
});
