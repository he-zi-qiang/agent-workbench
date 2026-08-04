import { afterEach, describe, expect, it, vi } from "vitest";
import { apiRequest, askChat, createTask, identityHeaders } from "./client";
import type { PrincipalIdentity } from "./types";

const identity: PrincipalIdentity = {
  tenantId: "tenant_a",
  principalId: "principal_a",
  scopes: [],
};

afterEach(() => vi.restoreAllMocks());

describe("identity headers", () => {
  it("omits an empty scopes header", () => {
    expect(identityHeaders(identity)).toEqual({
      "x-tenant-id": "tenant_a",
      "x-principal-id": "principal_a",
    });
  });

  it("serializes non-empty scopes", () => {
    expect(identityHeaders({ ...identity, scopes: ["artifact:export", "admin"] })).toEqual({
      "x-tenant-id": "tenant_a",
      "x-principal-id": "principal_a",
      "x-principal-scopes": "artifact:export,admin",
    });
  });
});

describe("apiRequest", () => {
  it("sends the development identity on protected requests", async () => {
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValue(new Response(JSON.stringify({ ok: true }), { status: 200 }));

    await apiRequest(identity, "/v1/example");

    const init = fetchMock.mock.calls[0]?.[1];
    expect(new Headers(init?.headers).get("x-tenant-id")).toBe("tenant_a");
    expect(new Headers(init?.headers).get("x-principal-id")).toBe("principal_a");
    expect(new Headers(init?.headers).has("x-principal-scopes")).toBe(false);
  });

  it("sends an explicit direct source without a knowledge base", async () => {
    const response = {
      answer: "Direct answer",
      citations: [],
      withheld: false,
      grounded: false,
      run_id: "run_1",
      turn_id: "turn_1",
    };
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValue(new Response(JSON.stringify(response), { status: 200 }));

    await askChat(
      identity,
      "ses_1",
      {
        question: "hello",
        answerMode: "direct",
        knowledgeBaseId: null,
      },
      "chat:direct",
    );

    const init = fetchMock.mock.calls[0]?.[1];
    if (typeof init?.body !== "string") throw new Error("request body was not JSON");
    expect(JSON.parse(init.body)).toMatchObject({
      answer_mode: "direct",
      knowledge_base_id: null,
      question: "hello",
    });
  });

  it("lets a task retry reuse the same idempotency key", async () => {
    const response = {
      task_id: "task_1",
      status: "queued",
      status_detail: null,
      created_at: "2026-08-02T00:00:00Z",
      updated_at: "2026-08-02T00:00:00Z",
    };
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockImplementation(() =>
        Promise.resolve(new Response(JSON.stringify(response), { status: 201 })),
      );

    const input = { objective: "research", maxRevisions: 2, wantsReport: false };
    await createTask(identity, input, "task:stable-attempt");
    await createTask(identity, input, "task:stable-attempt");

    for (const [, init] of fetchMock.mock.calls) {
      expect(new Headers(init?.headers).get("idempotency-key")).toBe(
        "task:stable-attempt",
      );
    }
  });
});
