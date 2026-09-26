import { useEffect, useRef } from "react";
import { useLocation, useNavigate } from "react-router";
import type { NativeSession } from "./native-session";
import type { NativeSessionState } from "./native-types";

const obsoleteModeKey = "hermes.dashboard.chat.mode";

/** URL housekeeping belongs to the active Native session, never to a mode router. */
export function useNativeRoute(session: NativeSession, state: NativeSessionState, isActive: boolean, initialLearn: string | null) {
  const location = useLocation();
  const navigate = useNavigate();
  const params = new URLSearchParams(location.search);
  const resume = params.get("resume");
  const learn = params.get("learn");
  const lastResume = useRef(resume);
  const initialLocation = useRef(location.key);
  const cleanedStorage = useRef(false);

  useEffect(() => {
    if (!state.ready || cleanedStorage.current) return;
    cleanedStorage.current = true;
    try {
      // The value is obsolete, including unknown values. No other storage is touched.
      localStorage.getItem(obsoleteModeKey);
      localStorage.removeItem(obsoleteModeKey);
    } catch { /* Storage access and cleanup are both best effort. */ }
  }, [state.ready]);

  useEffect(() => {
    if (!isActive) return;
    // Removing a query on route-away/back is visibility, not a new session.
    if (resume && resume !== lastResume.current && resume !== state.storedId) {
      lastResume.current = resume;
      session.select(resume);
      return;
    }
    lastResume.current = resume;
    if (!state.ready) return;
    const next = new URLSearchParams(location.search);
    next.delete("chat_mode");
    const wanted = state.durable ? state.storedId : null;
    if (wanted) next.set("resume", wanted); else next.delete("resume");
    if (learn !== null) {
      // A profile switch must not import the previous profile's pending request.
      if ((location.key === initialLocation.current && initialLearn === null) || !learn.trim()) {
        next.delete("learn");
      } else if (!session.draftText) {
        session.setDraft(`/learn ${learn}`.trim());
        next.delete("learn");
      }
    }
    if (next.toString() !== new URLSearchParams(location.search).toString()) {
      navigate({ pathname: location.pathname, search: next.toString(), hash: location.hash }, { replace: true });
    }
  }, [isActive, resume, learn, initialLearn, location, navigate, session, state.ready, state.durable, state.storedId]);

  const acceptLearn = (append: boolean) => {
    if (!isActive || !state.ready || !learn) return;
    if (append) session.setDraft(`${session.draftText}${session.draftText ? "\n" : ""}/learn ${learn}`.trim());
    const next = new URLSearchParams(location.search);
    next.delete("learn");
    navigate({ pathname: location.pathname, search: next.toString(), hash: location.hash }, { replace: true });
  };
  const newSession = () => {
    session.select(null);
    lastResume.current = null;
    const next = new URLSearchParams(location.search);
    next.delete("resume");
    navigate({ pathname: location.pathname, search: next.toString(), hash: location.hash }, { replace: true });
  };
  return { newSession, acceptLearn, pendingLearn: isActive && state.ready && !!learn?.trim() && !!session.draftText };
}
