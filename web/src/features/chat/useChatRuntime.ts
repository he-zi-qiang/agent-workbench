import { useSyncExternalStore } from "react";
import type { PrincipalIdentity } from "../../api/types";
import { chatRuntimeFor, type ChatRuntime } from "./runtime";
import type { ChatState } from "./model";

export function useChatRuntime(identity: PrincipalIdentity): {
  runtime: ChatRuntime;
  state: ChatState;
} {
  const runtime = chatRuntimeFor(identity);
  const state = useSyncExternalStore(
    runtime.subscribe,
    runtime.getSnapshot,
    runtime.getSnapshot,
  );
  return { runtime, state };
}
