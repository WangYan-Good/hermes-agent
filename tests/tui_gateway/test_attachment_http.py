"""Real streamed HTTP bytes must be tied to a prepared gateway occurrence."""
import importlib.util

from fastapi import FastAPI
from fastapi.testclient import TestClient

from tui_gateway.attachments import store


class Owner:
    _closed = False


def test_http_upload_uses_owned_path_and_cancel_is_terminal(tmp_path):
    assert importlib.util.find_spec("tui_gateway.attachment_http"), "attachment streaming endpoint missing"
    from tui_gateway.attachment_http import install
    app = FastAPI()
    install(app)
    d = store.create("runtime-a", "p", tmp_path, Owner())
    a = store.prepare(d, occurrence="o", request_id="r", name="file.txt", size=5, mime="text/plain")
    headers = {"X-Hermes-Attachment-Token": d.token, "X-Hermes-Runtime": "runtime-a", "X-Hermes-Profile": "p", "X-Hermes-Attachment-Upload": a.upload_token}
    with TestClient(app) as client:
        url = f"/api/chat/attachments/{d.id}/{a.id}"
        assert client.put(url, content=b"hello").status_code == 403
        response = client.put(url, headers=headers, content=b"hello")
        assert response.status_code == 200
        assert response.json()["state"] == "uploaded"
        assert "path" not in response.json()
        assert a.path.read_bytes() == b"hello"
        assert client.put(url, headers=headers, content=b"hello").json()["id"] == a.id
        store.cancel(d, a.id)
        assert client.put(url, headers=headers, content=b"hello").status_code == 409
    store.drafts.pop(d.id)


def test_stream_size_limit_deletes_incomplete_file(tmp_path):
    assert importlib.util.find_spec("tui_gateway.attachment_http"), "attachment streaming endpoint missing"
    from tui_gateway.attachment_http import install
    app = FastAPI()
    install(app)
    d = store.create("runtime-a", "p", tmp_path, Owner())
    a = store.prepare(d, occurrence="o", request_id="r", name="file.txt", size=3, mime="text/plain")
    headers = {"X-Hermes-Attachment-Token": d.token, "X-Hermes-Runtime": "runtime-a", "X-Hermes-Profile": "p", "X-Hermes-Attachment-Upload": a.upload_token}
    with TestClient(app) as client:
        response = client.put(f"/api/chat/attachments/{d.id}/{a.id}", headers=headers, content=b"too many bytes")
        assert response.status_code == 413
        assert a.state == "failed"
        assert not list(tmp_path.rglob("*.upload"))
    store.drafts.pop(d.id)


def test_cookie_recovery_requires_same_principal_owner_and_prefix(tmp_path, monkeypatch):
    from tui_gateway.attachment_http import install
    from tui_gateway.methods_attachments import connections
    import time
    app = FastAPI()
    install(app)
    owner = Owner()
    d = store.create("runtime-a", "p", tmp_path, owner)
    monkeypatch.setenv("HERMES_BASE_PATH", "/hermes")
    with TestClient(app, base_url="https://localhost") as client:
        connections["handshake"] = (time.time() + 60, d.runtime_id, d.profile, owner)
        response = client.post(f"/api/chat/attachments/{d.id}/recover", headers={"X-Hermes-Attachment-Connection": "handshake", "X-Hermes-Attachment-Token": d.token})
        assert response.status_code == 200
        cookie = response.headers["set-cookie"]
        assert "HttpOnly" in cookie and "SameSite=strict" in cookie and "Secure" in cookie
        assert f"Path=/hermes/api/chat/attachments/{d.id}" in cookie
        assert response.headers["cache-control"] == "no-store"
        connections["foreign"] = (time.time() + 60, "other-runtime", d.profile, owner)
        assert client.post(f"/api/chat/attachments/{d.id}/recover", headers={"X-Hermes-Attachment-Connection": "foreign", "X-Hermes-Attachment-Token": d.token}).status_code == 403
    store.drafts.pop(d.id)


def test_http_rejects_foreign_authenticated_principal_and_expired_upload(tmp_path):
    from types import SimpleNamespace
    from tui_gateway.attachment_http import install
    app = FastAPI()
    app.state.auth_required = True
    identity = {"user": "alice"}

    @app.middleware("http")
    async def authenticate(request, call_next):
        request.state.session = SimpleNamespace(provider="oauth", user_id=identity["user"])
        return await call_next(request)

    install(app)
    owner = Owner()
    owner._ws = SimpleNamespace(scope={"attachment_principal": ("oauth", "alice")})
    d = store.create("runtime", "profile", tmp_path, owner)
    a = store.prepare(d, occurrence="one", request_id="request", name="file.txt", size=5, mime="text/plain")
    headers = {"X-Hermes-Attachment-Token": d.token, "X-Hermes-Runtime": d.runtime_id, "X-Hermes-Attachment-Upload": a.upload_token}
    url = f"/api/chat/attachments/{d.id}/{a.id}"
    with TestClient(app) as client:
        identity["user"] = "bob"
        assert client.put(url, headers=headers, content=b"hello").status_code == 403
        assert a.state == "local"
        identity["user"] = "alice"
        a.upload_expires = 0
        assert client.put(url, headers=headers, content=b"hello").status_code == 403
        store.prepare(d, occurrence="one", request_id="request", name="file.txt", size=5, mime="text/plain")
        headers["X-Hermes-Attachment-Upload"] = a.upload_token
        assert client.put(url, headers=headers, content=b"hello").status_code == 200
    store.drafts.pop(d.id)


def test_image_pixel_bomb_rejected_before_decode_and_cleans_temporary(tmp_path):
    import io
    import struct
    import zlib
    from PIL import Image
    from tui_gateway.attachment_http import install
    buf = io.BytesIO()
    Image.new("RGB", (1, 1)).save(buf, format="PNG")
    data = bytearray(buf.getvalue())
    data[16:24] = struct.pack(">II", 100000, 100000)
    data[29:33] = struct.pack(">I", zlib.crc32(data[12:29]))
    app = FastAPI()
    install(app)
    d = store.create("runtime", "p", tmp_path, Owner())
    a = store.prepare(d, occurrence="bomb", request_id="r", name="bomb.png", size=len(data), mime="image/png")
    headers = {"X-Hermes-Attachment-Token": d.token, "X-Hermes-Runtime": d.runtime_id, "X-Hermes-Attachment-Upload": a.upload_token}
    with TestClient(app) as client:
        response = client.put(f"/api/chat/attachments/{d.id}/{a.id}", headers=headers, content=bytes(data))
        assert response.status_code == 413
        assert a.state == "failed"
        assert not list(tmp_path.rglob("*.upload"))
    store.drafts.pop(d.id)
