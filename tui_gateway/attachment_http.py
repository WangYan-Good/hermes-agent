"""Streaming HTTP transport; mounted behind the dashboard's existing auth."""
import asyncio
import secrets
import os
from pathlib import Path
import time

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse, FileResponse

from .attachments import store


def install(app):
    def principal(request):
        session = getattr(request.state, "session", None)
        if session:
            return (session.provider, session.user_id)
        identity = getattr(request.state, "token_principal", None)
        if identity:
            return (identity.provider, identity.principal)
        if getattr(request.app.state, "auth_required", False):
            raise HTTPException(403, "Attachment authentication rejected")
        return ("local", "local")

    def authorized(request, draft_id):
        # Scope comes from the ledger; the token is scoped to that exact tuple.
        d = store.drafts.get(draft_id)
        try:
            if not d or d.principal != principal(request):
                raise PermissionError()
            runtime = request.headers.get("X-Hermes-Runtime", "")
            return store.authorize(draft_id, request.headers.get("X-Hermes-Attachment-Token", ""), runtime, request.headers.get("X-Hermes-Profile", d.profile))
        except PermissionError:
            raise HTTPException(403, "Attachment ownership rejected") from None

    @app.get("/api/chat/resources")
    async def resource(path: str, profile: str = ""):
        from hermes_cli.web_server import _profile_scope, get_hermes_home

        def resolve():
            with _profile_scope(profile) as scoped_home:
                home = Path(scoped_home or get_hermes_home()).resolve()
                target = Path(path).expanduser()
                if not target.is_absolute():
                    target = home / target
                target = target.resolve()
                roots = [home / name for name in ("attachments", "images", "screenshots", "cache")]
                if not any(target.is_relative_to(root) for root in roots):
                    raise HTTPException(403, "Path outside chat resource roots")
                if not target.is_file():
                    raise HTTPException(404, "Resource unavailable")
                if target.stat().st_size > 100 * 1024 * 1024:
                    raise HTTPException(413, "Resource too large")
                mime = {".mp3": "audio/mpeg", ".wav": "audio/wav", ".ogg": "audio/ogg",
                        ".mp4": "video/mp4", ".webm": "video/webm"}.get(target.suffix.lower(), "application/octet-stream")
                if target.suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}:
                    from PIL import Image
                    try:
                        with Image.open(target) as image:
                            frames = getattr(image, "n_frames", 1)
                            if frames > 200 or image.width * image.height * frames > 40_000_000:
                                raise ValueError("Image dimensions exceeded")
                            mime = Image.MIME.get(image.format, mime)
                            image.verify()
                    except (ValueError, OSError, Image.DecompressionBombError):
                        raise HTTPException(415, "Invalid image") from None
                return target, mime

        target, mime = await asyncio.to_thread(resolve)
        return FileResponse(target, media_type=mime, filename=target.name,
                            headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "private, no-store"})

    @app.post("/api/chat/attachments/{draft_id}/recover")
    async def recover(draft_id: str, request: Request):
        from .methods_attachments import connections
        # No body, filenames, or credentials in query strings.
        grant = request.headers.get("X-Hermes-Attachment-Connection", "")
        with store.lock:
            context = connections.pop(grant, None)
            if not context or context[0] < time.time():
                raise HTTPException(403, "Connection grant expired")
            _, runtime, profile, owner = context
            d = store.drafts.get(draft_id)
            if not d or d.principal != principal(request):
                raise HTTPException(410, "Draft expired")
            cookie_name = "hermes_attachment_" + d.id
            initial = request.headers.get("X-Hermes-Attachment-Token", "")
            try:
                if initial:
                    d = store.authorize(d.id, initial, runtime, profile, owner)
                else:
                    d = store.recover(d.id, request.cookies.get(cookie_name, ""), owner, runtime, profile)
            except PermissionError:
                raise HTTPException(403, "Draft recovery rejected") from None
            response = JSONResponse({"draft_id": d.id, "draft_token": d.token, "runtime_id": d.runtime_id, "attachments": store.snapshot(d)})
            # Respect reverse proxy prefixes without storing credentials in URLs.
            from hermes_cli.web_server import _normalise_prefix
            prefix = _normalise_prefix(request.scope.get("root_path") or os.environ.get("HERMES_BASE_PATH") or request.headers.get("x-forwarded-prefix"))
            path = prefix + "/api/chat/attachments/" + d.id
            response.set_cookie(cookie_name, d.recovery, max_age=86400, httponly=True, secure=request.url.scheme == "https", samesite="strict", path=path)
            response.headers["Cache-Control"] = "no-store"
            return response

    @app.put("/api/chat/attachments/{draft_id}/{attachment_id}")
    async def upload(draft_id: str, attachment_id: str, request: Request):
        d = authorized(request, draft_id)
        try:
            with store.lock:
                a = store.item(d, attachment_id)
                grant = request.headers.get("X-Hermes-Attachment-Upload", "")
                if time.time() > a.upload_expires or not secrets.compare_digest(grant, a.upload_token):
                    raise PermissionError("Upload grant expired or rejected")
                if a.state in {"uploaded", "submitted"}:
                    return a.public()
                temporary = store.begin_upload(d, a.id)
        except PermissionError:
            raise HTTPException(403, "Attachment ownership rejected") from None
        except ValueError:
            raise HTTPException(409, "Attachment is not available for upload") from None
        total = 0
        completed = False
        token = d.token
        try:
            with temporary.open("wb") as out:
                async for chunk in request.stream():
                    total += len(chunk)
                    if total > a.size:
                        raise HTTPException(413, "Upload exceeds prepared size")
                    if a.state != "uploading" or not secrets.compare_digest(token, d.token):
                        raise HTTPException(409, "Upload cancelled or superseded")
                    await asyncio.to_thread(out.write, chunk)
            from PIL import Image
            try:
                await asyncio.to_thread(store.finish_upload, d, a.id, temporary)
            except Image.DecompressionBombError:
                raise HTTPException(413, "Image pixel budget exceeded") from None
            completed = True
            return a.public()
        except (ValueError, OSError):
            raise HTTPException(400, "Invalid or incomplete attachment") from None
        finally:
            if not completed:
                store.fail_upload(d, a.id, temporary)


async def run_attachment_reaper():
    from hermes_constants import get_hermes_home
    await asyncio.to_thread(store.cleanup_orphans, get_hermes_home())
    while True:
        await asyncio.sleep(60)
        await asyncio.to_thread(store.sweep)
