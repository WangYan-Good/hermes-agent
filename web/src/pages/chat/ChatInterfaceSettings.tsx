import { useSyncExternalStore } from 'react';
import { useSearchParams } from 'react-router';
import { useProfileScope } from '@/contexts/useProfileScope';
import { useChatMode } from './use-chat-mode';
import { chatHosts, preferenceNotice, preferenceRevision, requestBrowserPreference, subscribePreferences } from './chat-preferences';
import { normalizeChatMode } from './chat-mode';

export function ChatInterfaceSettings() {
  const { profile } = useProfileScope();
  const preference = useChatMode();
  const [, setParams] = useSearchParams();
  useSyncExternalStore(subscribePreferences, preferenceRevision);
  const host = chatHosts.get(profile);
  const choose = (mode: 'native' | 'terminal' | null) => {
    requestBrowserPreference(profile, mode);
    if (!host) setParams(prev => { const next = new URLSearchParams(prev); next.delete('chat_mode'); return next; }, { replace: true });
  };
  return <section aria-label="Chat Interface" className="my-4 space-y-2 rounded-lg border border-border p-4">
    <label htmlFor="chat-browser-preference" className="block font-medium">Chat Interface — browser preference</label>
    <select id="chat-browser-preference" className="rounded border border-border bg-background p-2" value={preference.browserMode ?? ''} onChange={e => choose(normalizeChatMode(e.target.value))}>
      <option value="">Follow profile default</option><option value="native">Native — browser-native Hermes chat</option><option value="terminal">Terminal — classic terminal/TUI interface</option>
    </select>
    <p className="text-sm text-muted-foreground">Profile default: {preference.serverMode ?? (preference.loaded ? 'native' : 'unavailable')}. Preference source: {host?.source ?? preference.source}. {preference.browserMode ? 'A browser override takes priority over saved profile settings.' : 'Following the profile default.'}</p>
    {host?.source === 'url' ? <p>The URL overrides the saved profile default. <button type="button" onClick={() => choose(preference.browserMode)}>Clear URL override</button></p> : null}
    {preference.browserMode ? <button type="button" onClick={() => choose(null)}>Clear browser preference</button> : null}
    {preferenceNotice ? <p role="status">{preferenceNotice}</p> : null}
    <p role="status" className="text-sm">{chatHosts.get(profile)?.status ?? 'Applies when built-in chat is opened.'}</p>
  </section>;
}
