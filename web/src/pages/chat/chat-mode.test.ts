import { afterEach, describe, expect, it, vi } from "vitest";

import {
  CHAT_MODE_STORAGE_KEY,
  normalizeChatMode,
  readBrowserChatMode,
  resolveChatMode,
} from "./chat-mode";

afterEach(() => vi.unstubAllGlobals());

describe("chat mode contract", () => {
  it.each(["terminal", "native"])("accepts only the literal %s", (value) => {
    expect(normalizeChatMode(value)).toBe(value);
  });

  it.each([null, undefined, "", "TUI", "xterm", "chatgpt", "Native", " terminal ", "future-mode", 1, {}, []])(
    "rejects unknown value %j",
    (value) => {
      expect(normalizeChatMode(value)).toBeNull();
      expect(resolveChatMode({ browserMode: value, serverMode: value })).toEqual({
        requested: "terminal", effective: "terminal",
      });
    },
  );

  it("falls back to terminal without any preference", () => {
    expect(resolveChatMode()).toEqual({ requested: "terminal", effective: "terminal" });
  });

  it("uses server default, but falls back for unsupported native", () => {
    expect(resolveChatMode({ serverMode: "native" })).toEqual({
      requested: "native", effective: "terminal",
    });
  });

  it("prefers valid browser preference over server default", () => {
    expect(resolveChatMode({ browserMode: "terminal", serverMode: "native" })).toEqual({
      requested: "terminal", effective: "terminal",
    });
    expect(resolveChatMode({ browserMode: "native", serverMode: "terminal" })).toEqual({
      requested: "native", effective: "terminal",
    });
    expect(resolveChatMode({ browserMode: "future-mode", serverMode: "native" }).requested).toBe("native");
  });

  it("resolves available native only with explicit support", () => {
    expect(resolveChatMode({ serverMode: "native", nativeAvailable: true })).toEqual({
      requested: "native", effective: "native",
    });
  });

  it.each(["native", "terminal", '{"mode":"native"}', "future-mode", "", null])(
    "reads browser storage without writing: %j", (value) => {
      const getItem = vi.fn(() => value);
      const setItem = vi.fn();
      vi.stubGlobal("window", { localStorage: { getItem, setItem } });
      expect(readBrowserChatMode()).toBe(normalizeChatMode(value));
      expect(getItem).toHaveBeenCalledWith(CHAT_MODE_STORAGE_KEY);
      expect(setItem).not.toHaveBeenCalled();
      expect(resolveChatMode({ browserMode: readBrowserChatMode() }).effective).toBe("terminal");
    },
  );

  it("survives blocked storage and non-browser rendering", () => {
    vi.stubGlobal("window", { get localStorage() { throw new Error("blocked"); } });
    expect(readBrowserChatMode()).toBeNull();
    vi.stubGlobal("window", undefined);
    expect(readBrowserChatMode()).toBeNull();
  });
});

it("prioritizes explicit URL modes and keeps unrequested native unavailable", () => {
  expect(resolveChatMode({ urlMode: "native", browserMode: "terminal", nativeAvailable: true }).effective).toBe("native");
  expect(resolveChatMode({ urlMode: "terminal", browserMode: "native", serverMode: "native", nativeAvailable: true }).effective).toBe("terminal");
  expect(resolveChatMode({ serverMode: "native", nativeAvailable: false }).effective).toBe("terminal");
});
