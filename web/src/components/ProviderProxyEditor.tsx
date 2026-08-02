import { useState } from "react";
import { AlertTriangle, Check, Network, X } from "lucide-react";

import { Badge } from "@nous-research/ui/ui/components/badge";
import { Button } from "@nous-research/ui/ui/components/button";
import { Input } from "@nous-research/ui/ui/components/input";
import { Select, SelectOption } from "@nous-research/ui/ui/components/select";
import { Spinner } from "@nous-research/ui/ui/components/spinner";

import { api, type OAuthProvider, type ProviderProxyTestResult } from "@/lib/api";
import {
  initialProxyEditorValue,
  isProxyDirty,
  proxyBadge,
  proxySubmitError,
  toProxyPayload,
  type ProxyMode,
} from "@/lib/provider-proxy";
import { useI18n } from "@/i18n";

interface Props {
  provider: OAuthProvider;
  /** Refetch the card so the badge reflects what was just saved. */
  onSaved: () => void;
  onError?: (msg: string) => void;
  onSuccess?: (msg: string) => void;
}

/** The per-provider proxy control on one OAuth row.
 *
 *  Collapsed by default: the card lists every OAuth provider, and a
 *  permanently-expanded control on each row would drown the login status that
 *  is the card's actual job. */
export function ProviderProxyEditor({
  provider,
  onSaved,
  onError,
  onSuccess,
}: Props) {
  const { t } = useI18n();
  const saved = provider.proxy;
  const initial = initialProxyEditorValue(saved);

  const [open, setOpen] = useState(false);
  const [mode, setMode] = useState<ProxyMode>(initial.mode);
  const [url, setUrl] = useState(initial.url);
  const [busy, setBusy] = useState<"save" | "test" | null>(null);
  const [result, setResult] = useState<ProviderProxyTestResult | null>(null);
  const [localError, setLocalError] = useState<string | null>(null);

  const copy = t.oauth.proxy;

  // A row with no editable config key — the synthetic claude-code
  // subscription entry — gets no editor at all. The backend says so by
  // sending null; an older backend omits the field entirely.
  if (saved === null || saved === undefined) return null;

  const badge = proxyBadge(saved);
  const submitError = proxySubmitError(mode, url);
  const dirty = isProxyDirty(saved, mode, url);

  const openEditor = () => {
    const next = initialProxyEditorValue(saved);
    setMode(next.mode);
    setUrl(next.url);
    setResult(null);
    setLocalError(null);
    setOpen(true);
  };

  const handleTest = async () => {
    if (submitError) {
      setLocalError(copy[submitError]);
      return;
    }
    setBusy("test");
    setLocalError(null);
    setResult(null);
    try {
      // Tests the pending value, not the saved one — testing after saving
      // inverts the point.
      setResult(await api.testProviderProxy(provider.id, toProxyPayload(mode, url)));
    } catch (e) {
      setLocalError(String(e));
    } finally {
      setBusy(null);
    }
  };

  const handleSave = async () => {
    if (submitError) {
      setLocalError(copy[submitError]);
      return;
    }
    setBusy("save");
    setLocalError(null);
    try {
      await api.setProviderProxy(provider.id, toProxyPayload(mode, url));
      onSuccess?.(copy.saved.replace("{provider}", provider.name));
      setOpen(false);
      setResult(null);
      onSaved();
    } catch (e) {
      onError?.(`${copy.label}: ${e}`);
    } finally {
      setBusy(null);
    }
  };

  if (!open) {
    // One affordance, not two: a configured provider's badge *is* the button.
    return (
      <button
        type="button"
        onClick={openEditor}
        aria-label={`${copy.label} — ${provider.name}`}
        className="inline-flex w-fit items-center gap-1 text-xs text-text-tertiary hover:text-foreground underline-offset-2 hover:underline"
      >
        {badge.kind === "proxy" && (
          <Badge tone="outline" className="text-xs">
            {copy.badgeProxy.replace("{host}", badge.host)}
          </Badge>
        )}
        {badge.kind === "direct" && (
          <Badge tone="outline" className="text-xs">
            {copy.badgeDirect}
          </Badge>
        )}
        {badge.kind === "invalid" && (
          <Badge tone="destructive" className="text-xs">
            {copy.badgeInvalid}
          </Badge>
        )}
        {badge.kind === "none" && (
          <>
            <Network className="h-3 w-3" />
            {copy.configure}
          </>
        )}
      </button>
    );
  }

  return (
    <div className="flex flex-col gap-2 rounded-md border border-border p-2">
      <div className="flex flex-wrap items-center gap-2">
        <Select
          className="min-w-0"
          value={mode}
          disabled={busy !== null}
          onValueChange={(next: string) => {
            setMode(next as ProxyMode);
            setResult(null);
            setLocalError(null);
          }}
        >
          <SelectOption value="inherit">{copy.modeInherit}</SelectOption>
          <SelectOption value="direct">{copy.modeDirect}</SelectOption>
          <SelectOption value="url">{copy.modeUrl}</SelectOption>
        </Select>

        {mode === "url" && (
          <Input
            className="min-w-0 flex-1 font-mono-ui text-xs"
            value={url}
            disabled={busy !== null}
            placeholder={copy.urlPlaceholder}
            onChange={(e) => {
              setUrl(e.target.value);
              setResult(null);
              setLocalError(null);
            }}
          />
        )}

        <Button
          size="sm"
          outlined
          onClick={() => void handleTest()}
          disabled={busy !== null}
          prefix={busy === "test" ? <Spinner /> : undefined}
        >
          {busy === "test" ? copy.testing : copy.test}
        </Button>
        <Button
          size="sm"
          onClick={() => void handleSave()}
          disabled={busy !== null || !dirty}
          prefix={busy === "save" ? <Spinner /> : undefined}
        >
          {busy === "save" ? copy.saving : copy.save}
        </Button>
        <Button size="sm" ghost onClick={() => setOpen(false)} disabled={busy !== null}>
          {t.common.cancel}
        </Button>
      </div>

      {saved.invalid && (
        <span className="text-xs text-destructive">{copy.invalidHint}</span>
      )}
      {localError && <span className="text-xs text-destructive">{localError}</span>}
      {result && <ProxyTestResultLine result={result} />}
    </div>
  );
}

function ProxyTestResultLine({ result }: { result: ProviderProxyTestResult }) {
  const { t } = useI18n();
  const copy = t.oauth.proxy;
  const status = String(result.status ?? "");

  if (result.kind === "transport_error") {
    return (
      <span className="flex items-start gap-1 text-xs text-destructive">
        <X className="h-3 w-3 shrink-0 mt-0.5" />
        {copy.resultTransportError.replace("{detail}", result.detail)}
      </span>
    );
  }
  if (result.kind === "reachable") {
    return (
      <span className="flex items-start gap-1 text-xs text-success">
        <Check className="h-3 w-3 shrink-0 mt-0.5" />
        {copy.resultReachable.replace("{status}", status)}
      </span>
    );
  }
  // Answered, but the operator judges the code: api.anthropic.com replies 403
  // on a direct connection from a blocked region, and that is not success.
  return (
    <span className="flex items-start gap-1 text-xs text-text-secondary">
      <AlertTriangle className="h-3 w-3 shrink-0 mt-0.5" />
      {copy.resultHttp.replace("{status}", status)}
    </span>
  );
}
