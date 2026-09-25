"""Non-sensitive MCP operation identity in the existing live prompt registry.

The authenticated Web start endpoints claim before performing side effects,
so losing the HTTP response cannot lose the recovery identity. No DB or
browser persistence, answers, credentials, or arbitrary metadata mutations.
"""

from pathlib import Path
import re


def _payload(gateway, session_id: str, request_id: str, home: str) -> dict:
    session = gateway._sessions.get(session_id)
    pending = gateway._pending.get(request_id)
    kind, payload = gateway._pending_prompt_payloads.get(request_id, ("", {}))
    if (not session or not pending or pending[0] != session_id or pending[1].is_set()
            or kind != "mcp.setup.request"
            or Path(session.get("profile_home") or gateway._hermes_home).resolve() != Path(home).resolve()):
        raise ValueError("MCP setup request is expired or outside this session/profile")
    return payload


def operation_snapshot(value) -> dict | None:
    """Strict whitelist shared by REST and session.resume/activate snapshots."""
    if not isinstance(value, dict):
        return None
    if value.get("kind") not in {"install", "authorize"}:
        return None
    if not isinstance(value.get("id"), str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", value["id"]):
        return None
    if value.get("state") not in {"starting", "running", "failed"}:
        return None
    if not isinstance(value.get("profile"), str):
        return None
    return {key: value[key] for key in ("kind", "id", "state", "profile")}


def claim_operation(session_id: str, request_id: str, home: str, profile: str,
                    server: str, kind: str, operation_id: str) -> tuple[dict, bool]:
    from tui_gateway import server as gateway

    proposed = operation_snapshot({"kind": kind, "id": operation_id, "state": "starting", "profile": profile})
    if proposed is None:
        raise ValueError("Invalid MCP operation identity")
    with gateway._sessions_lock, gateway._prompt_lock:
        payload = _payload(gateway, session_id, request_id, home)
        if payload.get("server") != server or payload.get("action") != kind:
            raise ValueError("MCP operation does not match the request")
        existing = operation_snapshot(payload.get("operation"))
        if existing:
            return existing, False
        payload["operation"] = proposed
        return dict(proposed), True


def read_operation(session_id: str, request_id: str, home: str) -> dict | None:
    from tui_gateway import server as gateway

    with gateway._sessions_lock, gateway._prompt_lock:
        return operation_snapshot(_payload(gateway, session_id, request_id, home).get("operation"))


def mark_operation(session_id: str, request_id: str, home: str, operation_id: str, state: str) -> None:
    from tui_gateway import server as gateway

    if state not in {"running", "failed"}:
        raise ValueError("Invalid MCP operation state")
    with gateway._sessions_lock, gateway._prompt_lock:
        try:
            payload = _payload(gateway, session_id, request_id, home)
        except ValueError:
            return  # An accepted background action may outlive its request.
        operation = operation_snapshot(payload.get("operation"))
        if operation and operation["id"] == operation_id and operation["state"] == "starting":
            payload["operation"] = {**operation, "state": state}
