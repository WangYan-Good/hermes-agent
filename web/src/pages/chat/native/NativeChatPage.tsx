import { NativeInteractions } from "./NativeInteractions";
import { NativeActivity } from "./NativeActivity";
import { useEffect, useMemo, useRef, useState } from "react";
import { useLocation, useSearchParams } from "react-router";
import { ThreadPrimitive } from "@assistant-ui/react";
import { useProfileScope } from "@/contexts/useProfileScope";
import type { ChatPageProps } from "../../ChatPage";
import { NativeChatRuntime } from "./NativeChatRuntime";
import { NativeComposer } from "./NativeComposer";
import { NativeThread } from "./NativeThread";
import { useNativeGateway } from "./use-native-gateway";
import { ChatHostContext } from '@hermes/chat-ui';
import { createNativeHost } from './native-host';

function NativeSurface({ profile, initialResume, isActive = true, inputEnabled = true, registerLifecycle }: ChatPageProps & { profile: string; initialResume: string | null }) {
  const host = useMemo(() => createNativeHost(profile), [profile]);
  const [params, setParams] = useSearchParams();
  const location = useLocation();
  const { session, state } = useNativeGateway(profile, initialResume);
  useEffect(() => registerLifecycle?.(session.lifecycle), [registerLifecycle, session]);
  const lastResume = useRef(params.get("resume"));
  const resume = params.get("resume");
  useEffect(() => {
    if (!isActive || !inputEnabled) return;
    // Query removal on route-away/back is visibility, not a new-session command.
    if (resume && resume !== lastResume.current && resume !== state.storedId) {
      lastResume.current = resume;
      session.select(resume);
      return;
    }
    lastResume.current = resume;
    if (!state.ready) return;
    const wanted = state.durable ? state.storedId : null;
    if (resume === wanted) return;
    setParams(prev => {
      const next = new URLSearchParams(prev);
      if (wanted) next.set("resume", wanted); else next.delete("resume");
      return next;
    }, { replace: true });
  }, [isActive, inputEnabled, location.pathname, resume, params, setParams, session, state.ready, state.durable, state.storedId]);

  const newSession = () => {
    session.select(null);
    lastResume.current = null;
    setParams(prev => { const next = new URLSearchParams(prev); next.delete("resume"); return next; }, { replace: true });
  };
  return <ChatHostContext.Provider value={host}><NativeChatRuntime state={state} session={session}>
    <ThreadPrimitive.Root className="flex h-full min-h-0 flex-col text-foreground" aria-label="Native Chat">
      <header className="flex items-center justify-between border-b border-current/10 px-5 py-3"><div><span className="font-medium">Hermes</span><span className="ml-3 text-xs opacity-60">Native</span></div><button type="button" disabled={!inputEnabled || state.conversation.running || state.connection === "connecting"} onClick={newSession} className="text-sm disabled:opacity-40">New session</button></header>
      {state.connection !== "open" || !state.ready ? <div role="status" className="px-5 py-2 text-sm">{state.connection === "connecting" ? "Connecting…" : "Connection needs attention"}</div> : null}
      {state.conversation.error ? <div role="alert" className="mx-5 my-2 rounded-lg border border-red-400/40 p-3 text-sm">{state.conversation.error}<button type="button" className="ml-3 underline" onClick={session.retry}>Reconnect</button></div> : null}
      {state.conversation.status ? <div role="status" className="px-5 py-1 text-sm opacity-60">{state.conversation.status}</div> : null}
      {state.durable ? <button type="button" className="text-sm underline" onClick={() => void session.loadOlder()}>Load earlier messages</button> : null}
      <NativeThread />
      <NativeInteractions state={state} session={session} visible={isActive} />
      <NativeActivity control={state.control} />
      <NativeComposer state={state} session={session} inputEnabled={inputEnabled} />
    </ThreadPrimitive.Root>
  </NativeChatRuntime></ChatHostContext.Provider>;
}

export default function NativeChatPage(props: ChatPageProps) {
  const { profile } = useProfileScope();
  const [params] = useSearchParams();
  const [scope, setScope] = useState({ profile, resume: props.handoffResume !== undefined ? props.handoffResume : params.get("resume") });
  if (scope.profile !== profile) setScope({ profile, resume: null });
  return <NativeSurface key={profile} profile={profile} initialResume={scope.profile === profile ? scope.resume : null} {...props} />;
}
