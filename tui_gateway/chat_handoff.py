"""Dashboard presentation handoff; no prompt or interaction response is synthesized."""

import secrets


def handle(server, rid, params):
    sid = str(params.get("session_id") or "")
    session, error = server._sess_nowait(params, rid)
    if error:
        return error
    owner = server.current_transport() or server._stdio_transport
    if session.get("transport") is not owner:
        return server._err(rid, 4031, "Presentation owner changed")
    action = params.get("action", "status")
    if action not in {"status", "prepare", "cancel", "release"}:
        return server._err(rid, 4004, "Unknown handoff action")
    with session["history_lock"]:
        if session.get("transport") is not owner:
            return server._err(rid, 4031, "Presentation owner changed")
        held = session.get("presentation_handoff")
        # Reconnect first reclaims the session through the existing owner gate.
        # Its new transport inherits the frozen presentation, never a second owner.
        if held:
            held["owner"] = owner
        if action in {"cancel", "release"}:
            if (
                not held
                or held["owner"] is not owner
                or not secrets.compare_digest(
                    str(params.get("ticket", "")), held["ticket"]
                )
            ):
                return server._err(rid, 4094, "Handoff authority unavailable")
            if action == "cancel":
                session.pop("presentation_handoff", None)
                return server._ok(rid, {"cancelled": True})
        blocked = bool(
            session.get("running")
            or session.get("_presentation_workers")
            or session.get("queued_prompt")
            or session.get("queued_prompts")
            or session.get("resume_hydrating")
            or session.get("resume_history_error")
            or session.get("_auto_continue_scheduled")
            or server._pending_interaction_payloads(sid)
            or server._pending_approval_request_payload(
                str(session.get("session_key") or "")
            )
        )
        if blocked:
            return server._ok(rid, {"ready": False})
        key = str(session.get("session_key") or "")
        agent = session.get("agent")
        key = str(getattr(agent, "session_id", None) or key)
        with server._session_db(session) as db:
            if db is None:
                return server._err(rid, 5030, "Session identity unavailable")
            durable = key if db.get_session(key) else None
            if durable is None and (
                session.get("history")
                or session.get("display_history_prefix")
                or session.get("resume_session_id")
            ):
                return server._err(rid, 5030, "Session identity unavailable")
        if action == "prepare":
            if held and held["owner"] is not owner:
                return server._err(rid, 4094, "Handoff already owned")
            held = held or {"owner": owner, "ticket": secrets.token_hex(24)}
            session["presentation_handoff"] = held
        payload = {"ready": True, "stored_id": durable}
        if held:
            payload["ticket"] = held["ticket"]
        if action != "release":
            return server._ok(rid, payload)
        # Freeze remains on the detached record: a submit that looked it up
        # before the pop must still refuse under this same history lock.
    # Never acquire resume_lock while holding history_lock: resume takes them
    # in the opposite order. The frozen record rejects competing submissions.
    with server._session_resume_lock:
        with session["history_lock"]:
            popped = server._pop_session_by_id(sid)
    server._teardown_popped_session(popped, end_reason="presentation_handoff")
    return server._ok(rid, {**payload, "released": True})
