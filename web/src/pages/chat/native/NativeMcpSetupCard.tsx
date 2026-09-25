import { useEffect, useRef, useState } from "react";
import { api, type McpCatalogEntry } from "@/lib/api";
import { completeMcpDashboardOAuth, McpOAuthCancelled } from "@/lib/mcp-dashboard-oauth";
import { setupBinding, settleMcp, waitForMcpOperation, type McpRequest, type McpOutcome } from "./native-mcp-operation";
import type { NativeSession } from "./native-session";

interface Props { request: McpRequest; session: NativeSession }
const button = "rounded-lg border border-current/25 px-3 py-2 text-sm disabled:opacity-40";
export function NativeMcpSetupCard({ request: r, session }: Props) {
  const [entry, setEntry] = useState<McpCatalogEntry | null>(null);
  const [loaded, setLoaded] = useState(r.action !== "install");
  const [working, setWorking] = useState(Boolean(r.operation));
  const [uncertain, setUncertain] = useState(false);
  const [notice, setNotice] = useState(r.operation?.kind === "authorize" ? "Recovering the existing authorization. Complete the original authorization window, or cancel this flow. No new flow will start." : "");
  const form = useRef<HTMLFormElement>(null);
  const operation = useRef<AbortController | null>(null);
  const initialRequest = useRef(r);
  const decided = useRef(false);
  const cancelRequested = useRef(false);
  const flowId = useRef(r.operation?.kind === "authorize" ? r.operation.id : undefined);
  const cancelSent = useRef(false);
  const pending = r.phase === "pending";
  useEffect(() => {
    let live = true;
    if (r.action === "install" && !initialRequest.current.operation) void api.getMcpCatalog(session.profile).then(result => { if (live) { setEntry(result.entries.find(e => e.name === r.server) ?? null); setLoaded(true); } }).catch(() => { if (live) { setLoaded(true); setNotice("Could not load the MCP catalog."); } });
    const element = form.current;
    return () => { live = false; operation.current?.abort(); element?.reset(); };
  }, [r.action, r.server, session]);
  useEffect(() => {
    const request = initialRequest.current;
    if (!request.operation) return;
    const abort = new AbortController(); operation.current = abort;
    void waitForMcpOperation(session, request, request.operation, abort.signal)
      .then(outcome => settleMcp(session, request, decided, abort.signal, outcome))
      .catch(() => { if (!abort.signal.aborted && session.isCurrent(request)) setNotice("Could not confirm the existing operation. Recover the request; setup will not be started again."); })
      .finally(() => { if (!abort.signal.aborted) setWorking(false); });
    return () => abort.abort();
  }, [session]);
  const finish = (outcome: McpOutcome) => settleMcp(session, r, decided, operation.current?.signal, outcome);
  const cancelFlow = async () => {
    if (flowId.current && !cancelSent.current) { cancelSent.current = true; await api.cancelMcpOAuthFlow(flowId.current); }
  };
  const cancel = async () => {
    if (decided.current || cancelRequested.current) return;
    cancelRequested.current = true; operation.current?.abort(); form.current?.reset();
    try { await cancelFlow(); }
    catch { if (session.isCurrent(r)) setNotice("Could not confirm OAuth cancellation. Check MCP settings; no new flow will start."); }
    await finish({ status: "declined", ...(working || r.operation ? { detail: "User cancelled setup. An already submitted installation may continue in the background." } : {}) });
    setWorking(false);
  };
  const execute = async () => {
    if (working || uncertain || r.operation || (operation.current && !operation.current.signal.aborted) || !pending || !session.isCurrent(r) || decided.current) return;
    const abort = new AbortController(); operation.current = abort; setWorking(true);
    const check = () => { if (abort.signal.aborted || !session.isCurrent(r) || decided.current) throw new McpOAuthCancelled(); };
    try {
      if (r.action === "authorize") {
        const flow = await completeMcpDashboardOAuth({ serverName: r.server, start: async name => {
          const started = await api.authMcpServer(name, session.profile, setupBinding(r));
          flowId.current = started.flow_id;
          session.rememberMcpOperation(r, started.operation);
          if (cancelRequested.current) await cancelFlow();
          return started;
        }, status: api.getMcpOAuthFlow, cancel: api.cancelMcpOAuthFlow, signal: abort.signal, cancelOnAbort: false, open: (...args) => window.open(...args) });
        check(); await finish({ status: "authorized", ...(flow.tools ? { tools: flow.tools.map(t => t.name) } : {}) }); return;
      }
      if (r.action === "enable") {
        const enabled = await api.setMcpServerEnabled(r.server, true, session.profile); check();
        await finish(enabled.ok ? { status: "enabled" } : { status: "error", detail: "MCP enable failed." }); return;
      }
      if (r.action !== "install" || !entry) { await finish({ status: "error", detail: "No supported catalog entry or setup action is available." }); return; }
      const servers = await api.getMcpServers(session.profile); check();
      if (entry.installed && servers.servers.some(s => s.name === r.server)) { await finish({ status: "installed" }); return; }
      let env: Record<string, string> = {};
      for (const field of entry.required_env) {
        const input = form.current?.elements.namedItem(field.name) as HTMLInputElement | null;
        if (input?.value) env[field.name] = input.value;
      }
      const install = api.installMcpCatalogEntry(r.server, env, true, session.profile, setupBinding(r));
      env = {}; form.current?.reset();
      const result = await install; check();
      session.rememberMcpOperation(r, result.operation);
      if (!result.ok || (result.background && !result.action)) { await finish({ status: "error", detail: "Installation could not be confirmed." }); return; }
      if (!result.operation || result.operation.kind !== "install" || result.operation.id !== result.action) throw new Error("Backend operation identity was not confirmed");
      await finish(await waitForMcpOperation(session, r, result.operation, abort.signal));
    } catch (error) {
      if (!(error instanceof McpOAuthCancelled) && session.isCurrent(r) && !abort.signal.aborted) {
        // An HTTP reply may have been lost after backend acceptance. Read the
        // registry before settling; never retry an install or auth POST.
        try {
          const recovered = await api.getMcpSetupOperation(setupBinding(r), session.profile); check();
          if (recovered.operation) {
            session.rememberMcpOperation(r, recovered.operation);
            await finish(await waitForMcpOperation(session, r, recovered.operation, abort.signal)); return;
          }
          await finish({ status: "error", detail: r.action === "authorize" ? "Authorization failed or the popup was blocked. Allow popups and check MCP settings." : "MCP setup failed. Check MCP settings before retrying." });
        } catch { if (!abort.signal.aborted && session.isCurrent(r)) { setUncertain(true); setNotice("Setup could not be confirmed. Recover the request before trying again; no operation will be replayed."); } }
      }
    } finally { form.current?.reset(); if (!abort.signal.aborted) setWorking(false); }
  };
  return <form ref={form} autoComplete="off" onSubmit={event => { event.preventDefault(); void execute(); }}>
    <h3 className="font-medium">MCP: {r.server} · {r.action}</h3><p>{r.reason}</p>
    {entry && !r.operation ? <><p className="my-2 text-sm">Source: {entry.source}</p>{entry.required_env.map(field => <label key={field.name} className="my-2 block">{field.prompt || field.name}{field.required ? " (required)" : ""}<input type="password" name={field.name} autoComplete="off" spellCheck={false} required={field.required} disabled={working || !pending} className="mt-1 block w-full rounded border bg-transparent p-2" /></label>)}</> : null}
    {notice ? <p role="status">{notice}</p> : null}
    {working ? <p role="status">{r.operation ? "Checking the existing operation. " : "Setup in progress. "}Cancel stops this interaction; an installation already submitted may continue.</p> : null}
    <div className="mt-3 flex gap-2">{!r.operation && !uncertain ? <button className={button} disabled={working || !loaded || !pending}>{working ? "Working…" : "Confirm setup"}</button> : <button type="button" className={button} disabled={working || !pending} onClick={session.retry}>Recover request</button>}<button type="button" className={button} disabled={!pending || decided.current || cancelRequested.current} onClick={() => void cancel()}>{working || r.operation ? "Cancel" : "Decline"}</button></div>
  </form>;
}
