import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

// Vitest runs without `globals`, so React Testing Library never sees a global
// `afterEach` to register its own cleanup on. Without this, every render in a
// file stays mounted for the rest of that file and `getBy*` starts matching
// elements from earlier tests.
afterEach(cleanup);

// jsdom implements neither half of the object-URL API, so a component that
// turns fetched bytes into something a frame can read throws on render instead
// of showing anything -- which would make every such test fail for a reason
// that has nothing to do with what it is testing.
//
// Defined on `URL` rather than replacing the global, because a test that stubs
// the whole object (`api/client.test.ts` does, deliberately, for one call)
// takes `new URL()` with it, and that is not a trade a shared setup should make
// for every file. The ids are distinct so that a test can prove the URL it was
// handed is the one that got revoked.
let objectUrlNumber = 0;
Object.defineProperty(URL, "createObjectURL", {
  writable: true,
  configurable: true,
  value: () => `blob:agent-workbench/${String(++objectUrlNumber)}`,
});
Object.defineProperty(URL, "revokeObjectURL", {
  writable: true,
  configurable: true,
  value: () => undefined,
});

Object.defineProperty(window, "matchMedia", {
  writable: true,
  value: (query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: () => undefined,
    removeListener: () => undefined,
    addEventListener: () => undefined,
    removeEventListener: () => undefined,
    dispatchEvent: () => false,
  }),
});

// jsdom has no layout and no `ResizeObserver`, so a component that measures a
// box to scale something into it would throw on mount. Defined as a no-op that
// never fires: with no observation, `useBoxSize` keeps returning null, and the
// preview falls back to rendering at 100% -- which is exactly the behaviour a
// test without layout should see, and the one asserted in HtmlPreview.test.tsx.
Object.defineProperty(window, "ResizeObserver", {
  writable: true,
  configurable: true,
  value: class {
    observe() {}
    unobserve() {}
    disconnect() {}
  },
});
