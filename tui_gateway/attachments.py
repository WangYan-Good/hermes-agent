"""Live browser attachment ledger. Bytes use the existing profile mount roots.

No draft is a durable session, no upload grant is prompt idempotency, and no
client-supplied path is ever used as a storage location.
"""
from __future__ import annotations

import os
import secrets
import re
import sqlite3
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from contextlib import closing
from pathlib import Path
from urllib.parse import unquote

FILE_LIMIT = 100 * 1024 * 1024
IMAGE_LIMIT = 25 * 1024 * 1024
TOTAL_LIMIT = 200 * 1024 * 1024
TTL = 86400
IMAGE_TYPES = {"PNG": ".png", "JPEG": ".jpg", "GIF": ".gif", "WEBP": ".webp", "BMP": ".bmp"}


def owner_principal(owner):
    return getattr(getattr(owner, "_ws", None), "scope", {}).get("attachment_principal", ("local", "local"))


@dataclass
class Attachment:
    id: str
    occurrence: str
    request_id: str
    name: str
    size: int
    mime: str
    state: str = "local"
    path: Path | None = None
    temporary: Path | None = None
    turn_id: str | None = None
    upload_token: str = field(default_factory=lambda: secrets.token_urlsafe(32), repr=False)
    upload_expires: float = field(default_factory=lambda: time.time() + 300)

    def public(self):
        return {"id": self.id, "occurrence_id": self.occurrence, "request_id": self.request_id,
                "name": self.name, "size": self.size, "mime": self.mime,
                "state": self.state, "turn_id": self.turn_id}


@dataclass
class Draft:
    id: str
    runtime_id: str
    profile: str
    home: Path
    owner: object
    principal: tuple[str, str] = ("local", "local")
    token: str = field(default_factory=lambda: secrets.token_urlsafe(32), repr=False)
    recovery: str = field(default_factory=lambda: secrets.token_urlsafe(32), repr=False)
    updated: float = field(default_factory=time.time)
    items: dict[str, Attachment] = field(default_factory=dict)


class AttachmentStore:
    def __init__(self):
        self.lock = threading.RLock()
        self.drafts: dict[str, Draft] = {}
        self.homes: set[Path] = set()

    def create(self, runtime_id: str, profile: str, home: Path, owner: object) -> Draft:
        with self.lock:
            self.expire()
            if Path(home).resolve() not in self.homes:
                self.cleanup_orphans(Path(home))
            if sum(d.owner is owner for d in self.drafts.values()) >= 20 or len(self.drafts) >= 1000:
                raise ValueError("Too many attachment drafts")
            d = Draft(uuid.uuid4().hex, runtime_id, profile, Path(home), owner, principal=owner_principal(owner))
            self.drafts[d.id] = d
            return d

    def authorize(self, draft_id, token, runtime_id, profile, owner=None) -> Draft:
        with self.lock:
            d = self.drafts.get(draft_id)
            if (not d or getattr(d.owner, "_closed", False) or time.time() - d.updated > TTL or not isinstance(token, str)
                    or not secrets.compare_digest(d.token, token)
                    or d.runtime_id != runtime_id or d.profile != profile
                    or (owner is not None and d.owner is not owner)):
                raise PermissionError("Attachment ownership rejected")
            d.updated = time.time()
            return d

    def recover(self, draft_id, recovery, owner, runtime_id, profile):
        with self.lock:
            d = self.drafts.get(draft_id)
            if (not d or time.time() - d.updated > TTL or not isinstance(recovery, str)
                    or not secrets.compare_digest(d.recovery, recovery)
                    or d.runtime_id != runtime_id or d.profile != profile
                    or d.principal != owner_principal(owner)
                    or (d.owner is not owner and not getattr(d.owner, "_closed", False))):
                raise PermissionError("Attachment recovery rejected")
            if d.owner is not owner:
                for item in d.items.values():
                    if item.state == "uploading":
                        item.state = "failed"
                        if item.temporary:
                            item.temporary.unlink(missing_ok=True)
                d.token = secrets.token_urlsafe(32)
                d.owner = owner
            d.updated = time.time()
            return d

    def prepare(self, d, *, occurrence, request_id, name, size, mime):
        for value in (occurrence, request_id):
            if not isinstance(value, str) or not value or len(value) > 128:
                raise ValueError("Invalid occurrence or request identity")
        if not isinstance(name, str):
            raise ValueError("Invalid filename")
        decoded = unquote(name)
        if (not decoded.strip() or len(decoded) > 240 or decoded in {".", ".."}
                or any(c in decoded for c in '/\\') or any(ord(c) < 32 for c in decoded)):
            raise ValueError("Unsafe filename")
        if not isinstance(mime, str) or len(mime) > 120 or any(ord(c) < 32 for c in mime):
            raise ValueError("Invalid MIME type")
        mime = mime.lower().split(";", 1)[0] or "application/octet-stream"
        limit = IMAGE_LIMIT if mime.startswith("image/") and mime != "image/svg+xml" else FILE_LIMIT
        if isinstance(size, bool) or not isinstance(size, int) or not 0 < size <= limit:
            raise ValueError("File is empty or exceeds upload limit")
        with self.lock:
            for a in d.items.values():
                if a.occurrence == occurrence:
                    if (a.request_id, a.name, a.size, a.mime) != (request_id, name, size, mime):
                        raise ValueError("Occurrence conflicts with an existing upload")
                    if a.state in {"local", "failed"}:
                        a.upload_token = secrets.token_urlsafe(32)
                        a.upload_expires = time.time() + 300
                    return a
            active = [a for a in d.items.values() if a.state not in {"cancelled", "submitted"}]
            if len(active) >= 10 or sum(a.size for a in active) + size > TOTAL_LIMIT:
                raise ValueError("Attachment count or total size limit exceeded")
            if len(d.items) >= 1000:
                raise ValueError("Draft occurrence limit exceeded; start a new draft")
            a = Attachment(uuid.uuid4().hex, occurrence, request_id, name, size, mime)
            d.items[a.id] = a
            d.updated = time.time()
            return a

    def item(self, d, attachment_id):
        a = d.items.get(attachment_id)
        if not a:
            raise PermissionError("Attachment does not belong to this draft")
        return a

    def begin_upload(self, d, attachment_id):
        with self.lock:
            a = self.item(d, attachment_id)
            if a.state not in {"local", "failed"}:
                raise ValueError("Attachment is not available for upload")
            if sum(i.state == "uploading" for i in d.items.values()) >= 2:
                raise ValueError("Two uploads are already in flight")
            image = a.mime.startswith("image/") and a.mime != "image/svg+xml"
            root = d.home / ("images" if image else "attachments") / "web-drafts" / d.id
            if not root.resolve().is_relative_to(d.home.resolve()):
                raise ValueError("Attachment directory escapes profile")
            root.mkdir(parents=True, exist_ok=True, mode=0o700)
            path = root / (a.id + "." + uuid.uuid4().hex + ".upload")
            # Exclusive creation prevents retries from sharing a write target.
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
            a.temporary = path
            a.state = "uploading"
            return path

    def finish_upload(self, d, attachment_id, temporary):
        with self.lock:
            a = self.item(d, attachment_id)
            if a.state != "uploading" or a.temporary != temporary or getattr(d.owner, "_closed", False):
                temporary.unlink(missing_ok=True)
                raise ValueError("Upload was cancelled or superseded")
            if temporary.stat().st_size != a.size:
                raise ValueError("Upload size mismatch")
            suffix = Path(a.name).suffix.lower()
            if suffix in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"} and not a.mime.startswith("image/"):
                raise ValueError("Image requires an image MIME type")
            if a.mime.startswith("image/") and a.mime != "image/svg+xml":
                from PIL import Image
                with Image.open(temporary) as image:
                    expected = IMAGE_TYPES.get(image.format)
                    if not expected or image.width * image.height > 40_000_000:
                        raise ValueError("Unsupported image or excessive dimensions")
                    if suffix and suffix not in {expected, ".jpeg" if expected == ".jpg" else expected}:
                        raise ValueError("Image extension does not match content")
                    actual_mime = Image.MIME.get(image.format, "")
                    if a.mime != actual_mime:
                        raise ValueError("Image MIME does not match content")
                    image.verify()
                with Image.open(temporary) as decoded:
                    frames = getattr(decoded, "n_frames", 1)
                    if frames > 200 or decoded.width * decoded.height * frames > 40_000_000:
                        raise ValueError("Image frame pixel budget exceeded")
                    for frame in range(frames):
                        decoded.seek(frame)
                        decoded.load()
                suffix = expected
            target = temporary.with_name(a.id + suffix)
            os.replace(temporary, target)
            a.path = target
            a.temporary = None
            a.state = "uploaded"
            d.updated = time.time()
            return a

    def fail_upload(self, d, attachment_id, temporary):
        with self.lock:
            a = self.item(d, attachment_id)
            temporary.unlink(missing_ok=True)
            if a.state == "uploading" and a.temporary == temporary:
                a.state = "failed"
                a.temporary = None

    def cancel(self, d, attachment_id):
        with self.lock:
            a = self.item(d, attachment_id)
            if a.state == "submitted":
                return
            a.state = "cancelled"
            for path in (a.temporary, a.path):
                if path:
                    path.unlink(missing_ok=True)

    def claim(self, d, ids, turn_id):
        with self.lock:
            if not isinstance(ids, list) or not ids or len(ids) > 10 or any(not isinstance(i, str) for i in ids) or len(set(ids)) != len(ids):
                raise ValueError("Invalid attachment selection")
            items = [self.item(d, i) for i in ids]
            if any(a.state != "uploaded" or not a.path or not a.path.is_file() for a in items):
                raise ValueError("Attachments are not ready or were already submitted")
            for a in items:
                a.state = "submitted"
                a.turn_id = turn_id
            d.updated = time.time()
            return items

    def reject_claim(self, d, ids, turn_id):
        """Only the synchronous pre-dispatch rejection may return a claim."""
        with self.lock:
            for aid in ids:
                a = self.item(d, aid)
                if a.state == "submitted" and a.turn_id == turn_id:
                    a.state = "uploaded"
                    a.turn_id = None

    def snapshot(self, d):
        with self.lock:
            return [a.public() for a in d.items.values()]

    def cleanup_orphans(self, home):
        """Repeatable, conservative scan, serialized with every ledger transition.

        A live item protects both its temporary and completed names. Only old
        generated completed files with a successful negative durable lookup
        are removable. An unreadable directory/database is never an orphan.
        """
        with self.lock:
            home = home.resolve()
            self.homes.add(home)
            live = {(d.id, a.id) for d in self.drafts.values() if d.home.resolve() == home
                    for a in d.items.values() if a.state != "cancelled"}
            try:
                self._cleanup_home(home, live)
            except OSError:
                pass  # Retain anything whose filesystem ownership is uncertain.

    def _cleanup_home(self, home, live):
        for category in ("images", "attachments"):
            root = home / category / "web-drafts"
            if (not root.is_dir() or (home / category).is_symlink() or root.is_symlink()
                    or not root.resolve().is_relative_to(home)):
                continue
            for folder in root.iterdir():
                if folder.is_symlink() or not folder.is_dir() or not re.fullmatch(r"[0-9a-f]{32}", folder.name):
                    continue
                for path in folder.iterdir():
                    if path.is_symlink() or not path.is_file():
                        continue
                    temporary = re.fullmatch(r"([0-9a-f]{32})\.[0-9a-f]{32}\.upload", path.name)
                    completed = re.fullmatch(r"([0-9a-f]{32})(?:\.[^./]+)?", path.name)
                    identity = temporary or completed
                    if not identity or (folder.name, identity[1]) in live:
                        continue
                    if temporary:
                        path.unlink(missing_ok=True)
                    elif time.time() - path.stat().st_mtime > TTL:
                        try:
                            with closing(sqlite3.connect((home / "state.db").as_uri() + "?mode=ro", uri=True)) as db:
                                found = db.execute("SELECT 1 FROM messages WHERE instr(content, ?) > 0 OR instr(COALESCE(display_metadata, ''), ?) > 0 LIMIT 1", (identity[1], identity[1])).fetchone()
                            if not found:
                                path.unlink(missing_ok=True)
                        except (OSError, sqlite3.Error):
                            pass  # Unknown ownership is retained, never guessed.

    def sweep(self):
        """Revisit every profile that has hosted browser drafts in this process."""
        with self.lock:
            self.expire()
            for home in tuple(self.homes):
                self.cleanup_orphans(home)

    def expire(self):
        with self.lock:
            for key, d in list(self.drafts.items()):
                if time.time() - d.updated <= TTL:
                    continue
                # The gateway takes history_lock before this ledger lock.
                # Do not acquire history_lock here (reverse lock order). The
                # runtime's running flag is published before claim dispatch;
                # retaining on any active turn is intentionally conservative.
                gateway = sys.modules.get("tui_gateway.server")
                session = getattr(gateway, "_sessions", {}).get(d.runtime_id)
                if session and session.get("running") and any(a.state == "submitted" for a in d.items.values()):
                    continue
                for a in d.items.values():
                    self.cancel(d, a.id)
                del self.drafts[key]


store = AttachmentStore()
