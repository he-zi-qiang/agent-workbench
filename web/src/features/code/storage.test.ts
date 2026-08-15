import { beforeEach, describe, expect, it } from "vitest";
import type { PrincipalIdentity } from "../../api/types";
import { loadCodeSessions, rememberCodeSession } from "./storage";

const ALICE: PrincipalIdentity = {
  tenantId: "tenant_a",
  principalId: "alice",
  scopes: ["workspace:write"],
};

const BOB: PrincipalIdentity = {
  tenantId: "tenant_a",
  principalId: "bob",
  scopes: ["workspace:write"],
};

beforeEach(() => {
  window.localStorage.clear();
});

describe("code session memory", () => {
  it("puts the session just seen first, and keeps it once", () => {
    rememberCodeSession(ALICE, { sessionId: "ses_a", seenAt: "2026-08-14T10:00:00Z" });
    rememberCodeSession(ALICE, { sessionId: "ses_b", seenAt: "2026-08-14T11:00:00Z" });
    const after = rememberCodeSession(ALICE, {
      sessionId: "ses_a",
      seenAt: "2026-08-14T12:00:00Z",
    });

    // Re-seeing a session moves it, rather than adding a second row for it.
    expect(after.map((held) => held.sessionId)).toEqual(["ses_a", "ses_b"]);
    expect(after[0]?.seenAt).toBe("2026-08-14T12:00:00Z");
  });

  it("does not offer one principal the sessions of another", () => {
    rememberCodeSession(ALICE, { sessionId: "ses_a", seenAt: "2026-08-14T10:00:00Z" });

    // Two people share a browser more often than they share a machine, and a
    // list that crossed over would offer one of them a link the other opened.
    expect(loadCodeSessions(BOB)).toEqual([]);
    expect(loadCodeSessions(ALICE).map((held) => held.sessionId)).toEqual(["ses_a"]);
  });

  it("treats a damaged note as no note rather than repairing it", () => {
    rememberCodeSession(ALICE, { sessionId: "ses_a", seenAt: "2026-08-14T10:00:00Z" });
    const key = Object.keys(window.localStorage).find((name) =>
      name.startsWith("aw.code.sessions.v1"),
    );
    expect(key).toBeDefined();
    window.localStorage.setItem(key as string, "{not json");

    // Guessing at half of it would produce links this version cannot open.
    expect(loadCodeSessions(ALICE)).toEqual([]);
  });

  it("drops a row that is missing what a link needs", () => {
    const key = `aw.code.sessions.v1:${encodeURIComponent(
      JSON.stringify(["tenant_a", "alice", ["workspace:write"]]),
    )}`;
    window.localStorage.setItem(
      key,
      JSON.stringify([
        { seenAt: "2026-08-14T10:00:00Z" },
        { sessionId: "ses_good", seenAt: "2026-08-14T11:00:00Z" },
      ]),
    );

    // One bad row is not a reason to lose the good ones, and a row with no id
    // is a button that cannot navigate anywhere.
    expect(loadCodeSessions(ALICE).map((held) => held.sessionId)).toEqual(["ses_good"]);
  });
});
