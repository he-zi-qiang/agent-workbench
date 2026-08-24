import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it } from "vitest";
import { IdentityProvider, useIdentity } from "./IdentityContext";

function ScopeReadout() {
  const { identity } = useIdentity();
  return <p>{identity.scopes.join(" ")}</p>;
}

describe("stored identity", () => {
  beforeEach(() => {
    localStorage.clear();
  });

  it.each(["aw.identity.v1", "aw.identity.v2"])(
    "does not hand a console the scope set stored under %s",
    (staleKey) => {
      // The regression, in the browsers most likely to have it: the default
      // scope set grew, but a console opened before that kept the stored one,
      // because stored state is returned as it stands rather than merged with
      // today's default. The tool was then denied with
      // `missing_permission_scope` on a deployment that had enabled it -- and
      // the consoles carrying the stale set are exactly the ones already used
      // to try. Both retired keys are checked, because a fix that reads neither
      // is the only one that holds for the next scope too.
      localStorage.setItem(
        staleKey,
        JSON.stringify({
          tenantId: "tenant_local",
          principalId: "user_local",
          scopes: ["artifact:export"],
        }),
      );

      render(
        <IdentityProvider>
          <ScopeReadout />
        </IdentityProvider>,
      );

      expect(screen.getByText(/external:search/)).toBeInTheDocument();
    },
  );

  it("asks for the scopes the Work page's own graph needs", () => {
    // Not a restatement of the constant. `workspace:write` is what v2's `work`
    // node needs to put a document in the workspace at all, and `mcp:word` is
    // what lets it render one -- the pair `docs/word-mcp-local.md:163` names.
    // Without them a Task submitted from this console reaches the tool, is
    // denied `missing_permission_scope`, and settles as 已完成 having produced
    // no document. A default that omits either is that bug again.
    //
    // `sandbox:run` joins them for the same reason on the Code side (ADR-057):
    // without it a coding session is offered `sandbox_run`, proposes it, and is
    // denied by a gate the reader has no way to see from the transcript.
    render(
      <IdentityProvider>
        <ScopeReadout />
      </IdentityProvider>,
    );

    const scopes = screen.getByText(/artifact:export/).textContent ?? "";
    expect(scopes.split(" ")).toEqual(
      expect.arrayContaining([
        "artifact:export",
        "external:search",
        "workspace:write",
        "mcp:web",
        "mcp:word",
        "sandbox:run",
      ]),
    );
  });

  it("keeps an identity edited under the current key", () => {
    // The control over the cheaper fix. Ignoring stored identity altogether
    // would also satisfy the two tests above, and would break the identity
    // editor: a reader acting as another tenant would be back to `user_local`
    // on the next reload. The stale sets had to stop being read; reading stored
    // identity did not.
    localStorage.setItem(
      "aw.identity.v5",
      JSON.stringify({
        tenantId: "tenant_other",
        principalId: "user_other",
        scopes: ["artifact:export"],
      }),
    );

    render(
      <IdentityProvider>
        <ScopeReadout />
      </IdentityProvider>,
    );

    expect(screen.getByText("artifact:export")).toBeInTheDocument();
  });
});
