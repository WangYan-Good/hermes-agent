"""Allowlisted tool display metadata, independent of agent execution semantics."""
import mimetypes
import re
from urllib.parse import urlsplit


def safe_media_ref(value):
    if not isinstance(value, str) or not value or len(value) > 4096 or any(ord(c) < 32 for c in value):
        return None
    try:
        parsed = urlsplit(value)
        if parsed.scheme:
            if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password or parsed.query:
                return None
        return value
    except ValueError:
        return None


def tool_presentation(tool_id, name, args, result, diff):
    p = {"version": 1, "tool_call_id": tool_id}
    if isinstance(diff, str) and diff:
        # Diff was computed by the backend's edit snapshot/result contract.
        # Headers here are the structured diff format, never arbitrary stdout.
        sections = []
        for line in diff.splitlines():
            if line.startswith("--- "):
                sections.append([line])
            elif sections:
                sections[-1].append(line)
        changes = []
        for lines in sections:
            target = next((s[4:].split("\t", 1)[0] for s in lines if s.startswith("+++ ")), "")
            source = lines[0][4:].split("\t", 1)[0]
            path = source if target == "/dev/null" else target
            if path.startswith(("a/", "b/")):
                path = path[2:]
            if not path or any(ord(c) < 32 for c in path):
                continue
            operation = "delete" if target == "/dev/null" else "create" if source == "/dev/null" else None
            # Snapshot diffs use a/b headers even for missing or unreadable
            # files. Zero-length sides cannot prove create/delete/modify.
            ranges = [re.match(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", line) for line in lines]
            if operation is None and any(match and int(match[2] or 1) > 0 and int(match[4] or 1) > 0 for match in ranges):
                operation = "modify"
            changes.append({"path": path, "diff": "\n".join(lines),
                            **({"operation": operation} if operation else {}),
                            "added": sum(s.startswith("+") and not s.startswith("+++") for s in lines),
                            "removed": sum(s.startswith("-") and not s.startswith("---") for s in lines)})
        if changes:
            p["changes"] = changes
    if name in {"image_generate", "image_generation"} and isinstance(result, dict) and result.get("success") is not False:
        ref = safe_media_ref(result.get("image"))
        if ref:
            mime = mimetypes.guess_type(urlsplit(ref).path)[0] or "image/png"
            if mime in {"image/png", "image/jpeg", "image/gif", "image/webp", "image/bmp"}:
                p["media"] = [{"id": f"{tool_id}:media:0", "ref": ref, "mime": mime, "title": "Generated image"}]
    return p if len(p) > 2 else None


def persist_tool_presentation(db, session_id, tool_id, presentation):
    """Merge into the already flushed tool row, preserving other sidecar fields."""
    if not presentation:
        return None

    def write(conn):
        row = conn.execute("SELECT id, display_metadata FROM messages WHERE session_id = ? AND role = 'tool' AND tool_call_id = ? ORDER BY id DESC LIMIT 1", (session_id, tool_id)).fetchone()
        if row is None:
            return None
        meta = db._decode_display_metadata(row[1]) or {}
        meta["presentation"] = presentation
        conn.execute("UPDATE messages SET display_metadata = ? WHERE id = ? AND session_id = ?", (db._encode_display_metadata(meta), row[0], session_id))
        return row[0]

    return db._execute_write(write)


def finalize_turn_presentation(db, session_id, turn_id):
    """Stamp display identity after durable append; never correlate by body text.

    The user sidecar is the authority for the turn boundary. A missing boundary
    leaves old/client-specific history untouched.
    """
    def write(conn):
        user = conn.execute("SELECT id, display_metadata FROM messages WHERE session_id = ? AND role = 'user' ORDER BY id DESC LIMIT 1", (session_id,)).fetchone()
        if not user or (db._decode_display_metadata(user[1]) or {}).get("turn_id") != turn_id:
            return []
        rows = conn.execute("SELECT id, role, content, display_metadata FROM messages WHERE session_id = ? AND id > ? AND active = 1 ORDER BY id", (session_id, user[0])).fetchall()
        sources = []
        for row in rows:
            meta = db._decode_display_metadata(row[3]) or {}
            meta["turn_id"] = turn_id
            content = db._decode_content(row[2])
            has_text = bool(content) if isinstance(content, str) else isinstance(content, list) and any(isinstance(part, dict) and part.get("text") for part in content)
            if row[1] == "assistant" and has_text:
                source = f"{session_id}:row:{row[0]}:content:0"
                meta["content_source"] = source
                sources.append(source)
            conn.execute("UPDATE messages SET display_metadata = ? WHERE id = ? AND session_id = ?", (db._encode_display_metadata(meta), row[0], session_id))
        return sources
    return db._execute_write(write)
