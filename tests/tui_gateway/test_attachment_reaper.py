"""Repeated orphan sweeps use real profile files, SessionDB and ledger locks."""
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from hermes_state import SessionDB
from tui_gateway.attachments import AttachmentStore, TTL


def uploaded(store, home):
    d = store.create("runtime", "profile", home, SimpleNamespace(_closed=False))
    a = store.prepare(d, occurrence="o", request_id="r", name="report.txt", size=5, mime="text/plain")
    temporary = store.begin_upload(d, a.id)
    temporary.write_bytes(b"hello")
    store.finish_upload(d, a.id, temporary)
    os.utime(a.path, (time.time() - TTL - 10,) * 2)
    return d, a


def detach(store, d, a):
    store.claim(d, [a.id], "turn")
    d.updated -= TTL + 10
    store.expire()
    assert a.path.exists()  # Expiry never deletes submitted files itself.


def test_later_sweep_reclaims_completed_orphan_in_each_used_profile(tmp_path):
    store = AttachmentStore()
    paths = []
    for name in ("default", "other"):
        home = tmp_path / name
        home.mkdir()
        with SessionDB(home / "state.db"):
            pass
        store.cleanup_orphans(home)
        d, a = uploaded(store, home)
        detach(store, d, a)
        paths.append(a.path)
    store.sweep()
    assert all(not path.exists() for path in paths)


@pytest.mark.parametrize("reference", ["content", "metadata"])
def test_repeated_sweeps_preserve_references_until_canonical_session_deletion(tmp_path, reference):
    store = AttachmentStore()
    with SessionDB(tmp_path / "state.db") as db:
        db.create_session("durable", source="webui")
        d, a = uploaded(store, tmp_path)
        db.append_message("durable", "user", content="@file:" + str(a.path) if reference == "content" else "file",
                          display_metadata={"attachments": [{"id": a.id}]} if reference == "metadata" else None)
        detach(store, d, a)
        store.cleanup_orphans(tmp_path)
        store.cleanup_orphans(tmp_path)
        assert a.path.exists()
        db.delete_session("durable")
        store.cleanup_orphans(tmp_path)
        assert not a.path.exists()


@pytest.mark.parametrize("state", ["uploaded", "submitted"])
def test_live_ledger_protects_old_completed_files_without_durable_reference(tmp_path, state):
    store = AttachmentStore()
    with SessionDB(tmp_path / "state.db"):
        pass
    d, a = uploaded(store, tmp_path)
    if state == "submitted":
        store.claim(d, [a.id], "turn")
    store.cleanup_orphans(tmp_path)
    store.cleanup_orphans(tmp_path)
    assert a.path.read_bytes() == b"hello"


def test_uncertain_database_and_unknown_or_symlink_files_are_retained(tmp_path):
    store = AttachmentStore()
    d, a = uploaded(store, tmp_path)
    detach(store, d, a)
    store.cleanup_orphans(tmp_path)  # No database: ownership is unknown.
    assert a.path.exists()
    folder = a.path.parent
    unknown = folder / "user-owned.txt"
    unknown.write_text("private")
    linked = folder / ("f" * 32 + ".txt")
    linked.symlink_to(unknown)
    for path in (unknown,):
        os.utime(path, (time.time() - TTL - 10,) * 2)
    with SessionDB(tmp_path / "state.db"):
        pass
    for _ in range(2):
        store.cleanup_orphans(tmp_path)
    assert unknown.read_text() == "private"
    assert linked.is_symlink()
    assert not a.path.exists()


@pytest.mark.parametrize("transition", ["finish", "claim"])
def test_sweep_serializes_with_upload_completion_and_claim(tmp_path, monkeypatch, transition):
    store = AttachmentStore()
    with SessionDB(tmp_path / "state.db"):
        pass
    d, a = uploaded(store, tmp_path)
    if transition == "finish":
        a.state = "failed"
        temporary = store.begin_upload(d, a.id)
        temporary.write_bytes(b"hello")
    entered = threading.Event()
    release = threading.Event()
    attempting_sweep = threading.Event()
    swept = threading.Event()
    lock = store.lock

    class ObservedLock:
        def __enter__(self):
            if threading.current_thread().name == "sweeper":
                attempting_sweep.set()
            lock.acquire()

        def __exit__(self, *args):
            lock.release()

    store.lock = ObservedLock()
    original_item = store.item

    def blocked_item(*args):
        # Both transitions call item only after acquiring the store lock.
        entered.set()
        assert release.wait(10)
        return original_item(*args)

    monkeypatch.setattr(store, "item", blocked_item)

    def sweep():
        threading.current_thread().name = "sweeper"
        store.cleanup_orphans(tmp_path)
        swept.set()

    with ThreadPoolExecutor(max_workers=2) as workers:
        commit = workers.submit(store.finish_upload, d, a.id, temporary) if transition == "finish" else workers.submit(store.claim, d, [a.id], "turn")
        try:
            assert entered.wait(5)
            cleanup = workers.submit(sweep)
            assert attempting_sweep.wait(5)
            assert not swept.is_set()
            assert a.path.exists()
        finally:
            release.set()
        commit.result(timeout=5)
        cleanup.result(timeout=5)
    assert a.state == ("uploaded" if transition == "finish" else "submitted")
    assert a.path.read_bytes() == b"hello"


def test_expired_draft_still_protects_a_running_turn(tmp_path, monkeypatch):
    from tui_gateway import server
    store = AttachmentStore()
    with SessionDB(tmp_path / "state.db"):
        pass
    d, a = uploaded(store, tmp_path)
    store.claim(d, [a.id], "turn")
    d.updated -= TTL + 10
    monkeypatch.setitem(server._sessions, d.runtime_id, {"running": True})
    store.sweep()
    assert d.id in store.drafts
    assert a.path.exists()
    server._sessions[d.runtime_id]["running"] = False
    store.sweep()
    assert d.id not in store.drafts
    assert not a.path.exists()


@pytest.mark.asyncio
async def test_running_reaper_revisits_files_created_after_startup(tmp_path, monkeypatch):
    import asyncio
    from tui_gateway import attachment_http
    store = AttachmentStore()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(attachment_http, "store", store)
    path = None

    async def next_tick(_delay):
        nonlocal path
        if path is None:
            # The initial dashboard-home scan has finished. A different
            # profile begins using attachments during this same process.
            home = tmp_path / "profile"
            home.mkdir()
            with SessionDB(home / "state.db"):
                pass
            d, a = uploaded(store, home)
            detach(store, d, a)
            path = a.path
        else:
            assert not path.exists()
            raise asyncio.CancelledError

    monkeypatch.setattr(attachment_http.asyncio, "sleep", next_tick)
    with pytest.raises(asyncio.CancelledError):
        await attachment_http.run_attachment_reaper()
