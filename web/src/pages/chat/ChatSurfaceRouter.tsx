import { ChatSurfaceBoundary } from './ChatSurfaceBoundary';
import { lazy, Suspense, useCallback, useEffect, useRef, useState, useSyncExternalStore } from 'react';
import { useLocation, useNavigate } from 'react-router';
import { useStore } from '@nanostores/react';
import { useProfileScope } from '@/contexts/useProfileScope';
import { ChatSwitch } from './chat-switch';
import { normalizeChatMode, resolveChatMode, type ChatMode } from './chat-mode';
import { $browserMode, $chatHost, $preferenceNotice, $profileModes, loadProfileMode, requestBrowserMode } from './chat-preferences';
import type { ChatSurfaceLifecycle } from './chat-surface-lifecycle';
import type { ChatPageProps } from '../ChatPage';

const Native = lazy(() => import('./native/NativeChatPage'));
const Terminal = lazy(() => import('./TerminalChatPage'));

function SurfaceHost({ initialMode, profile, initialResume, initialLearn, isActive = true }: ChatPageProps & { initialMode: ChatMode; profile: string; initialResume: string | null; initialLearn: string | null }) {
  const location = useLocation(), navigate = useNavigate();
  const route = useRef({ location, isActive });
  useEffect(() => { route.current = { location, isActive }; }, [location, isActive]);
  const cleanupTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const queryChanges = useRef(new Map<string, string[]>());
  // Apply only each hook's changed keys, batching Native session and mode
  // housekeeping so neither can resurrect the other's consumed query intent.
  const updateQuery = useCallback((before: string, after: URLSearchParams) => {
    const previous = new URLSearchParams(before);
    for (const key of new Set([...previous.keys(), ...after.keys()])) {
      if (JSON.stringify(previous.getAll(key)) !== JSON.stringify(after.getAll(key))) queryChanges.current.set(key, after.getAll(key));
    }
    clearTimeout(cleanupTimer.current);
    cleanupTimer.current = setTimeout(() => {
      const { location: current, isActive: visible } = route.current;
      if (!visible) { queryChanges.current.clear(); return; }
      const next = new URLSearchParams(current.search);
      for (const [key, values] of queryChanges.current) { next.delete(key); for (const value of values) next.append(key, value); }
      queryChanges.current.clear();
      if (next.toString() !== new URLSearchParams(current.search).toString()) navigate({ pathname: current.pathname, search: next.toString(), hash: current.hash }, { replace: true });
    }, 0);
  }, [navigate]);
  const canonicalize = useCallback((mode?: ChatMode) => {
    const before = route.current.location.search;
    const params = new URLSearchParams(before);
    const intent = normalizeChatMode(params.get('chat_mode'));
    if (mode && intent && intent !== mode) return;
    params.delete('chat_mode');
    updateQuery(before, params);
  }, [updateQuery]);
  const [machine] = useState(() => new ChatSwitch(initialMode, initialResume));
  useEffect(() => machine.onReady(canonicalize), [machine, canonicalize]);
  const state = useSyncExternalStore(machine.subscribe, machine.getSnapshot);
  useEffect(() => {
    if (state.phase !== 'initializing' && state.phase !== 'switching') return;
    const timeout = setTimeout(machine.failMount, 45_000);
    return () => clearTimeout(timeout);
  }, [machine, state.phase, state.generation]);
  const notice = useStore($preferenceNotice);
  const profiles = useStore($profileModes);
  const visited = useRef(new Set<string>());
  const lastProfileDefault = useRef(profiles[profile]?.mode);
  useEffect(() => {
    const publish = () => $chatHost.set({ profile, machine, state: machine.getSnapshot() });
    publish(); const unsubscribe = machine.subscribe(publish);
    return () => { unsubscribe(); if ($chatHost.get()?.machine === machine) $chatHost.set(null); };
  }, [machine, profile]);
  // StrictMode's probe must not dispose the real host before its first mount.
  const lifetime = useRef(0);
  useEffect(() => {
    const epoch = ++lifetime.current;
    const stopIfUnmounted = () => { if (lifetime.current === epoch) machine.stop(); };
    return () => { clearTimeout(cleanupTimer.current); queueMicrotask(stopIfUnmounted); };
  }, [machine]);
  useEffect(() => {
    if (!isActive || visited.current.has(location.key)) return;
    const url = normalizeChatMode(new URLSearchParams(location.search).get('chat_mode'));
    if (!url) { visited.current.add(location.key); return; }
    if (state.phase !== 'stable') return;
    visited.current.add(location.key);
    if (machine.request(url)) { if (url === state.mounted) canonicalize(); }
  }, [isActive, location.key, location.search, state.phase, state.mounted, machine, canonicalize]);
  useEffect(() => {
    const mode = profiles[profile]?.mode;
    if (mode === lastProfileDefault.current || !profiles[profile]?.loaded || state.phase !== 'stable') return;
    lastProfileDefault.current = mode;
    if (!$browserMode.get() && !normalizeChatMode(new URLSearchParams(route.current.location.search).get('chat_mode'))) machine.request(mode || 'native');
  }, [profiles, profile, state.phase, machine]);
  const generation = state.generation;
  const attach = useCallback((surface: ChatSurfaceLifecycle | null) => machine.attach(surface, generation), [machine, generation]);
  const pending = state.phase !== 'stable';
  return <section className="flex h-full min-h-0 flex-col" aria-label="Dashboard Chat">
    <header className="flex flex-wrap items-center gap-3 border-b p-2">
      <label>Chat Interface <select aria-label="Chat Interface" value={state.requested} disabled={pending} onChange={e => void requestBrowserMode(profile, normalizeChatMode(e.target.value))}>
        <option value="native">Native</option><option value="terminal">Terminal</option>
      </select></label>
      {pending ? <span role="status">{state.phase}: {state.blocked.join(', ')}</span> : null}
      {state.error ? <span role="alert">{state.error}</span> : null}
      {notice ? <span role="alert">{notice}</span> : null}
      {state.blocked.includes('draft') || state.blocked.includes('attachments') ? <button onClick={() => void machine.discard()}>Discard draft and switch</button> : null}
      {state.owner && pending ? <button onClick={machine.cancel}>Cancel switch</button> : null}
      {state.phase === 'failed' ? <><button onClick={machine.retry}>Retry</button><button onClick={machine.revert}>Return to previous interface</button></> : null}
    </header>
    <div className="min-h-0 flex-1">
      <ChatSurfaceBoundary key={generation} onFailure={machine.failMount}><Suspense fallback={<div role="status">Loading chat interface…</div>}>
        {state.mounted === 'native' ? <Native key={generation} isActive={isActive} resume={state.storedId} learn={initialLearn} onRouteQuery={updateQuery} onLifecycle={attach} /> : <Terminal key={generation} profile={profile} resume={state.storedId || undefined} isActive={isActive} onSurface={attach} />}
      </Suspense></ChatSurfaceBoundary>
    </div>
  </section>;
}

export default function ChatSurfaceRouter(props: ChatPageProps) {
  const { profile } = useProfileScope();
  const location = useLocation();
  const browser = useStore($browserMode), profiles = useStore($profileModes);
  const [scope, setScope] = useState({ profile, resume: new URLSearchParams(location.search).get('resume'), learn: new URLSearchParams(location.search).get('learn'), mode: null as ChatMode | null });
  if (scope.profile !== profile) setScope({ profile, resume: null, learn: null, mode: null });
  const url = normalizeChatMode(new URLSearchParams(location.search).get('chat_mode'));
  useEffect(() => { void loadProfileMode(profile); }, [profile]);
  const setting = profiles[profile];
  const activeScope = scope.profile === profile ? scope : { profile, resume: null, learn: null, mode: null };
  let initialMode = activeScope.mode;
  if (!initialMode && (url || browser || setting?.loaded)) {
    initialMode = resolveChatMode(url, browser, setting?.mode).mode;
    setScope({ ...activeScope, mode: initialMode });
  }
  if (!initialMode) return <div role="status">{setting?.error ? 'Profile default unavailable.' : 'Loading chat configuration…'}<button onClick={() => void loadProfileMode(profile)}>Retry</button></div>;
  return <SurfaceHost key={profile} {...props} profile={profile} initialMode={initialMode} initialResume={activeScope.resume} initialLearn={activeScope.learn} />;
}
