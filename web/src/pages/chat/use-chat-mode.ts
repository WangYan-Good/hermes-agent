import { useCallback, useEffect, useState, useSyncExternalStore } from 'react';
import { useProfileScope } from '@/contexts/useProfileScope';
import { api } from '@/lib/api';
import { getNestedValue } from '@/lib/nested';
import { normalizeChatMode, resolveChatMode, type ChatMode } from './chat-mode';
import { browserPreference, preferenceRevision, profileModes, subscribePreferences } from './chat-preferences';

export function useChatMode(search = typeof window === 'undefined' ? '' : window.location.search) {
  const { profile } = useProfileScope();
  useSyncExternalStore(subscribePreferences, preferenceRevision);
  const [attempt, retry] = useState(0);
  const [server, setServer] = useState<{ profile: string; mode: ChatMode | null; error: boolean; version: number }>();
  useEffect(() => {
    let cancelled = false;
    const version = preferenceRevision();
    void api.getConfig(profile).then(config => {
      if (!cancelled) setServer({ profile, mode: normalizeChatMode(getNestedValue(config, 'dashboard.chat.default_mode')), error: false, version });
    }).catch(() => { if (!cancelled) setServer({ profile, mode: null, error: true, version }); });
    return () => { cancelled = true; };
  }, [profile, attempt]);
  const saved = profileModes.get(profile);
  const current = server?.profile === profile ? server : undefined;
  const serverMode = saved && (!current || saved.version > current.version) ? saved.mode : current?.mode;
  const urlMode = normalizeChatMode(new URLSearchParams(search).get('chat_mode'));
  const browserMode = browserPreference();
  const resolution = resolveChatMode({ urlMode, browserMode, serverMode });
  const retryConfig = useCallback(() => retry(n => n + 1), []);
  const known = !!saved || !!current && !current.error;
  return { ...resolution, profileKnown: known, browserMode, serverMode, urlMode, loaded: known || !!urlMode || !!browserMode, error: !known && !!current?.error, retry: retryConfig };
}
