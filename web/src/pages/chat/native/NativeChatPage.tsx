import { useEffect, useRef, useState } from "react";
import { useLocation, useSearchParams } from "react-router";
import { ThreadPrimitive } from "@assistant-ui/react";
import { useProfileScope } from "@/contexts/useProfileScope";
import { HERMES_BASE_PATH } from "@/lib/api";
import type { ChatPageProps } from "../../ChatPage";
import { NativeChatRuntime } from "./NativeChatRuntime";
import { NativeComposer } from "./NativeComposer";
import { NativeThread } from "./NativeThread";
import { useNativeGateway } from "./use-native-gateway";

function NativeSurface({ profile, initialResume, isActive = true }: ChatPageProps & { profile: string; initialResume: string | null }) {
  const [params, setParams] = useSearchParams();
  const location = useLocation();
  const { session, state } = useNativeGateway(profile, initialResume);
  const lastResume = useRef(params.get("resume"));
  const resume = params.get("resume");
  useEffect(() => {
    if (!isActive) return;
    // Query removal on route-away/back is visibility, not a new-session command.
    if (resume && resume !== lastResume.current && resume !== state.storedId) {
      lastResume.current = resume;
      session.select(resume);
      return;
    }
    lastResume.current = resume;
    if (!state.ready) return;
    const wanted = state.durable ? state.storedId : null;
    if (params.get("chat_mode") === "native" && resume === wanted) return;
    setParams(prev => {
      const next = new URLSearchParams(prev);
      next.set("chat_mode", "native");
      if (wanted) next.set("resume", wanted); else next.delete("resume");
      return next;
    }, { replace: true });
  }, [isActive, location.pathname, resume, params, setParams, session, state.ready, state.durable, state.storedId]);

  const terminalParams = new URLSearchParams({ chat_mode: "terminal" });
  if (state.durable && state.storedId) terminalParams.set("resume", state.storedId);
  if (profile) terminalParams.set("profile", profile);
  const newSession = () => {
    session.select(null);
    lastResume.current = null;
    setParams(prev => { const next = new URLSearchParams(prev); next.delete("resume"); next.set("chat_mode", "native"); return next; }, { replace: true });
  };
  return <NativeChatRuntime state={state} session={session}>
    <ThreadPrimitive.Root className="flex h-full min-h-0 flex-col" aria-label="Native Chat">
      <header className="flex items-center justify-between border-b border-current/10 px-5 py-3"><div><span className="font-medium">Hermes</span><span className="ml-3 text-xs opacity-60">Native · Experimental</span></div><button type="button" disabled={state.conversation.running || !state.ready} onClick={newSession} className="text-sm disabled:opacity-40">New session</button></header>
      {state.connection !== "open" || !state.ready ? <div role="status" className="px-5 py-2 text-sm">{state.connection === "connecting" ? "Connecting…" : "Connection needs attention"}</div> : null}
      {state.conversation.error ? <div role="alert" className="mx-5 my-2 rounded-lg border border-red-400/40 p-3 text-sm">{state.conversation.error}<button type="button" className="ml-3 underline" onClick={session.retry}>Reconnect</button></div> : null}
      {state.conversation.blocked ? <div role="alert" className="mx-5 my-2 rounded-lg border border-amber-400/40 p-3 text-sm">This interaction is not supported in Native MVP ({state.conversation.blocked}). Stop this turn, then open Terminal.</div> : null}
      {state.conversation.status ? <div role="status" className="px-5 py-1 text-sm opacity-60">{state.conversation.status}</div> : null}
      <NativeThread />
      <NativeComposer state={state} session={session} />
      {!state.conversation.running && state.ready ? <a className="mb-3 text-center text-xs opacity-60 underline" href={`${HERMES_BASE_PATH}/chat?${terminalParams}`}>Open Terminal (reload page)</a> : null}
    </ThreadPrimitive.Root>
  </NativeChatRuntime>;
}

export default function NativeChatPage(props: ChatPageProps) {
  const { profile } = useProfileScope();
  const [params] = useSearchParams();
  const [scope, setScope] = useState({ profile, resume: params.get("resume") });
  if (scope.profile !== profile) setScope({ profile, resume: null });
  return <NativeSurface key={profile} profile={profile} initialResume={scope.profile === profile ? scope.resume : null} {...props} />;
}
