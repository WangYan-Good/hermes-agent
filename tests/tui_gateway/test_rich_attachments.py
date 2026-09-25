"""The attachment ledger owns occurrences, never the browser's selected session."""
import importlib.util
from pathlib import Path

import pytest


def ledger(tmp_path):
    assert importlib.util.find_spec("tui_gateway.attachments"), "session-owned attachment ledger is missing"
    from tui_gateway.attachments import AttachmentStore
    return AttachmentStore()


class Owner:
    _closed = False


def draft(store, tmp_path, owner=None):
    return store.create("runtime-a", "profile-a", tmp_path, owner or Owner())


def prepare(store, d, occurrence="occurrence-1", **overrides):
    return store.prepare(d, occurrence=occurrence, request_id="request-1", name="report.txt", size=5, mime="text/plain", **overrides)


def test_occurrence_retry_and_cancel_cannot_resurrect(tmp_path):
    store = ledger(tmp_path)
    d = draft(store, tmp_path)
    a = prepare(store, d)
    assert prepare(store, d).id == a.id
    store.cancel(d, a.id)
    with pytest.raises(ValueError):
        store.begin_upload(d, a.id)
    b = prepare(store, d, occurrence="occurrence-2")
    assert b.id != a.id


@pytest.mark.parametrize("name", ["../secret", "%2e%2e%2fsecret", "a/b", "a\\b", "a\x00b"])
def test_rejects_untrusted_filenames(tmp_path, name):
    store = ledger(tmp_path)
    d = draft(store, tmp_path)
    with pytest.raises(ValueError):
        store.prepare(d, occurrence="o", request_id="r", name=name, size=5, mime="text/plain")


def test_scope_and_transport_are_both_required(tmp_path):
    store = ledger(tmp_path)
    owner = Owner()
    d = draft(store, tmp_path, owner)
    assert store.authorize(d.id, d.token, "runtime-a", "profile-a", owner) is d
    for sid, profile, transport in [("runtime-b", "profile-a", owner), ("runtime-a", "profile-b", owner), ("runtime-a", "profile-a", Owner())]:
        with pytest.raises(PermissionError):
            store.authorize(d.id, d.token, sid, profile, transport)


def test_uploaded_bytes_claim_once_and_cancel_preserves_history(tmp_path):
    store = ledger(tmp_path)
    d = draft(store, tmp_path)
    a = prepare(store, d)
    path = store.begin_upload(d, a.id)
    path.write_bytes(b"hello")
    store.finish_upload(d, a.id, path)
    assert store.snapshot(d)[0]["state"] == "uploaded"
    claimed = store.claim(d, [a.id], "turn-1")
    assert claimed[0].path.read_bytes() == b"hello"
    with pytest.raises(ValueError):
        store.claim(d, [a.id], "turn-2")
    store.cancel(d, a.id)
    assert claimed[0].path.exists()
    assert store.snapshot(d)[0]["state"] == "submitted"


def test_cancel_wins_over_late_finish(tmp_path):
    store = ledger(tmp_path)
    d = draft(store, tmp_path)
    a = prepare(store, d)
    path = store.begin_upload(d, a.id)
    path.write_bytes(b"hello")
    store.cancel(d, a.id)
    with pytest.raises(ValueError):
        store.finish_upload(d, a.id, path)
    assert not path.exists()


def test_recovery_rotates_authority_and_refuses_active_other_owner(tmp_path):
    store = ledger(tmp_path)
    d = draft(store, tmp_path)
    old = d.token
    with pytest.raises(PermissionError):
        store.recover(d.id, d.recovery, Owner(), "runtime-a", "profile-a")
    d.owner._closed = True
    replacement = Owner()
    store.recover(d.id, d.recovery, replacement, "runtime-a", "profile-a")
    assert d.token != old
    with pytest.raises(PermissionError):
        store.authorize(d.id, old, "runtime-a", "profile-a", replacement)


def test_claim_validation_is_atomic(tmp_path):
    store = ledger(tmp_path)
    d = draft(store, tmp_path)
    ready = prepare(store, d)
    temp = store.begin_upload(d, ready.id)
    temp.write_bytes(b"hello")
    store.finish_upload(d, ready.id, temp)
    pending = prepare(store, d, occurrence="other")
    with pytest.raises(ValueError):
        store.claim(d, [ready.id, pending.id], "turn")
    assert ready.state == "uploaded"
    with pytest.raises(PermissionError):
        store.claim(d, [ready.id, "foreign-id"], "turn")
    assert ready.state == "uploaded"


def test_mime_spoof_and_incomplete_images_are_rejected(tmp_path):
    store = ledger(tmp_path)
    d = draft(store, tmp_path)
    a = store.prepare(d, occurrence="image", request_id="r", name="fake.png", size=5, mime="image/png")
    temp = store.begin_upload(d, a.id)
    temp.write_bytes(b"hello")
    with pytest.raises((ValueError, OSError)):
        store.finish_upload(d, a.id, temp)
    store.fail_upload(d, a.id, temp)
    assert not temp.exists()


def test_expiry_preserves_claimed_file(tmp_path):
    from tui_gateway.attachments import TTL
    store = ledger(tmp_path)
    d = draft(store, tmp_path)
    a = prepare(store, d)
    temp = store.begin_upload(d, a.id)
    temp.write_bytes(b"hello")
    store.finish_upload(d, a.id, temp)
    store.claim(d, [a.id], "turn")
    other = prepare(store, d, occurrence="other")
    unfinished = store.begin_upload(d, other.id)
    d.updated -= TTL + 1
    store.expire()
    assert a.path.exists()
    assert not unfinished.exists()
    assert d.id not in store.drafts


def test_limits_and_conflicting_requests(tmp_path):
    from tui_gateway.attachments import FILE_LIMIT as MAX_FILE
    store = ledger(tmp_path)
    d = draft(store, tmp_path)
    for size in (0, MAX_FILE + 1):
        with pytest.raises(ValueError):
            store.prepare(d, occurrence="bad", request_id="r", name="file.txt", size=size, mime="text/plain")
    first = prepare(store, d)
    with pytest.raises(ValueError):
        store.prepare(d, occurrence=first.occurrence, request_id="conflict", name="file.txt", size=5, mime="text/plain")
    for index in range(9):
        prepare(store, d, occurrence=f"item-{index}")
    with pytest.raises(ValueError):
        prepare(store, d, occurrence="eleventh")
    items = list(d.items.values())
    store.begin_upload(d, items[0].id)
    store.begin_upload(d, items[1].id)
    with pytest.raises(ValueError):
        store.begin_upload(d, items[2].id)


def test_restart_cleanup_preserves_durable_references_and_unknown_files(tmp_path):
    import os
    import time
    from hermes_state import SessionDB
    from tui_gateway.attachments import TTL
    folder = tmp_path / "attachments" / "web-drafts" / ("a" * 32)
    folder.mkdir(parents=True)
    referenced = folder / ("b" * 32 + ".txt")
    orphan = folder / ("c" * 32 + ".txt")
    incomplete = folder / ("d" * 32 + "." + "e" * 32 + ".upload")
    unknown = folder / "user-owned.txt"
    for path in (referenced, orphan, incomplete, unknown):
        path.write_text("data")
        os.utime(path, (time.time() - TTL - 1,) * 2)
    with SessionDB(tmp_path / "state.db") as db:
        db.create_session("durable", source="webui")
        db.append_message("durable", "user", content="@file:" + str(referenced))
    store = ledger(tmp_path)
    store.cleanup_orphans(tmp_path)
    assert referenced.exists() and unknown.exists()
    assert not orphan.exists() and not incomplete.exists()


def test_restart_cleanup_does_not_follow_parent_symlinks(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    outside = tmp_path / "outside"
    folder = outside / "web-drafts" / ("a" * 32)
    folder.mkdir(parents=True)
    incomplete = folder / ("b" * 32 + "." + "c" * 32 + ".upload")
    incomplete.write_text("must survive")
    (home / "attachments").symlink_to(outside, target_is_directory=True)
    ledger(home).cleanup_orphans(home)
    assert incomplete.exists()
