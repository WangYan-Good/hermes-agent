import { Component, lazy, Suspense, useCallback, useEffect, useLayoutEffect, useRef, useState, useSyncExternalStore, type ReactNode } from 'react';
import { useLocation, useNavigate } from 'react-router';
import { useProfileScope } from '@/contexts/useProfileScope';
import type { ChatPageProps } from '../ChatPage';
import { ChatSwitch, type ChatSurfaceLifecycle } from './chat-switch';
import { normalizeChatMode, resolveChatMode } from './chat-mode';
import { chatHosts, commitBrowserPreference, notifyPreferences } from './chat-preferences';
import { useChatMode } from './use-chat-mode';

const loaders = {
  native: () => import('./native/NativeChatPage'),
  terminal: () => import('./TerminalChatPage'),
};
const surfaces = { native: lazy(loaders.native), terminal: lazy(loaders.terminal) };
function invalidateSurface(mode: 'native' | 'terminal' | null) {
  if (mode) surfaces[mode] = lazy(loaders[mode]);
}

class SurfaceBoundary extends Component<{ children: ReactNode; onError(): void }, { failed: boolean }> {
  state = { failed: false };
  static getDerivedStateFromError() { return { failed: true }; }
  componentDidCatch() { this.props.onError(); }
  render() { return this.state.failed ? null : this.props.children; }
}
function SelectedHost({ isActive, initialResume, preference, clearUrl }: ChatPageProps & { initialResume: string | null; preference: ReturnType<typeof useChatMode>; clearUrl(): void }) {
  const { profile } = useProfileScope();
  const location = useLocation();
  const navigate = useNavigate();
  const currentLocation = useRef(location);
  useLayoutEffect(() => { currentLocation.current = location; }, [location]);
  const [machine] = useState(() => new ChatSwitch(preference.effective, initialResume));
  const state = useSyncExternalStore(machine.subscribe, machine.getSnapshot);
  const retryConfig = preference.retry;
  const [notice, setNotice] = useState('');
  useEffect(() => { machine.activate(); return machine.dispose; }, [machine]);
  const register = useCallback((adapter: ChatSurfaceLifecycle) => machine.register(adapter, state.mount), [machine, state.mount]);
  const lastResolved = useRef(preference.effective);
  useEffect(() => {
    if (lastResolved.current !== preference.effective) {
      lastResolved.current = preference.effective;
      machine.request(preference.effective);
    }
  }, [machine, preference.effective]);
  useEffect(() => {
    const host = { source: preference.source, status: notice || (state.target ? `${state.phase}: ${state.target}` : `Active: ${state.active ?? 'connecting'}`), request: (mode: 'native' | 'terminal' | null) => {
      if (mode === null && !preference.profileKnown) {
        setNotice('Could not confirm the profile default. Reloading configuration; retry the preference change when available.');
        retryConfig(); return;
      }
      setNotice('');
      const target = resolveChatMode({ browserMode: mode, serverMode: preference.serverMode }).effective;
      machine.request(target, () => {
        if (!commitBrowserPreference(mode)) setNotice('Interface changed for this page. Browser preference could not be saved.');
        clearUrl();
        const { pathname, search, hash } = currentLocation.current;
        const next = new URLSearchParams(search); next.delete('chat_mode');
        navigate({ pathname, search: next.toString(), hash }, { replace: true });
      });
    } };
    chatHosts.set(profile, host); notifyPreferences();
    return () => { if (chatHosts.get(profile) === host) { chatHosts.delete(profile); notifyPreferences(); } };
  }, [machine, profile, preference.serverMode, preference.source, preference.profileKnown, retryConfig, notice, navigate, clearUrl, state.active, state.phase, state.target]);
  const Surface = state.mounted ? surfaces[state.mounted] : null;
  const stable = state.phase === 'stable-native' || state.phase === 'stable-terminal';
  return <div className="flex h-full min-h-0 flex-col">
    <div className="flex flex-wrap items-center gap-3 border-b border-border px-5 py-2 text-sm">
      <label htmlFor="chat-interface-live">Chat Interface</label>
      <select id="chat-interface-live" value={state.target ?? state.active ?? preference.effective} disabled={state.phase === 'switching' || state.phase === 'initializing'} onChange={e => { const mode = normalizeChatMode(e.target.value); if (mode) chatHosts.get(profile)?.request(mode); }} className="rounded border border-border bg-background p-1">
        <option value="native">Native — browser chat</option><option value="terminal">Terminal — classic TUI</option>
      </select>
      <span className="text-xs text-muted-foreground">Preference: {preference.source}{state.active ? ` · Active: ${state.active}` : ''}</span>
    </div>
    {!stable ? <div role="status" aria-live="polite" className="flex flex-wrap items-center gap-3 px-5 py-2 text-sm">
      <span>{state.target && state.phase === 'waiting-for-idle' ? `Switching to ${state.target} after the current turn and pending work. ` : ''}{state.reason || 'Connecting…'}</span>
      {state.draft ? <button type="button" onClick={() => void machine.discard()}>Discard draft and switch</button> : null}
      {state.phase === 'waiting-for-idle' || state.phase === 'switch-requested' ? <button type="button" onClick={machine.cancel}>Cancel switch</button> : null}
      {state.phase === 'failed' ? <><button type="button" onClick={machine.retry}>Retry</button><button type="button" onClick={() => void machine.revert()}>Return to previous interface</button></> : null}
    </div> : null}
    {notice ? <p role="status">{notice}</p> : null}
    {Surface ? <SurfaceBoundary key={state.mount} onError={() => { invalidateSurface(state.mounted); machine.fail(); }}><Suspense fallback={<div role="status">Loading chat…</div>}><Surface isActive={isActive} inputEnabled={stable || state.phase === 'waiting-for-idle'} handoffResume={state.resume} registerLifecycle={register} /></Suspense></SurfaceBoundary> : null}
  </div>;
}
function ProfileHost(props: ChatPageProps) {
  const location = useLocation();
  const [search, setSearch] = useState(location.pathname.endsWith('/chat') ? location.search : '');
  const lastLocation = useRef({ key: location.key, path: location.pathname });
  useEffect(() => {
    if (lastLocation.current.key === location.key) return;
    const wasChat = lastLocation.current.path.endsWith("/chat");
    lastLocation.current = { key: location.key, path: location.pathname };
    // Bare navigation back to /chat is visibility, not a new mode command.
    if (location.pathname.endsWith('/chat') && (wasChat || new URLSearchParams(location.search).has('chat_mode'))) setSearch(location.search);
  }, [location.key, location.pathname, location.search]);
  const clearUrl = useCallback(() => setSearch(prev => { const next = new URLSearchParams(prev); next.delete("chat_mode"); return next.toString(); }), []);
  const preference = useChatMode(search);
  if (!preference.loaded) return <div role="status">{preference.error ? <><span>Could not load this profile's Chat Interface. </span><button onClick={preference.retry}>Retry</button></> : 'Loading Chat Interface…'}</div>;
  return <SelectedHost {...props} preference={preference} clearUrl={clearUrl} initialResume={props.handoffResume ?? null} />;
}
export default function ChatSurfaceRouter(props: ChatPageProps) {
  const { profile } = useProfileScope();
  const location = useLocation();
  const [scope, setScope] = useState({ profile, resume: new URLSearchParams(location.search).get('resume') });
  if (scope.profile !== profile) setScope({ profile, resume: null });
  return <ProfileHost key={profile} {...props} handoffResume={scope.profile === profile ? scope.resume : null} />;
}
