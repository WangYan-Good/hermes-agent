// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api, fetchJSON } from "./api";

const reloadMocks = vi.hoisted(() => ({
  attemptDashboardTokenReloadOnce: vi.fn(() => false),
  clearDashboardTokenReloadAttempt: vi.fn(),
}));

vi.mock("./dashboard-auth-reload", () => ({
  attemptDashboardTokenReloadOnce: reloadMocks.attemptDashboardTokenReloadOnce,
  clearDashboardTokenReloadAttempt: reloadMocks.clearDashboardTokenReloadAttempt,
}));

const SESSION_HEADER = "X-Hermes-Session-Token";

beforeEach(() => {
  reloadMocks.attemptDashboardTokenReloadOnce.mockReset();
  reloadMocks.attemptDashboardTokenReloadOnce.mockReturnValue(false);
  reloadMocks.clearDashboardTokenReloadAttempt.mockReset();

  Object.defineProperty(window, "__HERMES_SESSION_TOKEN__", {
    configurable: true,
    value: "stale-token",
    writable: true,
  });
  Object.defineProperty(window, "__HERMES_AUTH_REQUIRED__", {
    configurable: true,
    value: false,
    writable: true,
  });
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

function jsonFetchMock(body: unknown = { ok: true }) {
  return vi.fn<typeof fetch>(
    async () =>
      new Response(JSON.stringify(body), {
        headers: { "Content-Type": "application/json" },
        status: 200,
      }),
  );
}

describe("fetchJSON", () => {
  it("tries the one-shot reload path for loopback 401s", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({
        clone: () => ({
          json: async () => ({}),
        }),
        ok: false,
        status: 401,
        statusText: "Unauthorized",
        text: async () => "Unauthorized",
      })),
    );
    reloadMocks.attemptDashboardTokenReloadOnce.mockReturnValue(true);

    const pending = fetchJSON("/api/status");
    await expect(Promise.race([pending, Promise.resolve("pending")])).resolves.toBe(
      "pending",
    );

    expect(reloadMocks.attemptDashboardTokenReloadOnce).toHaveBeenCalledTimes(1);
    expect(reloadMocks.clearDashboardTokenReloadAttempt).not.toHaveBeenCalled();
  });

  it("clears the reload latch after a successful response", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({
        json: async () => ({ ok: true }),
        ok: true,
        status: 200,
      })),
    );

    await expect(fetchJSON("/api/status")).resolves.toEqual({ ok: true });

    expect(reloadMocks.clearDashboardTokenReloadAttempt).toHaveBeenCalledTimes(1);
  });
});

describe("api.getModelOptions", () => {
  it("requests a live model refresh when asked", async () => {
    vi.stubGlobal("window", {});

    const fetchMock = jsonFetchMock({ providers: [] });
    vi.stubGlobal("fetch", fetchMock);

    await api.getModelOptions({ refresh: true });

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/model/options?refresh=1&include_unconfigured=1",
      expect.objectContaining({ credentials: "include" }),
    );
  });

  it("keeps explicit profile scoping when refreshing", async () => {
    vi.stubGlobal("window", {});

    const fetchMock = jsonFetchMock({ providers: [] });
    vi.stubGlobal("fetch", fetchMock);

    await api.getModelOptions({ profile: "default", refresh: true });

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/model/options?profile=default&refresh=1&include_unconfigured=1",
      expect.objectContaining({ credentials: "include" }),
    );
  });
});

describe("api OAuth helpers", () => {
  it("starts OAuth login in gated mode without requiring an injected session token", async () => {
    vi.stubGlobal("window", { __HERMES_AUTH_REQUIRED__: true });
    const fetchMock = jsonFetchMock({
      flow: "device_code",
      session_id: "oauth-session",
    });
    vi.stubGlobal("fetch", fetchMock);

    await api.startOAuthLogin("openai-codex");

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/providers/oauth/openai-codex/start",
      expect.objectContaining({
        body: "{}",
        credentials: "include",
        method: "POST",
      }),
    );
    const headers = fetchMock.mock.calls[0][1]?.headers as Headers;
    expect(headers.get("Content-Type")).toBe("application/json");
    expect(headers.has(SESSION_HEADER)).toBe(false);
  });

  it("still sends the injected session token for OAuth login in loopback mode", async () => {
    vi.stubGlobal("window", { __HERMES_SESSION_TOKEN__: "loopback-token" });
    const fetchMock = jsonFetchMock({
      flow: "device_code",
      session_id: "oauth-session",
    });
    vi.stubGlobal("fetch", fetchMock);

    await api.startOAuthLogin("openai-codex");

    const headers = fetchMock.mock.calls[0][1]?.headers as Headers;
    expect(headers.get(SESSION_HEADER)).toBe("loopback-token");
  });

  it("runs provider auth mutations in gated mode via cookie auth", async () => {
    vi.stubGlobal("window", { __HERMES_AUTH_REQUIRED__: true });
    const fetchMock = jsonFetchMock({ ok: true });
    vi.stubGlobal("fetch", fetchMock);

    await api.disconnectOAuthProvider("anthropic");
    await api.submitOAuthCode("anthropic", "oauth-session", "code-123");
    await api.cancelOAuthSession("oauth-session");
    await api.revealEnvVar("OPENAI_API_KEY");

    for (const call of fetchMock.mock.calls) {
      const init = call[1] as RequestInit;
      expect(init.credentials).toBe("include");
      expect((init.headers as Headers).has(SESSION_HEADER)).toBe(false);
    }
  });
});

describe("api provider proxy", () => {
  it("PUTs the pending proxy setting to the provider-agnostic route", async () => {
    vi.stubGlobal("window", {});
    const fetchMock = jsonFetchMock({ ok: true });
    vi.stubGlobal("fetch", fetchMock);

    await api.setProviderProxy("openai-codex", {
      mode: "url",
      url: "http://127.0.0.1:7890",
    });

    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/providers/openai-codex/proxy");
    expect((init as RequestInit).method).toBe("PUT");
    expect((init as RequestInit).body).toBe(
      JSON.stringify({ mode: "url", url: "http://127.0.0.1:7890" }),
    );
  });

  it("POSTs a test without saving anything", async () => {
    vi.stubGlobal("window", {});
    const fetchMock = jsonFetchMock({ kind: "reachable", ok: true });
    vi.stubGlobal("fetch", fetchMock);

    await api.testProviderProxy("anthropic", { mode: "direct" });

    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/providers/anthropic/proxy/test");
    expect((init as RequestInit).method).toBe("POST");
    expect((init as RequestInit).body).toBe(JSON.stringify({ mode: "direct" }));
  });

  it("escapes the provider id in the path", async () => {
    vi.stubGlobal("window", {});
    const fetchMock = jsonFetchMock({ ok: true });
    vi.stubGlobal("fetch", fetchMock);

    await api.setProviderProxy("a/b", { mode: "inherit" });

    expect(fetchMock.mock.calls[0][0]).toBe("/api/providers/a%2Fb/proxy");
  });
});

describe("api migration targets", () => {
  it("posts a target to the collection route", async () => {
    vi.stubGlobal("window", {});
    const fetchMock = jsonFetchMock({ ok: true, target: {} });
    vi.stubGlobal("fetch", fetchMock);

    await api.createMigrationTarget({ id: "prod", host: "h", user: "u" });

    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/migration/targets");
    expect((init as RequestInit).method).toBe("POST");
    expect((init as RequestInit).body).toBe(
      JSON.stringify({ id: "prod", host: "h", user: "u" }),
    );
  });

  it("encodes the id in per-target routes", async () => {
    vi.stubGlobal("window", {});
    const fetchMock = jsonFetchMock({ ok: true });
    vi.stubGlobal("fetch", fetchMock);

    await api.deleteMigrationTarget("a b");

    expect(fetchMock.mock.calls[0][0]).toBe("/api/migration/targets/a%20b");
    expect((fetchMock.mock.calls[0][1] as RequestInit).method).toBe("DELETE");
  });

  it("passes confirm_overwrite as a query parameter", async () => {
    vi.stubGlobal("window", {});
    const fetchMock = jsonFetchMock({ ok: true, action: "migrate-host" });
    vi.stubGlobal("fetch", fetchMock);

    await api.startMigration("prod", true);

    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toContain("/api/migration/targets/prod/migrate");
    expect(url).toContain("confirm_overwrite=true");
    expect((init as RequestInit).method).toBe("POST");
  });

  it("defaults confirm_overwrite to false when omitted", async () => {
    vi.stubGlobal("window", {});
    const fetchMock = jsonFetchMock({ ok: true, action: "migrate-host" });
    vi.stubGlobal("fetch", fetchMock);

    await api.startMigration("prod");

    expect(fetchMock.mock.calls[0][0]).toContain("confirm_overwrite=false");
  });

  it("lists targets from the collection route", async () => {
    vi.stubGlobal("window", {});
    const fetchMock = jsonFetchMock({ targets: [] });
    vi.stubGlobal("fetch", fetchMock);

    await api.listMigrationTargets();

    expect(fetchMock.mock.calls[0][0]).toBe("/api/migration/targets");
  });

  it("PUTs an update to the per-target route", async () => {
    vi.stubGlobal("window", {});
    const fetchMock = jsonFetchMock({ ok: true, target: {} });
    vi.stubGlobal("fetch", fetchMock);

    await api.updateMigrationTarget("prod", { label: "Prod box" });

    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/migration/targets/prod");
    expect((init as RequestInit).method).toBe("PUT");
  });

  it("POSTs to the preflight route", async () => {
    vi.stubGlobal("window", {});
    const fetchMock = jsonFetchMock({ checks: [], blocked: false });
    vi.stubGlobal("fetch", fetchMock);

    await api.preflightMigrationTarget("prod");

    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/migration/targets/prod/preflight");
    expect((init as RequestInit).method).toBe("POST");
  });
});
