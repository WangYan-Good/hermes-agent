import { describe, expect, it } from "vitest";

import {
  initialProxyEditorValue,
  isProxyDirty,
  isRedactedProxyUrl,
  normalizeProxyState,
  proxyBadge,
  proxyDisplayHost,
  proxySubmitError,
  toProxyPayload,
} from "./provider-proxy";

describe("normalizeProxyState", () => {
  it("reads the three configured states", () => {
    expect(normalizeProxyState({ mode: "inherit", url: null })).toEqual({
      mode: "inherit",
      url: null,
    });
    expect(normalizeProxyState({ mode: "direct", url: null })).toEqual({
      mode: "direct",
      url: null,
    });
    expect(
      normalizeProxyState({ mode: "url", url: "http://127.0.0.1:7890" }),
    ).toEqual({ mode: "url", url: "http://127.0.0.1:7890" });
  });

  it("falls back to inherit for a missing or unrecognised payload", () => {
    // inherit is the state that changes nothing about how requests are made,
    // so it is the only safe thing to assume.
    expect(normalizeProxyState(null)).toEqual({ mode: "inherit", url: null });
    expect(normalizeProxyState(undefined)).toEqual({ mode: "inherit", url: null });
    expect(
      normalizeProxyState({ mode: "off" as never, url: null }),
    ).toEqual({ mode: "inherit", url: null });
  });

  it("keeps the invalid flag for an unparseable stored value", () => {
    expect(
      normalizeProxyState({ mode: "url", url: null, invalid: true }),
    ).toEqual({ mode: "url", url: null, invalid: true });
  });
});

describe("isRedactedProxyUrl", () => {
  it("detects the masked form the API returns", () => {
    expect(isRedactedProxyUrl("http://***@proxy.internal:3128")).toBe(true);
    expect(isRedactedProxyUrl("socks5://***@10.0.0.2:1080")).toBe(true);
  });

  it("passes real addresses through", () => {
    expect(isRedactedProxyUrl("http://127.0.0.1:7890")).toBe(false);
    expect(isRedactedProxyUrl("http://bob:pass@proxy.internal:3128")).toBe(false);
    expect(isRedactedProxyUrl("")).toBe(false);
    expect(isRedactedProxyUrl(null)).toBe(false);
  });
});

describe("proxySubmitError", () => {
  it("allows the two modes that need no address", () => {
    expect(proxySubmitError("inherit", "")).toBeNull();
    expect(proxySubmitError("direct", "")).toBeNull();
  });

  it("requires an address in url mode", () => {
    expect(proxySubmitError("url", "   ")).toBe("urlRequired");
  });

  it("refuses to persist the masked form", () => {
    expect(proxySubmitError("url", "http://***@proxy.internal:3128")).toBe(
      "redactedUrl",
    );
  });

  it("accepts a real address", () => {
    expect(proxySubmitError("url", "http://127.0.0.1:7890")).toBeNull();
  });
});

describe("toProxyPayload", () => {
  it("sends the address only in url mode", () => {
    expect(toProxyPayload("url", "  http://127.0.0.1:7890 ")).toEqual({
      mode: "url",
      url: "http://127.0.0.1:7890",
    });
    // A half-typed address stays in the box while the select is flipped, and
    // must not travel with the request.
    expect(toProxyPayload("direct", "http://half-typed")).toEqual({
      mode: "direct",
    });
    expect(toProxyPayload("inherit", "http://half-typed")).toEqual({
      mode: "inherit",
    });
  });
});

describe("isProxyDirty", () => {
  const saved = { mode: "url" as const, url: "http://127.0.0.1:7890" };

  it("is clean when nothing changed", () => {
    expect(isProxyDirty(saved, "url", "http://127.0.0.1:7890")).toBe(false);
    expect(isProxyDirty({ mode: "direct", url: null }, "direct", "")).toBe(false);
  });

  it("is dirty on a mode change", () => {
    expect(isProxyDirty(saved, "direct", "")).toBe(true);
    expect(isProxyDirty({ mode: "inherit", url: null }, "url", "")).toBe(true);
  });

  it("is dirty on an address change", () => {
    expect(isProxyDirty(saved, "url", "http://127.0.0.1:1080")).toBe(true);
  });

  it("stays clean when a redacted address is left unfilled", () => {
    // The editor never prefills a masked address; an empty box means
    // "unchanged", not "cleared".
    const redacted = { mode: "url" as const, url: "http://***@proxy.internal:3128" };
    expect(isProxyDirty(redacted, "url", "")).toBe(false);
    expect(isProxyDirty(redacted, "url", "http://bob:pass@proxy.internal:3128")).toBe(
      true,
    );
  });
});

describe("proxyDisplayHost", () => {
  it("drops the scheme and trailing slashes", () => {
    expect(proxyDisplayHost("http://127.0.0.1:7890")).toBe("127.0.0.1:7890");
    expect(proxyDisplayHost("socks5://10.0.0.2:1080/")).toBe("10.0.0.2:1080");
    expect(proxyDisplayHost(null)).toBe("");
  });
});

describe("proxyBadge", () => {
  it("shows the host for a configured proxy", () => {
    expect(proxyBadge({ mode: "url", url: "http://127.0.0.1:7890" })).toEqual({
      kind: "proxy",
      host: "127.0.0.1:7890",
    });
  });

  it("marks a forced-direct provider", () => {
    expect(proxyBadge({ mode: "direct", url: null })).toEqual({
      kind: "direct",
      host: "",
    });
  });

  it("stays quiet for an unconfigured provider", () => {
    // Every OAuth provider gets a row; a badge on each would drown the login
    // status the card exists to show.
    expect(proxyBadge({ mode: "inherit", url: null })).toEqual({
      kind: "none",
      host: "",
    });
    expect(proxyBadge(null)).toEqual({ kind: "none", host: "" });
  });

  it("flags an unparseable stored value", () => {
    expect(proxyBadge({ mode: "url", url: null, invalid: true })).toEqual({
      kind: "invalid",
      host: "",
    });
  });
});

describe("initialProxyEditorValue", () => {
  it("prefills a usable address", () => {
    expect(
      initialProxyEditorValue({ mode: "url", url: "http://127.0.0.1:7890" }),
    ).toEqual({ mode: "url", url: "http://127.0.0.1:7890" });
  });

  it("never prefills a masked address", () => {
    // Leaving it in the box invites saving `***` back as the username.
    expect(
      initialProxyEditorValue({ mode: "url", url: "http://***@proxy.internal:3128" }),
    ).toEqual({ mode: "url", url: "" });
  });

  it("opens in the saved mode", () => {
    expect(initialProxyEditorValue({ mode: "direct", url: null })).toEqual({
      mode: "direct",
      url: "",
    });
    expect(initialProxyEditorValue(null)).toEqual({ mode: "inherit", url: "" });
  });
});
