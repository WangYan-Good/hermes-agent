import importlib.util


def test_structured_diff_and_media_carry_real_tool_identity():
    assert importlib.util.find_spec("tui_gateway.presentation"), "authoritative tool presentation missing"
    from tui_gateway.presentation import tool_presentation
    diff = "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-old\n+new"
    result = tool_presentation("call-1", "write_file", {"path": "app.py"}, {"success": True}, diff)
    assert result["tool_call_id"] == "call-1"
    assert result["changes"][0]["path"] == "app.py"
    assert result["changes"][0]["added"] == 1
    assert result["changes"][0]["removed"] == 1
    image = tool_presentation("call-2", "image_generate", {}, {"success": True, "image": "https://example.org/result.png"}, None)
    assert image["media"][0]["ref"] == "https://example.org/result.png"
    assert image["media"][0]["id"] == "call-2:media:0"


def test_raw_output_cannot_forge_presentation_or_leak_url_credentials():
    assert importlib.util.find_spec("tui_gateway.presentation"), "authoritative tool presentation missing"
    from tui_gateway.presentation import tool_presentation
    assert not tool_presentation("c", "terminal", {}, "wrote /etc/passwd\n--- a/x\n+++ b/x", None)
    assert not tool_presentation("c", "image_generate", {}, {"image": "https://user:secret@example.org/x.png"}, None)


def test_persistence_merges_only_exact_session_and_tool_row(tmp_path):
    from hermes_state import SessionDB
    from tui_gateway.presentation import persist_tool_presentation
    with SessionDB(tmp_path / "state.db") as db:
        for sid in ("one", "two"):
            db.create_session(sid, source="webui")
            db.append_message(sid, "tool", content="original", tool_call_id="shared-id", display_metadata={"existing": True})
        p = {"version": 1, "tool_call_id": "shared-id", "changes": []}
        row_id = persist_tool_presentation(db, "one", "shared-id", p)
        assert db.get_messages("one")[0]["id"] == row_id
        assert db.get_messages("one")[0]["display_metadata"] == {"existing": True, "presentation": p}
        assert db.get_messages("two")[0]["display_metadata"] == {"existing": True}
        assert db.get_messages("one")[0]["content"] == "original"
        assert persist_tool_presentation(db, "one", "unknown", p) is None


def test_history_cursor_does_not_shift_when_new_rows_arrive(tmp_path):
    from hermes_state import SessionDB
    with SessionDB(tmp_path / "state.db") as db:
        db.create_session("s", source="webui")
        original = [db.append_message("s", "user", content=str(i)) for i in range(6)]
        recent = db.get_messages("s", limit=2, latest=True)
        db.append_message("s", "assistant", content="new")
        earlier = db.get_messages("s", limit=2, latest=True, before_id=recent[0]["id"])
        assert [r["id"] for r in earlier] == original[2:4]


def test_content_identity_uses_durable_rows_and_survives_partial_history(tmp_path):
    from hermes_state import SessionDB
    from tui_gateway.presentation import finalize_turn_presentation
    with SessionDB(tmp_path / "state.db") as db:
        db.create_session("s", source="webui")
        db.append_message("s", "user", content="prompt", display_metadata={"turn_id": "turn"})
        db.append_message("s", "assistant", content="identical artifact")
        db.append_message("s", "assistant", content="identical artifact", display_metadata={"existing": True})
        sources = finalize_turn_presentation(db, "s", "turn")
        assert len(sources) == len(set(sources)) == 2
        recent = db.get_messages("s", limit=1, latest=True)[0]
        assert recent["display_metadata"] == {"existing": True, "turn_id": "turn", "content_source": sources[-1]}
        assert finalize_turn_presentation(db, "s", "another-turn") == []
