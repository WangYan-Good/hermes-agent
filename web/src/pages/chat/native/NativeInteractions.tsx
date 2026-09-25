import { useEffect, useRef, useState } from "react";
import type { NativeInteraction } from "./native-interactions";
import type { NativeSession } from "./native-session";
import type { NativeSessionState } from "./native-types";
import { NativeMcpSetupCard } from "./NativeMcpSetupCard";

const button = "rounded-lg border border-current/25 px-3 py-2 text-sm disabled:opacity-40";
interface CardProps { request: NativeInteraction; session: NativeSession }
function Approval({ request: r, session }: CardProps) {
  const [confirm, setConfirm] = useState(false);
  if (r.kind !== "approval") return null;
  const labels = { once: "Allow once", session: "Allow this session", always: "Always allow", deny: "Deny" };
  return <><h3 className="font-medium">Approval required</h3><p>{r.description}</p><pre className="my-2 max-h-40 overflow-auto whitespace-pre-wrap break-words text-sm">{r.command}</pre>
    <div className="flex flex-wrap gap-2">{r.choices.map(choice => <button className={button} type="button" key={choice} disabled={r.phase !== "pending"} onClick={() => choice === "always" ? setConfirm(true) : void session.respondApproval(r, choice)}>{labels[choice]}</button>)}</div>
    {confirm && r.phase === "pending" ? <div role="alertdialog" aria-label="Confirm permanent approval" className="mt-3 rounded-lg border p-3"><p>This permanently changes approval policy for this command. Allow it in future sessions?</p><div className="mt-2 flex gap-2"><button className={button} type="button" onClick={() => { setConfirm(false); void session.respondApproval(r, "always"); }}>Confirm always allow</button><button className={button} type="button" onClick={() => setConfirm(false)}>Back</button></div></div> : null}
    {!r.choices.length ? <p>No supported choices were offered. Stop the turn or reconnect to recover.</p> : null}</>;
}
function Clarify({ request: r, session }: CardProps) {
  const [selected, setSelected] = useState<string[]>([]);
  const [text, setText] = useState("");
  if (r.kind !== "clarify") return null;
  return <form onSubmit={event => { event.preventDefault(); const answer = text.trim() ? text : r.multiSelect ? JSON.stringify(selected) : selected[0] || ""; void session.respondClarify(r, answer); }}>
    <h3 className="font-medium">{r.question}</h3><fieldset disabled={r.phase !== "pending"} className="my-3 space-y-2"><legend className="sr-only">Choose an answer</legend>{r.choices.map(choice => <label className="flex items-center gap-2" key={choice}><input type={r.multiSelect ? "checkbox" : "radio"} name={r.key} checked={selected.includes(choice)} onChange={() => setSelected(r.multiSelect ? selected.includes(choice) ? selected.filter(s => s !== choice) : [...selected, choice] : [choice])} />{choice}</label>)}
    <label className="block">Your answer<textarea className="mt-1 w-full rounded border bg-transparent p-2" value={text} onChange={event => setText(event.target.value)} /></label></fieldset>
    <div className="flex gap-2"><button className={button} disabled={r.phase !== "pending" || (!text.trim() && !selected.length)}>Answer</button><button className={button} type="button" disabled={r.phase !== "pending"} onClick={() => void session.respondClarify(r, "")}>Skip</button></div>
  </form>;
}
function Credential({ request: r, session }: CardProps) {
  const field = useRef<HTMLInputElement>(null);
  useEffect(() => { const input = field.current; return () => { if (input) input.value = ""; }; }, []);
  if (r.kind !== "secret" && r.kind !== "sudo") return null;
  // Uncontrolled masked field: the value never enters any React/store state.
  // This form is unmounted on disconnect, hide, expiry, submit, or navigation.
  const submit = (value: string) => r.kind === "secret" ? session.respondSecret(r, value) : session.respondSudo(r, value);
  return <form autoComplete="off" onSubmit={event => { event.preventDefault(); const form = event.currentTarget; const input = form.elements.namedItem("credential") as HTMLInputElement; let value = input.value; input.value = ""; void submit(value); value = ""; }}>
    <h3 className="font-medium">{r.kind === "sudo" ? "Sudo password" : r.envVar}</h3>{r.kind === "secret" ? <p>{r.prompt}</p> : null}
    <input ref={field} aria-label={r.kind === "sudo" ? "Sudo password" : "Secret value"} name="credential" type="password" autoComplete="off" spellCheck={false} required className="my-3 w-full rounded border bg-transparent p-2" />
    <div className="flex gap-2"><button className={button}>Submit securely</button><button className={button} type="button" onClick={event => { event.currentTarget.form?.reset(); void submit(""); }}>Cancel</button></div>
  </form>;
}
export function NativeInteractions({ state, session, visible = true }: { state: NativeSessionState; session: NativeSession; visible?: boolean }) {
  if (!visible) return null;
  return <div className="max-h-[45vh] shrink-0 overflow-y-auto px-5" aria-label="Agent interactions">{Object.values(state.interactions).map(r => <section key={`${r.generation}:${r.runtimeId}:${r.key}`} className="mx-auto my-3 max-w-3xl rounded-xl border border-current/20 p-4" data-request-id={r.requestId}>
    {r.phase === "resolved" || r.phase === "expired" ? <p role="status">{r.kind}: {r.phase === "expired" ? "Request expired" : "Response received"}</p> : !state.ready ? <p role="status">Reconnecting to recover {r.kind} request…</p> : r.kind === "approval" ? <Approval request={r} session={session} /> : r.kind === "clarify" ? <Clarify request={r} session={session} /> : r.kind === "mcp.setup" ? <NativeMcpSetupCard request={r} session={session} /> : r.phase === "pending" ? <Credential request={r} session={session} /> : <p role="status">Sending secure response…</p>}
    {r.error ? <div role="alert" className="mt-2 text-sm">{r.error}<button type="button" className="ml-2 underline" onClick={session.retry}>Recover request</button></div> : null}
  </section>)}</div>;
}
