import { NativeInteractions } from "./NativeInteractions";
import { NativeActivity } from "./NativeActivity";
import { useMemo, useState } from "react";
import { useSearchParams } from "react-router";
import { ThreadPrimitive } from "@assistant-ui/react";
import { useProfileScope } from "@/contexts/useProfileScope";
import type { ChatPageProps } from "../../ChatPage";
import { NativeChatRuntime } from "./NativeChatRuntime";
import { NativeComposer } from "./NativeComposer";
import { NativeThread } from "./NativeThread";
import { useNativeGateway } from "./use-native-gateway";
import { ChatHostContext } from '@hermes/chat-ui';
import { useNativeRoute } from "./use-native-route";
import { createNativeHost } from './native-host';

function NativeSurface({ profile, initialResume, initialLearn, isActive = true }: ChatPageProps & { profile: string; initialResume: string | null; initialLearn: string | null }) {
  const host = useMemo(() => createNativeHost(profile), [profile]);
  const { session, state } = useNativeGateway(profile, initialResume);
  const { newSession, pendingLearn, acceptLearn } = useNativeRoute(session, state, isActive, initialLearn);
  return <ChatHostContext.Provider value={host}><NativeChatRuntime state={state} session={session}>
    <ThreadPrimitive.Root className="flex h-full min-h-0 flex-col text-foreground" aria-label="Native Chat">
      <header className="flex items-center justify-between border-b border-current/10 px-5 py-3"><div><span className="font-medium">Hermes</span><span className="ml-3 text-xs opacity-60">Native</span></div><button type="button" disabled={state.conversation.running || state.connection === "connecting"} onClick={newSession} className="text-sm disabled:opacity-40">New session</button></header>
      {state.connection !== "open" || !state.ready ? <div role="status" className="px-5 py-2 text-sm">{state.connection === "connecting" ? "Connecting…" : "Connection needs attention"}</div> : null}
      {state.conversation.error ? <div role="alert" className="mx-5 my-2 rounded-lg border border-red-400/40 p-3 text-sm">{state.conversation.error}<button type="button" className="ml-3 underline" onClick={session.retry}>Reconnect</button></div> : null}
      {state.conversation.status ? <div role="status" className="px-5 py-1 text-sm opacity-60">{state.conversation.status}</div> : null}
      {state.durable ? <button type="button" className="text-sm underline" onClick={() => void session.loadOlder()}>Load earlier messages</button> : null}
      <NativeThread />
      <NativeInteractions state={state} session={session} visible={isActive} />
      <NativeActivity control={state.control} />
      {pendingLearn ? <div role="status" className="px-5 py-2 text-sm">A learning request is waiting. Your existing draft is unchanged.
        <button type="button" onClick={() => acceptLearn(true)}>Append to draft</button>
        <button type="button" onClick={() => acceptLearn(false)}>Ignore</button>
      </div> : null}
      <NativeComposer state={state} session={session} />
    </ThreadPrimitive.Root>
  </NativeChatRuntime></ChatHostContext.Provider>;
}

export default function NativeChatPage(props: ChatPageProps) {
  const { profile } = useProfileScope();
  const [params] = useSearchParams();
  const [scope, setScope] = useState({ profile, resume: params.get("resume"), learn: params.get("learn") });
  if (scope.profile !== profile) setScope({ profile, resume: null, learn: null });
  return <NativeSurface key={profile} profile={profile} initialResume={scope.profile === profile ? scope.resume : null} initialLearn={scope.profile === profile ? scope.learn : null} {...props} />;
}
