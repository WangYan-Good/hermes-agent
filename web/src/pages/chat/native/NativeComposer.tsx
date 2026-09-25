import { useState } from "react";
import { hasInteraction } from "./native-interactions";
import type { NativeSessionState } from "./native-types";
import type { NativeSession } from "./native-session";

interface NativeComposerProps { state: NativeSessionState; session: NativeSession }
export function NativeComposer({ state, session }: NativeComposerProps) {
  const [text, setText] = useState("");
  const disabled = !state.ready || state.control.submitting || hasInteraction(state.interactions);
  const send = (mode: "send" | "queue" | "steer") => {
    if (disabled || !text.trim()) return;
    setText("");
    void (mode === "steer" ? session.steer(text) : session.submit(text, mode === "queue"));
  };
  const button = "rounded-lg border px-3 py-2 text-sm disabled:opacity-40";
  return <form onSubmit={event => { event.preventDefault(); send("send"); }} className="mx-auto mb-5 flex w-[calc(100%-2.5rem)] max-w-3xl flex-wrap items-end gap-3 rounded-2xl border border-current/20 p-3">
    <textarea aria-label="Message Hermes" placeholder={hasInteraction(state.interactions) ? "Respond in the interaction card above" : "Message Hermes…"} rows={2} className="max-h-48 min-h-12 min-w-40 flex-1 resize-none bg-transparent p-2 outline-none disabled:opacity-50" value={text} onChange={event => setText(event.target.value)} disabled={disabled} onKeyDown={event => { if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing && event.nativeEvent.keyCode !== 229) { event.preventDefault(); send("send"); } }} />
    <div className="flex flex-wrap gap-2"><button className={button} disabled={disabled || !text.trim()}>Send</button>{state.conversation.running ? <><button className={button} type="button" disabled={disabled || !text.trim()} onClick={() => send("queue")}>Queue</button><button className={button} type="button" disabled={disabled || !text.trim()} onClick={() => send("steer")}>Steer</button><button className={button} type="button" disabled={!state.ready} onClick={() => void session.interrupt()}>Stop</button></> : null}</div>
  </form>;
}
