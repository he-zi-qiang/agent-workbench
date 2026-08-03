import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { createChatSession } from "../../api/client";
import type { PrincipalIdentity } from "../../api/types";
import { useIdentity } from "../../app/IdentityContext";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ChatPage } from "./ChatPage";
import { initialChatState } from "./model";
import type { ChatRuntime } from "./runtime";
import { useChatRuntime } from "./useChatRuntime";

vi.mock("../../api/client", () => ({
  createChatSession: vi.fn(),
}));

vi.mock("../../app/IdentityContext", () => ({
  useIdentity: vi.fn(),
}));

vi.mock("./useChatRuntime", () => ({
  useChatRuntime: vi.fn(),
}));

const ALICE: PrincipalIdentity = {
  tenantId: "tenant_a",
  principalId: "alice",
  scopes: ["knowledge:read"],
};

const BOB: PrincipalIdentity = {
  tenantId: "tenant_a",
  principalId: "bob",
  scopes: ["knowledge:read"],
};

let currentIdentity = ALICE;
let aliceRuntime: ChatRuntime;
let bobRuntime: ChatRuntime;
let aliceAddLocalSession = vi.fn();
let aliceStartAsk = vi.fn();
let bobStartAsk = vi.fn();

beforeEach(() => {
  vi.clearAllMocks();
  currentIdentity = ALICE;
  aliceAddLocalSession = vi.fn();
  aliceStartAsk = vi.fn();
  bobStartAsk = vi.fn();
  aliceRuntime = fakeRuntime(aliceAddLocalSession, aliceStartAsk);
  bobRuntime = fakeRuntime(vi.fn(), bobStartAsk);
  vi.mocked(useIdentity).mockImplementation(() => ({
    identity: currentIdentity,
    updateIdentity: vi.fn(),
    editorOpen: false,
    setEditorOpen: vi.fn(),
  }));
  vi.mocked(useChatRuntime).mockImplementation((identity) => ({
    runtime: identity.principalId === ALICE.principalId ? aliceRuntime : bobRuntime,
    state: initialChatState(),
  }));
});

describe("Chat identity boundary", () => {
  it("does not start an Ask in the old identity after Session creation resolves", async () => {
    let resolveCreate: ((response: { session_id: string }) => void) | undefined;
    vi.mocked(createChatSession).mockReturnValue(
      new Promise((resolve) => {
        resolveCreate = resolve;
      }),
    );

    const view = render(
      <MemoryRouter>
        <ChatPage key="alice" />
      </MemoryRouter>,
    );
    fireEvent.change(screen.getByLabelText("问题"), {
      target: { value: "Which identity owns this request?" },
    });
    fireEvent.click(screen.getByRole("button", { name: "发送问题" }));
    await waitFor(() => expect(createChatSession).toHaveBeenCalledTimes(1));

    currentIdentity = BOB;
    view.rerender(
      <MemoryRouter>
        <ChatPage key="bob" />
      </MemoryRouter>,
    );

    const finishCreate = resolveCreate;
    if (finishCreate === undefined) throw new Error("Session create mock did not start");
    act(() => {
      finishCreate({ session_id: "ses_created_as_alice" });
    });
    await waitFor(() =>
      expect(aliceAddLocalSession).toHaveBeenCalledTimes(1),
    );

    expect(aliceStartAsk).not.toHaveBeenCalled();
    expect(bobStartAsk).not.toHaveBeenCalled();
  });
});

function fakeRuntime(addLocalSession: () => void, startAsk: () => void): ChatRuntime {
  return {
    addLocalSession,
    startAsk,
  } as unknown as ChatRuntime;
}
