import { describe, expect, it } from "vitest";
import { workIdentityQueryKey } from "./workQueryKeys";

describe("workIdentityQueryKey", () => {
  it("does not collide when valid tenant and principal IDs contain separators", () => {
    const first = workIdentityQueryKey({
      tenantId: "a",
      principalId: "b:c",
      scopes: [],
    });
    const second = workIdentityQueryKey({
      tenantId: "a:b",
      principalId: "c",
      scopes: [],
    });

    expect(first).not.toEqual(second);
  });

  it("normalizes scope order like the server identity boundary", () => {
    const first = workIdentityQueryKey({
      tenantId: "tenant_1",
      principalId: "user_1",
      scopes: ["artifact:write", "artifact:read"],
    });
    const second = workIdentityQueryKey({
      tenantId: "tenant_1",
      principalId: "user_1",
      scopes: ["artifact:read", "artifact:write"],
    });

    expect(first).toEqual(second);
  });
});
