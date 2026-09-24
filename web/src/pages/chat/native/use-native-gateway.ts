import { useEffect, useState, useSyncExternalStore } from "react";
import { NativeSession } from "./native-session";

/** Visibility is deliberately absent: the persistent host owns this lifetime. */
export function useNativeGateway(profile: string, resume: string | null) {
  const [session] = useState(() => new NativeSession(profile, resume));
  const state = useSyncExternalStore(session.subscribe, session.getSnapshot);
  useEffect(() => {
    let cancelled = false;
    // StrictMode's setup/cleanup probe must not create an orphan socket/draft.
    queueMicrotask(() => { if (!cancelled) session.start(); });
    return () => { cancelled = true; session.stop(); };
  }, [session]);
  return { session, state };
}
