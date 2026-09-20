import { useEffect, useState } from "react";

import { useProfileScope } from "@/contexts/useProfileScope";
import { api } from "@/lib/api";
import { getNestedValue } from "@/lib/nested";

import {
  normalizeChatMode,
  readBrowserChatMode,
  resolveChatMode,
  type ChatMode,
} from "./chat-mode";

export function useChatMode() {
  const { profile } = useProfileScope();
  const [browserMode] = useState(readBrowserChatMode);
  const [server, setServer] = useState<{ profile: string; mode: ChatMode | null }>();

  useEffect(() => {
    let cancelled = false;
    void api.getConfig(profile).then((config) => {
      if (!cancelled) {
        setServer({
          profile,
          mode: normalizeChatMode(getNestedValue(config, "dashboard.chat.default_mode")),
        });
      }
    }).catch(() => {
      // Best-effort presentation preference; never block mounting the terminal.
    });
    return () => { cancelled = true; };
  }, [profile]);

  return resolveChatMode({
    browserMode,
    serverMode: server?.profile === profile ? server.mode : null,
    // UI-P1 has no native renderer or transport, regardless of user preference.
    nativeAvailable: false,
  });
}
