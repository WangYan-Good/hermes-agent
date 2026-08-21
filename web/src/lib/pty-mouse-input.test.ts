import { describe, expect, it } from "vitest";

import {
  classifyPtyInput,
  shouldForwardClassifiedPtyInput,
} from "./pty-mouse-input";

describe("classifyPtyInput", () => {
  it.each([
    ["wheel up", "\x1b[<64;12;8M", "sgr-wheel"],
    ["wheel down", "\x1b[<65;12;8M", "sgr-wheel"],
    ["shift + wheel", "\x1b[<68;12;8M", "sgr-wheel"],
    ["meta + wheel", "\x1b[<72;12;8M", "sgr-wheel"],
    ["ctrl + wheel", "\x1b[<80;12;8M", "sgr-wheel"],
  ] as const)(
    "recognizes %s including modifier bits",
    (_name, data, expected) => {
      expect(classifyPtyInput(data)).toBe(expected);
    },
  );

  it.each([
    ["left click", "\x1b[<0;12;8M", "sgr-other"],
    ["right click", "\x1b[<2;12;8M", "sgr-other"],
    ["release", "\x1b[<0;12;8m", "sgr-other"],
    ["drag/motion", "\x1b[<32;12;8M", "sgr-other"],
    ["wheel-shaped motion", "\x1b[<96;12;8M", "sgr-other"],
    ["keyboard data", "hello", "keyboard"],
  ] as const)(
    "does not classify %s as a wheel report",
    (_name, data, expected) => {
      expect(classifyPtyInput(data)).toBe(expected);
    },
  );
});

describe("shouldForwardClassifiedPtyInput", () => {
  it("forwards wheel reports only while the socket and PTY are open", () => {
    expect(shouldForwardClassifiedPtyInput("sgr-wheel", 1, "open")).toBe(true);
    expect(
      shouldForwardClassifiedPtyInput("sgr-wheel", 1, "reconnecting"),
    ).toBe(false);
    expect(shouldForwardClassifiedPtyInput("sgr-wheel", 1, "closed")).toBe(
      false,
    );
    expect(shouldForwardClassifiedPtyInput("sgr-wheel", 3, "open")).toBe(false);
  });

  it("always drops non-wheel SGR mouse reports", () => {
    expect(shouldForwardClassifiedPtyInput("sgr-other", 1, "open")).toBe(false);
  });
});
