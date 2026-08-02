/** Mapping between the dashboard's three-state proxy control and the wire
 *  value of `providers.<id>.proxy`.
 *
 *  The config has three states — key absent (follow the environment
 *  variables), `false` (force direct), and a URL — and the editor has to
 *  round-trip all of them. A single text box could not: it would show a
 *  hand-written `proxy: false` as empty and erase it on the next save.
 *
 *  Logic lives here rather than in the component because this repo's frontend
 *  tests cover `lib/` only.
 */

export type ProxyMode = "inherit" | "direct" | "url";

export interface ProviderProxyState {
  mode: ProxyMode;
  url: string | null;
  /** The stored value could not be parsed (a hand-edited `proxy: true`).
   *  The card still renders; the operator retypes the address. */
  invalid?: boolean;
}

export const PROXY_MODES: readonly ProxyMode[] = ["inherit", "direct", "url"];

const INHERIT: ProviderProxyState = { mode: "inherit", url: null };

function isProxyMode(value: unknown): value is ProxyMode {
  return (
    value === "inherit" || value === "direct" || value === "url"
  );
}

/** Coerce whatever the API returned into a state the control can render.
 *  Anything unrecognised reads as "inherit", which is the state that changes
 *  nothing about how requests are made today. */
export function normalizeProxyState(
  raw: ProviderProxyState | null | undefined,
): ProviderProxyState {
  if (!raw || !isProxyMode(raw.mode)) return INHERIT;
  const url = typeof raw.url === "string" && raw.url.trim() ? raw.url : null;
  const state: ProviderProxyState = { mode: raw.mode, url };
  if (raw.invalid) state.invalid = true;
  return state;
}

/** True for the `scheme://***@host` form the API returns for a proxy carrying
 *  credentials. Submitting it back unchanged would persist `***` as the
 *  username, so the editor blocks it and asks for the full address. */
export function isRedactedProxyUrl(value: string | null | undefined): boolean {
  const candidate = (value ?? "").trim();
  if (!candidate) return false;
  const match = /^[a-z0-9+.-]+:\/\/([^/?#]*)/i.exec(candidate);
  if (!match) return false;
  const authority = match[1];
  const at = authority.lastIndexOf("@");
  if (at < 0) return false;
  return authority.slice(0, at) === "***";
}

/** Why this (mode, url) pair cannot be submitted, or null when it can.
 *  Returns an i18n key suffix rather than a message — the component owns the
 *  copy. */
export function proxySubmitError(
  mode: ProxyMode,
  url: string,
): "urlRequired" | "redactedUrl" | null {
  if (mode !== "url") return null;
  if (!url.trim()) return "urlRequired";
  if (isRedactedProxyUrl(url)) return "redactedUrl";
  return null;
}

/** The request body for PUT/POST. `url` is sent only for `url` mode: the
 *  backend ignores it otherwise, so a half-typed address can stay in the box
 *  while the user flips the select. */
export function toProxyPayload(
  mode: ProxyMode,
  url: string,
): { mode: ProxyMode; url?: string } {
  return mode === "url" ? { mode, url: url.trim() } : { mode };
}

/** Whether the editor differs from what is saved — drives the Save button. */
export function isProxyDirty(
  saved: ProviderProxyState | null | undefined,
  mode: ProxyMode,
  url: string,
): boolean {
  const state = normalizeProxyState(saved);
  if (mode !== state.mode) return true;
  if (mode !== "url") return false;
  const savedUrl = (state.url ?? "").trim();
  // A redacted address is never prefilled, so an empty box means "unchanged",
  // not "cleared". Without this the Save button lights up the moment a
  // credential-carrying proxy's editor is opened.
  if (isRedactedProxyUrl(savedUrl) && !url.trim()) return false;
  return url.trim() !== savedUrl;
}

/** The address without its scheme, for the collapsed badge: a bare
 *  `127.0.0.1:7890` reads faster than the full URL in a dense row. */
export function proxyDisplayHost(url: string | null | undefined): string {
  const candidate = (url ?? "").trim();
  if (!candidate) return "";
  return candidate.replace(/^[a-z0-9+.-]+:\/\//i, "").replace(/\/+$/, "");
}

export type ProxyBadgeKind = "proxy" | "direct" | "none" | "invalid";

/** What the collapsed row shows. `none` means an unobtrusive link rather than
 *  a badge — the card lists every OAuth provider, and a badge on each would
 *  drown the login status that is the card's actual job. */
export function proxyBadge(
  saved: ProviderProxyState | null | undefined,
): { kind: ProxyBadgeKind; host: string } {
  const state = normalizeProxyState(saved);
  if (state.invalid) return { kind: "invalid", host: "" };
  if (state.mode === "direct") return { kind: "direct", host: "" };
  if (state.mode === "url") {
    return { kind: "proxy", host: proxyDisplayHost(state.url) };
  }
  return { kind: "none", host: "" };
}

/** The mode the editor opens in, and the address it prefills. A redacted
 *  address is not prefilled: it is not a usable value, and leaving it in the
 *  box invites saving it back. */
export function initialProxyEditorValue(
  saved: ProviderProxyState | null | undefined,
): { mode: ProxyMode; url: string } {
  const state = normalizeProxyState(saved);
  const url =
    state.mode === "url" && state.url && !isRedactedProxyUrl(state.url)
      ? state.url
      : "";
  return { mode: state.mode, url };
}
