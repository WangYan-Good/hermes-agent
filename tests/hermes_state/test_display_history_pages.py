"""Bounded raw display pages follow compression, never arbitrary parent links."""
import pytest

from hermes_state import SessionDB


@pytest.fixture
def db(tmp_path):
    with SessionDB(tmp_path / "state.db") as db:
        yield db


def chain(db, names=("A", "B", "C")):
    parent = None
    for name in names:
        if parent:
            db.end_session(parent, "compression")
        db.create_session(name, source="webui", parent_session_id=parent)
        db.append_message(name, "user", content=f"Question {name}")
        db.append_message(name, "assistant", content=f"Answer {name}")
        parent = name
    return parent


@pytest.mark.parametrize("names", [("A", "B"), ("A", "B", "C")])
def test_display_matches_resume_lineage_with_raw_identities(db, names):
    tip = chain(db, names)
    assert db.resolve_resume_session_id("A") == tip
    rows = db.get_display_messages(tip, limit=100)
    _, canonical = db.get_resume_conversations(tip)
    assert [r["id"] for r in rows] == [r["_row_id"] for r in canonical]
    assert [r["content"] for r in rows] == [r["content"] for r in canonical]
    assert {r["session_id"] for r in rows} == set(names)


def test_keyset_crosses_segments_and_ignores_new_appends(db):
    tip = chain(db)
    expected = db.get_display_messages(tip, limit=100)
    newest = db.get_display_messages(tip, limit=3)
    cursor = newest[0]["id"]
    older = db.get_display_messages(tip, limit=3, before_id=cursor)
    db.append_message(tip, "assistant", content="Appended later")
    assert db.get_display_messages(tip, limit=3, before_id=cursor) == older
    assert older + newest == expected
    assert len({r["id"] for r in older + newest}) == len(expected)


@pytest.mark.parametrize("marker,source", [("_branched_from", "webui"), ("_delegate_from", "webui"), ("_reset_from", "webui"), (None, "tool"), (None, "webui")])
def test_noncompression_parents_never_leak(db, marker, source):
    chain(db, ("parent",))
    db.create_session("fork", source=source, parent_session_id="parent",
                      model_config={marker: "parent"} if marker else None)
    db.append_message("fork", "user", content="Copied or independent")
    db.append_message("parent", "assistant", content="Parent after fork")
    if marker:
        db.end_session("parent", "compression")  # Marker still wins.
    assert [r["content"] for r in db.get_display_messages("fork")] == ["Copied or independent"]


def test_branch_can_itself_compress_without_crossing_its_fork(db):
    chain(db, ("parent",))
    cfg = {"_branched_from": "parent"}
    db.create_session("branch", source="webui", parent_session_id="parent", model_config=cfg)
    db.append_message("branch", "user", content="Branch history")
    db.end_session("branch", "compression")
    db.create_session("tip", source="webui", parent_session_id="branch", model_config=cfg)
    db.append_message("tip", "assistant", content="Branch continuation")
    assert [r["content"] for r in db.get_display_messages("tip")] == ["Branch history", "Branch continuation"]


def test_compacted_dedupe_precedes_cursor_and_preserves_rich_sidecars(db):
    parent = None
    for sid in ("A", "B"):
        if parent:
            db.end_session(parent, "compression")
        db.create_session(sid, source="webui", parent_session_id=parent)
        db.append_message(sid, "user", content=f"Question {sid}")
        db.append_message(sid, "assistant", content="```html\n<h1>source</h1>\n```",
                          reasoning="reason", api_content="exact API bytes",
                          display_kind="artifact", display_metadata={"turn_id": sid})
        # Same signature as the canonical compaction protected-tail copy.
        db._execute_write(lambda conn: conn.execute(
            "INSERT INTO messages (session_id, role, content, timestamp, active, compacted) "
            "SELECT session_id, role, content, timestamp, 0, 1 FROM messages WHERE session_id = ?", (sid,)))
        parent = sid
    expected = sorted(db.get_messages("A", include_compacted=True) + db.get_messages("B", include_compacted=True), key=lambda r: r["id"])
    rows, before = [], None
    while page := db.get_display_messages("B", limit=2, before_id=before, include_compacted=True):
        rows = page + rows
        before = page[0]["id"]
    assert rows == expected
    rich = [r for r in rows if r["display_kind"] == "artifact"]
    assert len(rich) == 2
    assert all(r["reasoning"] == "reason" and r["api_content"] == "exact API bytes" for r in rich)


def test_replayed_user_is_deduped_across_page_boundary_on_backend(db):
    db.create_session("A", source="webui")
    db.append_message("A", "user", content="Repeat me")
    db.end_session("A", "compression")
    db.create_session("B", source="webui", parent_session_id="A")
    db.append_message("B", "user", content=" Repeat me ")
    db.append_message("B", "assistant", content="Response")
    _, canonical = db.get_resume_conversations("B")
    latest = db.get_display_messages("B", limit=1)
    older = db.get_display_messages("B", limit=1, before_id=latest[0]["id"])
    assert [r["id"] for r in older + latest] == [r["_row_id"] for r in canonical]


def test_archived_display_rows_are_independent_of_lineage_selection(db):
    parent = None
    for sid in ("A", "B"):
        if parent:
            db.end_session(parent, "compression")
        db.create_session(sid, source="webui", parent_session_id=parent)
        db.append_message(sid, "user", content=f"Question {sid}")
        db.append_message(sid, "assistant", content=f"Answer {sid}")
        db.archive_and_compact(sid, [{"role": "assistant", "content": f"Summary {sid}"}])
        parent = sid
    active = db.get_display_messages("B", include_compacted=False)
    assert [r["content"] for r in active] == ["Summary A", "Summary B"]
    rows, before = [], None
    while page := db.get_display_messages("B", limit=2, before_id=before, include_compacted=True):
        rows = page + rows
        before = page[0]["id"]
    assert [r["content"] for r in rows] == ["Question A", "Answer A", "Summary A", "Question B", "Answer B", "Summary B"]
    assert len({r["id"] for r in rows}) == 6


def test_page_decodes_only_bounded_selected_rows(db, monkeypatch):
    tip = chain(db)
    db.append_messages_batch(tip, [
        {"role": role, "content": f"Turn {i}"}
        for i in range(1000) for role in ("user", "assistant")
    ])
    # Full conversation/materialization helpers must never service this API.
    def forbidden(*args, **kwargs):
        raise AssertionError("unbounded transcript read")
    monkeypatch.setattr(db, "get_resume_conversations", forbidden)
    monkeypatch.setattr(db, "get_messages", forbidden)
    decode = db._decode_message_rows
    def bounded_decode(rows):
        assert len(rows) <= 1
        return decode(rows)
    monkeypatch.setattr(db, "_decode_message_rows", bounded_decode)
    assert len(db.get_display_messages(tip, limit=1)) == 1
    assert db.get_display_messages(tip, limit=0) == []
    with pytest.raises(ValueError):
        db.get_display_messages(tip, limit=501)


def test_tool_result_and_rich_identity_across_lineage_page_boundary(db):
    db.create_session("A", source="webui")
    db.append_message("A", "assistant", content="", tool_calls=[
        {"id": "real-call", "type": "function", "function": {"name": "read_file", "arguments": '{"path":"report.txt"}'}}
    ], reasoning="Inspect file", display_metadata={"turn_id": "turn"})
    db.end_session("A", "compression")
    db.create_session("B", source="webui", parent_session_id="A")
    db.append_message("B", "tool", content="Result", tool_call_id="real-call", tool_name="read_file",
                      display_metadata={"turn_id": "turn", "presentation": {"version": 1, "tool_call_id": "real-call"}})
    result = db.get_display_messages("B", limit=1)[0]
    call = db.get_display_messages("B", limit=1, before_id=result["id"])[0]
    assert call["tool_calls"][0]["id"] == result["tool_call_id"] == "real-call"
    assert call["reasoning"] == "Inspect file"
    assert result["display_metadata"]["presentation"]["tool_call_id"] == "real-call"
    assert call["id"] < result["id"]


def test_repeated_user_text_after_an_assistant_is_a_distinct_turn(db):
    db.create_session("A", source="webui")
    for role, content in [("user", "same"), ("assistant", "answer"), ("user", "same")]:
        db.append_message("A", role, content=content)
    db.end_session("A", "compression")
    db.create_session("B", source="webui", parent_session_id="A")
    db.append_message("B", "user", content="same")
    db.append_message("B", "assistant", content="second answer")
    _, display = db.get_resume_conversations("B")
    rows, before = [], None
    while page := db.get_display_messages("B", limit=1, before_id=before):
        rows = page + rows
        before = page[0]["id"]
    assert [r["id"] for r in rows] == [r["_row_id"] for r in display]
    assert [r["content"] for r in rows].count("same") == 2
