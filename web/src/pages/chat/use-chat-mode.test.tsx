// @vitest-environment jsdom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { ProfileContext } from "@/contexts/profile-context";
import { CHAT_MODE_STORAGE_KEY } from "./chat-mode";
import { useChatMode } from "./use-chat-mode";

const getConfig = vi.hoisted(() => vi.fn<(profile: string) => Promise<Record<string, unknown>>>());
vi.mock("@/lib/api", () => ({ api: { getConfig } }));

let container: HTMLDivElement;
let root: Root;

function ModeProbe() {
  const { requested, effective } = useChatMode();
  return <output>{requested}/{effective}</output>;
}

async function renderProfile(profile: string) {
  await act(async () => root.render(
    <ProfileContext.Provider value={{ profile, currentProfile: "default", profiles: [], setProfile: () => {} }}>
      <ModeProbe />
    </ProfileContext.Provider>,
  ));
}

beforeEach(() => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  const storage = new Map<string, string>();
  vi.stubGlobal("localStorage", {
    getItem: (key: string) => storage.get(key) ?? null,
    setItem: (key: string, value: string) => storage.set(key, value),
  });
  getConfig.mockReset();
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
});

afterEach(async () => {
  await act(async () => root.unmount());
  container.remove();
  vi.unstubAllGlobals();
});

it("uses browser preference without overwriting it when server config arrives", async () => {
  window.localStorage.setItem(CHAT_MODE_STORAGE_KEY, "terminal");
  getConfig.mockResolvedValue({ dashboard: { chat: { default_mode: "native" } } });
  await renderProfile("");
  expect(container.textContent).toBe("terminal/terminal");
  expect(window.localStorage.getItem(CHAT_MODE_STORAGE_KEY)).toBe("terminal");
  expect(getConfig).toHaveBeenCalledExactlyOnceWith("");
});

it("ignores a superseded profile response and fails safe on malformed config", async () => {
  let finishOld!: (config: Record<string, unknown>) => void;
  getConfig.mockImplementationOnce(() => new Promise((resolve) => { finishOld = resolve; }));
  await renderProfile("");
  expect(container.textContent).toBe("native/native");
  getConfig.mockResolvedValueOnce({ dashboard: { chat: { default_mode: "native" } } });
  await renderProfile("work");
  expect(container.textContent).toBe("native/native");
  await act(async () => finishOld({ dashboard: { chat: { default_mode: "terminal" } } }));
  expect(container.textContent).toBe("native/native");
  getConfig.mockResolvedValueOnce({ dashboard: { chat: "broken" } });
  await renderProfile("other");
  expect(container.textContent).toBe("native/native");
});

it("does not reuse a previous profile preference when the next config fails", async () => {
  getConfig.mockResolvedValueOnce({ dashboard: { chat: { default_mode: "native" } } });
  await renderProfile("");
  expect(container.textContent).toBe("native/native");
  getConfig.mockRejectedValueOnce(new Error("offline"));
  await renderProfile("work");
  expect(container.textContent).toBe("native/native");
});
