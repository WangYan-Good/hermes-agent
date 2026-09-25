import { useEffect, useRef, useState } from "react";
import { api, type McpCatalogEntry } from "@/lib/api";
import { completeMcpDashboardOAuth, McpOAuthCancelled } from "@/lib/mcp-dashboard-oauth";
import type { NativeInteraction } from "./native-interactions";
import type { NativeSession } from "./native-session";

interface Props { request: Extract<NativeInteraction, { kind: "mcp.setup" }>; session: NativeSession }
const button = "rounded-lg border border-current/25 px-3 py-2 text-sm disabled:opacity-40";
export function NativeMcpSetupCard({ request: r, session }: Props) {
  const [entry, setEntry] = useState<McpCatalogEntry | null>(null);
  const [loaded, setLoaded] = useState(r.action !== "install");
  const [working, setWorking] = useState(false);
  const [notice, setNotice] = useState("");
  const form = useRef<HTMLFormElement>(null);
  const operation = useRef<AbortController | null>(null);
  const decided = useRef(false);
  const pending = r.phase === "pending";
  useEffect(() => {
    let live = true;
    if (r.action === "install") void api.getMcpCatalog(session.profile).then(result => { if (live) { setEntry(result.entries.find(e => e.name === r.server) ?? null); setLoaded(true); } }).catch(() => { if (live) { setLoaded(true); setNotice("Could not load the MCP catalog."); } });
    const element = form.current;
    return () => { live = false; operation.current?.abort(); element?.reset(); };
  }, [r.action, r.server, session]);
  const finish = async (status: "installed" | "enabled" | "authorized" | "declined" | "error", detail?: string, tools?: string[]) => {
    if (decided.current || !session.isCurrent(r) || (status !== "declined" && operation.current?.signal.aborted)) return;
    if (["installed", "enabled", "authorized"].includes(status)) await session.reloadMcp(r);
    if (decided.current || !session.isCurrent(r) || (status !== "declined" && operation.current?.signal.aborted)) return;
    decided.current = true;
    await session.respondMcpSetup(r, { status, server: r.server, ...(detail ? { detail } : {}), ...(tools ? { tools } : {}) });
  };
  const cancel = () => {
    if (decided.current) return;
    operation.current?.abort(); form.current?.reset();
    void finish("declined", working ? "User cancelled setup. An already submitted installation may continue in the background." : undefined);
    setWorking(false);
  };
  const execute = async () => {
    if (working || !pending || !session.isCurrent(r) || decided.current) return;
    const abort = new AbortController(); operation.current = abort; setWorking(true);
    const check = () => { if (abort.signal.aborted || !session.isCurrent(r) || decided.current) throw new McpOAuthCancelled(); };
    try {
      if (r.action === "authorize") {
        // Do not put an await before this helper: it opens from the click.
        const flow = await completeMcpDashboardOAuth({ serverName: r.server, start: name => api.authMcpServer(name, session.profile), status: api.getMcpOAuthFlow, cancel: api.cancelMcpOAuthFlow, signal: abort.signal, open: (...args) => window.open(...args) });
        check(); await finish("authorized", undefined, flow.tools?.map(t => t.name)); return;
      }
      if (r.action === "enable") {
        const enabled = await api.setMcpServerEnabled(r.server, true, session.profile); check(); if (!enabled.ok) { await finish("error", "MCP enable failed."); return; } await finish("enabled"); return;
      }
      if (r.action !== "install" || !entry) { await finish("error", "No supported catalog entry or setup action is available."); return; }
      let action = session.mcpActions.get(r.requestId);
      if (!action) {
        const servers = await api.getMcpServers(session.profile); check();
        if (entry.installed && servers.servers.some(s => s.name === r.server)) { await finish("installed"); return; }
        let env: Record<string, string> = {};
        for (const field of entry.required_env) {
          const input = form.current?.elements.namedItem(field.name) as HTMLInputElement | null;
          if (input?.value) env[field.name] = input.value;
        }
        const install = api.installMcpCatalogEntry(r.server, env, true, session.profile);
        env = {}; form.current?.reset();
        const result = await install;
        // Keep only the non-sensitive action identifier for in-process recovery.
        if (result.action && session.getSnapshot().runtimeId === r.runtimeId) session.mcpActions.set(r.requestId, result.action);
        check();
        if (!result.ok || (result.background && !result.action)) { await finish("error", "Installation could not be confirmed."); return; }
        action = result.background ? result.action : undefined;
      }
      if (action) {
        const deadline = Date.now() + 600_000;
        for (;;) {
          check();
          const status = await api.getActionStatus(action, 0); check();
          if (!status.running) {
            if (status.exit_code !== 0) { await finish("error", "MCP installation failed."); return; }
            break;
          }
          if (Date.now() >= deadline) { await finish("error", "Installation is still running; check MCP settings for its final status."); return; }
          await new Promise(resolve => setTimeout(resolve, 1500));
        }
      }
      check(); await finish("installed");
    } catch (error) {
      if (!(error instanceof McpOAuthCancelled) && session.isCurrent(r) && !abort.signal.aborted) await finish("error", r.action === "authorize" ? "Authorization failed or the popup was blocked. Allow popups and check MCP settings." : "MCP setup failed. Check MCP settings before retrying.");
    } finally { form.current?.reset(); if (!abort.signal.aborted) setWorking(false); }
  };
  return <form ref={form} autoComplete="off" onSubmit={event => { event.preventDefault(); void execute(); }}>
    <h3 className="font-medium">MCP: {r.server} · {r.action}</h3><p>{r.reason}</p>
    {entry ? <><p className="my-2 text-sm">Source: {entry.source}</p>{entry.required_env.map(field => <label key={field.name} className="my-2 block">{field.prompt || field.name}{field.required ? " (required)" : ""}<input type="password" name={field.name} autoComplete="off" spellCheck={false} required={field.required && !session.mcpActions.has(r.requestId)} disabled={working || !pending} className="mt-1 block w-full rounded border bg-transparent p-2" /></label>)}</> : null}
    {notice ? <p role="status">{notice}</p> : null}
    {working ? <p role="status">Setup in progress. Cancel stops this interaction; an installation already submitted may continue.</p> : null}
    <div className="mt-3 flex gap-2"><button className={button} disabled={working || !loaded || !pending}>{working ? "Working…" : "Confirm setup"}</button><button type="button" className={button} disabled={!pending || decided.current} onClick={cancel}>{working ? "Cancel" : "Decline"}</button></div>
  </form>;
}
