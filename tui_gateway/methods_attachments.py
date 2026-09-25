"""Browser attachments are a capability of the owning session transport."""
import secrets
import time
from pathlib import Path

from .attachments import store

# Short-lived handoff from an authenticated WS owner to authenticated HTTP.
connections = {}


def profile_key(server, session):
    return str(Path(session.get("profile_home") or server._hermes_home).resolve())


def register(server):
    def owned(params, rid):
        session, err = server._sess_nowait(params, rid)
        if err:
            return None, None, err
        owner = server.current_transport()
        if owner is None or session.get("transport") is not owner:
            return None, None, server._err(rid, 4031, "Attachment ownership rejected")
        return session, owner, None

    @server.method("attachment.connection")
    def connection(rid, params):
        session, owner, err = owned(params, rid)
        if err:
            return err
        with store.lock:
            for key, value in list(connections.items()):
                if value[0] < time.time():
                    del connections[key]
            if sum(value[3] is owner for value in connections.values()) >= 20 or len(connections) >= 2000:
                return server._err(rid, 4032, "Too many connection grants")
            grant = secrets.token_urlsafe(32)
            connections[grant] = (time.time() + 60, params["session_id"], profile_key(server, session), owner)
        return server._ok(rid, {"connection_token": grant})

    @server.method("attachment.prepare")
    def prepare(rid, params):
        session, owner, err = owned(params, rid)
        if err:
            return err
        try:
            profile = profile_key(server, session)
            if params.get("draft_id"):
                d = store.authorize(params["draft_id"], params.get("draft_token"), params["session_id"], profile, owner)
            else:
                d = store.create(params["session_id"], profile, Path(profile), owner)
            a = None
            if params.get("occurrence_id"):
                a = store.prepare(d, occurrence=params["occurrence_id"], request_id=params.get("upload_request_id"), name=params.get("name"), size=params.get("size"), mime=params.get("mime", "application/octet-stream"))
            return server._ok(rid, {"draft_id": d.id, "draft_token": d.token, "attachment": a.public() if a else None, "upload_token": a.upload_token if a else None})
        except (PermissionError, ValueError):
            return server._err(rid, 4032, "Attachment preparation rejected")

    def resolve(params, rid):
        session, owner, err = owned(params, rid)
        if err:
            return None, err
        try:
            return store.authorize(params.get("draft_id"), params.get("draft_token"), params.get("session_id"), profile_key(server, session), owner), None
        except PermissionError:
            return None, server._err(rid, 4032, "Attachment ownership rejected")

    @server.method("attachment.snapshot")
    def snapshot(rid, params):
        d, err = resolve(params, rid)
        return err or server._ok(rid, {"draft_id": d.id, "attachments": store.snapshot(d)})

    @server.method("attachment.cancel")
    def cancel(rid, params):
        d, err = resolve(params, rid)
        if err:
            return err
        try:
            store.cancel(d, params.get("attachment_id"))
            return server._ok(rid, {"attachments": store.snapshot(d)})
        except PermissionError:
            return server._err(rid, 4032, "Attachment ownership rejected")
