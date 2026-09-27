import { useEffect } from 'react';
import { useStore } from '@nanostores/react';
import { $browserMode, $chatHost, $preferenceNotice, $profileModes, loadProfileMode, requestBrowserMode } from './chat-preferences';
import { normalizeChatMode, resolveChatMode, type ChatMode } from './chat-mode';
import { useLocation } from 'react-router';

export interface ChatInterfaceSettingsProps { profile: string; profileValue?: unknown; onProfileChange?: (mode: ChatMode) => void }
export function ChatInterfaceSettings({ profile, profileValue, onProfileChange }: ChatInterfaceSettingsProps) {
  const browser = useStore($browserMode), profiles = useStore($profileModes), host = useStore($chatHost), notice = useStore($preferenceNotice);
  const location = useLocation();
  useEffect(() => { void loadProfileMode(profile); }, [profile]);
  const setting = profiles[profile];
  const url = normalizeChatMode(new URLSearchParams(location.search).get('chat_mode'));
  const resolved = resolveChatMode(url, browser, setting?.mode);
  const current = host?.profile === profile ? host.state : null;
  return <section aria-label="Chat Interface" className="rounded border p-4 space-y-2">
    <h2>Chat Interface</h2>
    {onProfileChange ? <label>Profile default <select aria-label="Profile default" value={normalizeChatMode(profileValue) || 'native'} onChange={e => onProfileChange(normalizeChatMode(e.target.value) || 'native')}><option value="native">Native</option><option value="terminal">Terminal</option></select> (use Save to apply to this profile)</label> : null}
    <p>Saved profile default: {setting?.loaded ? setting.mode || 'native' : 'unavailable'} — edit dashboard.chat.default_mode in the configuration form and Save.</p>
    <label>Browser preference <select aria-label="Browser preference" value={browser || 'profile'} onChange={e => void requestBrowserMode(profile, normalizeChatMode(e.target.value))}>
      <option value="profile">Follow profile default</option><option value="native">Native</option><option value="terminal">Terminal</option>
    </select></label>
    <p>Effective interface: {current?.owner || (setting?.loaded || browser || url ? resolved.mode : 'unavailable')}. Preference source: {resolved.source}. Browser override: {browser || 'none'}. URL override: {url || 'none'}.</p>
    {current && current.phase !== 'stable' ? <p role="status">Switch: {current.phase}. {current.error || current.blocked.join(', ')}</p> : null}
    {notice ? <p role="alert">{notice}</p> : null}
    {setting?.error ? <button onClick={() => void loadProfileMode(profile)}>Retry profile default</button> : null}
  </section>;
}
