import threading

from tui_gateway import server
from tui_gateway.attachments import store
from tui_gateway.transport import bind_transport, reset_transport


class Owner:
    _closed = False
    def write(self, obj):
        return True


def test_prepare_requires_owner_and_busy_submit_does_not_claim(tmp_path, monkeypatch):
    assert "attachment.prepare" in server._methods, "attachment RPC not registered"
    owner = Owner()
    sid = "attachment-rpc-session"
    session = {"transport": owner, "profile_home": str(tmp_path), "running": True,
               "history_lock": threading.RLock(), "agent": None, "session_key": "stored-a"}
    monkeypatch.setitem(server._sessions, sid, session)
    monkeypatch.setattr(server, "_ensure_active_session_slot", lambda *args: None)
    token = bind_transport(owner)
    try:
        response = server._methods["attachment.prepare"](1, {"session_id": sid})["result"]
        d = store.drafts[response["draft_id"]]
        a = store.prepare(d, occurrence="o", request_id="r", name="a.txt", size=1, mime="text/plain")
        temp = store.begin_upload(d, a.id)
        temp.write_bytes(b"x")
        store.finish_upload(d, a.id, temp)
        result = server._methods["prompt.submit"](2, {"session_id": sid, "text": "hello", "attachment_ids": [a.id], **response})
        assert result["error"]["code"] == 4093
        assert a.state == "uploaded"
        other = bind_transport(Owner())
        try:
            denied = server._methods["attachment.snapshot"](3, {"session_id": sid, **response})
            assert "error" in denied
        finally:
            reset_transport(other)
    finally:
        reset_transport(token)
