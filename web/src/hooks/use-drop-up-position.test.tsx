// @vitest-environment jsdom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useRef } from "react";
import { useDropUpPosition } from "./use-drop-up-position";

let container: HTMLDivElement;
let root: Root;
let anchorRect: Pick<DOMRect, "left" | "top">;

function Harness({ enabled, layoutKey }: { enabled: boolean; layoutKey: number }) {
  const anchorRef = useRef<HTMLDivElement>(null);
  const position = useDropUpPosition(anchorRef, enabled);

  return (
    <div data-layout-key={layoutKey} ref={anchorRef}>
      <output data-testid="position">
        {position ? `${position.bottom}:${position.left}` : "unset"}
      </output>
    </div>
  );
}

async function render(enabled: boolean, layoutKey = 0) {
  await act(async () => root.render(<Harness enabled={enabled} layoutKey={layoutKey} />));
}

beforeEach(() => {
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  anchorRect = { left: 24, top: 300 };
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(
    () => ({
      bottom: 0,
      height: 0,
      left: anchorRect.left,
      right: 0,
      toJSON: () => ({}),
      top: anchorRect.top,
      width: 0,
      x: anchorRect.left,
      y: anchorRect.top,
    }),
  );
  vi.stubGlobal(
    "ResizeObserver",
    class {
      disconnect() {}
      observe() {}
      unobserve() {}
    },
  );
  Object.defineProperty(window, "innerHeight", {
    configurable: true,
    value: 800,
  });
});

afterEach(async () => {
  await act(async () => root.unmount());
  container.remove();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("useDropUpPosition", () => {
  it("measures immediately when a mobile sheet switches to a desktop drop-up", async () => {
    await render(false);
    expect(container.querySelector("output")?.textContent).toBe("unset");

    await render(true);
    expect(container.querySelector("output")?.textContent).toBe("504:24");
  });

  it("remeasures after a parent layout change moves the anchor", async () => {
    await render(true);
    expect(container.querySelector("output")?.textContent).toBe("504:24");

    anchorRect = { left: 88, top: 220 };
    await render(true, 1);

    expect(container.querySelector("output")?.textContent).toBe("584:88");
  });
});
