import { ComposerPrimitive } from "@assistant-ui/react";
import type { NativeSessionState } from "./native-types";
import type { NativeSession } from "./native-session";

interface NativeComposerProps {
  state: NativeSessionState;
  session: NativeSession;
}

export function NativeComposer({ state, session }: NativeComposerProps) {
  const disabled = !state.ready || state.conversation.running || Boolean(state.conversation.blocked);
  return <ComposerPrimitive.Root className="mx-auto mb-5 flex w-[calc(100%-2.5rem)] max-w-3xl items-end gap-3 rounded-2xl border border-current/20 p-3">
    <ComposerPrimitive.Input aria-label="Message Hermes" placeholder="Message Hermes…" rows={2} className="max-h-48 min-h-12 flex-1 resize-none bg-transparent p-2 outline-none disabled:opacity-50" disabled={disabled} submitMode="enter" addAttachmentOnPaste={false}
      onKeyDown={event => { if (event.key === "Enter" && event.nativeEvent.keyCode === 229) event.preventDefault(); }} />
    {state.conversation.running ? <button type="button" className="rounded-lg border px-4 py-2" disabled={!state.ready} onClick={() => void session.interrupt()}>Stop</button> : <ComposerPrimitive.Send className="rounded-lg border px-4 py-2 disabled:opacity-40" disabled={disabled}>Send</ComposerPrimitive.Send>}
  </ComposerPrimitive.Root>;
}
