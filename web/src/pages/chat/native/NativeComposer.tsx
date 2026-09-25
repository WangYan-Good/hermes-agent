import { safeExternalUrl } from "@hermes/chat-ui";
import { NativeReferences } from "./NativeReferences";
import { useRef, useState, useSyncExternalStore } from "react";
import { hasInteraction } from "./native-interactions";
import type { NativeSessionState } from "./native-types";
import type { NativeSession } from "./native-session";

interface NativeComposerProps { state: NativeSessionState; session: NativeSession; inputEnabled?: boolean }
export function NativeComposer({ state, session, inputEnabled = true }: NativeComposerProps) {
  const text = session.draftText;
  const setText = (value: string | ((old: string) => string)) => session.setDraft(typeof value === "function" ? value(session.draftText) : value);
  const [refKind, setRefKind] = useState("url"); const [refValue, setRefValue] = useState("");
  const [dropError, setDropError] = useState(""); const input = useRef<HTMLInputElement>(null);
  const attachments = useSyncExternalStore(session.attachments.subscribe, session.attachments.getSnapshot);
  const items = attachments.items.filter(a => !["submitted", "cancelled"].includes(a.state));
  const richBlocked = items.length > 0 && (state.conversation.running || items.some(a => a.state !== "uploaded"));
  const add = (files: File[]) => { if (inputEnabled && !session.inputFrozen && state.ready && !state.control.submitting && !attachments.uncertain) void session.attachments.add(files); };
  const addReference = () => {
    const value = refValue.trim(); if (!value || [...value].some(char => char.charCodeAt(0) < 32) || (refKind === "url" && !safeExternalUrl(value))) return;
    const quote = !value.includes('`') ? '`' : !value.includes('"') ? '"' : !value.includes("'") ? "'" : "";
    if (!quote) return;
    setText(t => `${t}${t ? " " : ""}@${refKind}:${quote}${value}${quote}`); setRefValue("");
  };
  const disabled = !inputEnabled || !state.ready || state.control.submitting || hasInteraction(state.interactions) || attachments.recovering || attachments.uncertain;
  const send = (mode: "send" | "queue" | "steer") => {
    if (disabled || richBlocked || (!text.trim() && !items.length) || (mode !== "send" && items.length)) return;
    setText("");
    void (mode === "steer" ? session.steer(text) : session.submit(text, mode === "queue"));
  };
  const button = "rounded-lg border px-3 py-2 text-sm disabled:opacity-40";
  return <form onDragOver={event => { if (event.dataTransfer.types.includes("Files")) event.preventDefault(); }} onDrop={event => {
    event.preventDefault();
    if ([...event.dataTransfer.items].some(item => item.webkitGetAsEntry?.()?.isDirectory)) { setDropError("Local directory upload is not supported. Add a backend folder reference instead."); return; }
    setDropError(""); add([...event.dataTransfer.files]);
  }} onPaste={event => {
    const images = [...event.clipboardData.files].filter(file => file.type.startsWith("image/"));
    if (images.length) { event.preventDefault(); add(images); const pasted = event.clipboardData.getData("text/plain"); if (pasted) setText(t => t + pasted); }
  }} onSubmit={event => { event.preventDefault(); send("send"); }} className="mx-auto mb-5 flex w-[calc(100%-2.5rem)] max-w-3xl flex-wrap items-end gap-3 rounded-2xl border border-current/20 p-3">
    <div className="w-full flex flex-wrap gap-2">
      <input ref={input} type="file" multiple className="sr-only" aria-label="Attach files or images" onChange={event => { add([...event.target.files || []]); event.target.value = ""; }} />
      <button className={button} type="button" disabled={disabled} onClick={() => input.current?.click()}>Attach</button>
      {items.map(item => <div key={item.occurrence_id} className="rounded border p-2 text-sm">
        {item.preview ? <img src={item.preview} alt={item.name} className="h-12 max-w-24 object-contain" /> : null}
        <span>{item.name}</span> <span role="status">{item.state}</span>{' '}
        {item.requiresReselection ? <span role="status">Choose the file again to replace this unfinished upload.</span> : item.state === "failed" ? <button type="button" disabled={disabled} aria-label={`Retry ${item.name}`} onClick={() => void session.attachments.retry(item.occurrence_id)}>Retry</button> : null}{' '}
        <button type="button" disabled={attachments.uncertain || state.control.submitting} aria-label={`Remove ${item.name}`} onClick={() => void session.attachments.remove(item.occurrence_id)}>Remove</button>
      </div>)}
      <details><summary>Add reference</summary><select aria-label="Reference kind" value={refKind} onChange={e => setRefKind(e.target.value)}><option value="url">URL</option><option value="file">Backend file</option><option value="folder">Backend folder</option></select><input aria-label="Reference value" value={refValue} onChange={e => setRefValue(e.target.value)} /><button type="button" onClick={addReference}>Add reference</button></details>
    </div>
    {attachments.error || dropError ? <p className="w-full text-sm" role="alert">{attachments.error || dropError}</p> : null}
    {items.length > 0 && state.conversation.running ? <p role="status" className="w-full text-sm">Attachments are saved as a draft. Send them when this turn finishes.</p> : null}
    <NativeReferences text={text} />
    <textarea aria-label="Message Hermes" placeholder={hasInteraction(state.interactions) ? "Respond in the interaction card above" : "Message Hermes…"} rows={2} className="max-h-48 min-h-12 min-w-40 flex-1 resize-none bg-transparent p-2 outline-none disabled:opacity-50" value={text} onChange={event => setText(event.target.value)} disabled={disabled} onKeyDown={event => { if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing && event.nativeEvent.keyCode !== 229) { event.preventDefault(); send("send"); } }} />
    <div className="flex flex-wrap gap-2"><button className={button} disabled={disabled || richBlocked || (!text.trim() && !items.length)}>Send</button>{state.conversation.running ? <><button className={button} type="button" disabled={disabled || !!items.length || !text.trim()} onClick={() => send("queue")}>Queue</button><button className={button} type="button" disabled={disabled || !!items.length || !text.trim()} onClick={() => send("steer")}>Steer</button><button className={button} type="button" disabled={!state.ready} onClick={() => void session.interrupt()}>Stop</button></> : null}</div>
  </form>;
}
