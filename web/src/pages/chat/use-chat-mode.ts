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

export function useChatMode(initialSearch = typeof window === "undefined" ? "" : window.location.search) {
  const { profile } = useProfileScope();
  const [browserMode] = useState(readBrowserChatMode);
  // UI-P3 activation is latched at the persistent host's first mount. Route
  // visibility and late config must never swap transports. Unify in UI-P6.
  const [urlMode] = useState(() => normalizeChatMode(new URLSearchParams(initialSearch).get("chat_mode")));
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
    urlMode,
    browserMode,
    serverMode: server?.profile === profile ? server.mode : null,
    nativeAvailable: urlMode === "native",
  });
}
