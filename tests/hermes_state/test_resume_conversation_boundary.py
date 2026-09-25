"""Display and resume safety own the same compression conversation segments."""
import pytest

from hermes_state import SessionDB, SessionResumeTooLargeError


@pytest.fixture
def db(tmp_path):
    with SessionDB(tmp_path / "state.db") as store:
        yield store


def seed_fork(db, marker="_branched_from"):
    db.create_session("root", source="webui")
    db.append_messages_batch("root", [
        {"role": "assistant", "content": f"Excluded root {i}"} for i in range(30)
    ])
    db.end_session("root", "compression")
    config = {marker: "root"}
    db.create_session("fork", source="webui", parent_session_id="root", model_config=config)
    for role in ("user", "assistant"):
        db.append_message("fork", role, content=f"Fork {role}")
    db.end_session("fork", "compression")
    db.create_session("tip", source="webui", parent_session_id="fork", model_config=config)
    for role in ("user", "assistant"):
        db.append_message("tip", role, content=f"Tip {role}")


@pytest.mark.parametrize("marker", ["_branched_from", "_delegate_from", "_reset_from"])
def test_compressed_fork_uses_one_boundary_for_display_prefix_and_safety(db, marker):
    seed_fork(db, marker)
    model, display = db.get_resume_conversations("tip")
    assert [r["content"] for r in model] == ["Tip user", "Tip assistant"]
    assert [r["content"] for r in display] == ["Fork user", "Fork assistant", "Tip user", "Tip assistant"]
    assert [r["_row_id"] for r in display] == [r["id"] for r in db.get_display_messages("tip")]
    assert [r["content"] for r in db.get_ancestor_display_prefix("tip")] == ["Fork user", "Fork assistant"]
    assert [r["content"] for r in db.get_messages_as_conversation("tip", include_ancestors=True)] == [
        "Fork user", "Fork assistant", "Tip user", "Tip assistant",
    ]
    assert db.get_resume_message_count("tip") == 4
    assert db.assert_resume_safe("tip", max_messages=4) == 4
    with pytest.raises(SessionResumeTooLargeError) as error:
        db.assert_resume_safe("tip", max_messages=3)
    assert error.value.message_count == 4
    assert error.value.limit == 3


def test_safety_does_not_charge_large_original_root_to_small_branch(db):
    seed_fork(db)
    assert db.get_resume_message_count("root") == 30
    assert db.assert_resume_safe("tip", max_messages=5) == 4


def test_safety_still_rejects_the_branch_conversation_itself(db):
    seed_fork(db)
    with pytest.raises(SessionResumeTooLargeError) as error:
        db.assert_resume_safe("tip", max_messages=2)
    # The bounded count stops at limit+1; it does not scan the original root.
    assert error.value.message_count == 3


def test_normal_compression_keeps_all_segments_but_model_uses_only_tip(db):
    parent = None
    for name in ("A", "B", "C"):
        if parent:
            db.end_session(parent, "compression")
        db.create_session(name, source="webui", parent_session_id=parent)
        for role in ("user", "assistant"):
            db.append_message(name, role, content=f"{name} {role}")
        parent = name
    model, display = db.get_resume_conversations("C")
    assert [r["content"] for r in model] == ["C user", "C assistant"]
    assert [r["content"] for r in display] == ["A user", "A assistant", "B user", "B assistant", "C user", "C assistant"]
    assert [r["content"] for r in db.get_ancestor_display_prefix("C")] == ["A user", "A assistant", "B user", "B assistant"]
    assert db.get_resume_message_count("C") == db.assert_resume_safe("C", max_messages=6) == 6


def test_uncompressed_branch_owns_its_copy_without_appending_root_rows(db):
    db.create_session("root", source="webui")
    db.append_message("root", "user", content="Copied question")
    db.end_session("root", "compression")
    db.create_session("branch", source="webui", parent_session_id="root", model_config={"_branched_from": "root"})
    db.append_message("branch", "user", content="Copied question")
    model, display = db.get_resume_conversations("branch")
    assert [r["content"] for r in display] == ["Copied question"]
    assert [r["_row_id"] for r in display] == [r["id"] for r in db.get_messages("branch")]
    assert model == display
    assert db.get_ancestor_display_prefix("branch") == []
    assert db.get_resume_message_count("branch") == db.assert_resume_safe("branch", max_messages=1) == 1


def test_tool_child_has_no_parent_display_prefix_or_safety_count(db):
    db.create_session("root", source="webui")
    db.append_message("root", "user", content="Excluded root")
    db.end_session("root", "compression")
    db.create_session("tool", source="tool", parent_session_id="root")
    db.append_message("tool", "user", content="Tool owned")
    model, display = db.get_resume_conversations("tool")
    assert [r["content"] for r in display] == ["Tool owned"]
    assert model == display
    assert db.get_ancestor_display_prefix("tool") == []
    assert db.get_resume_message_count("tool") == 1


def test_compressed_branch_preserves_rich_rows_and_replayed_user_dedupe(db):
    db.create_session("root", source="webui")
    db.append_message("root", "assistant", content="Excluded root")
    db.end_session("root", "compression")
    cfg = {"_branched_from": "root"}
    db.create_session("branch", source="webui", parent_session_id="root", model_config=cfg)
    db.append_message("branch", "user", content="Replay me")
    db.end_session("branch", "compression")
    db.create_session("tip", source="webui", parent_session_id="branch", model_config=cfg)
    db.append_message("tip", "user", content=" Replay me ")
    call = {"id": "real-call", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}
    db.append_message("tip", "assistant", content="", tool_calls=[call], reasoning="Reasoning",
                      api_content=" Exact API bytes ", display_kind="artifact", display_metadata={"turn_id": "turn"})
    db.append_message("tip", "tool", content="Result", tool_call_id="real-call", tool_name="read_file")
    db.append_message("tip", "assistant", content="Done")
    model, display = db.get_resume_conversations("tip")
    assert [r["content"] for r in display] == ["Replay me", "", "Result", "Done"]
    assert display[0]["_row_id"] == db.get_messages("branch")[0]["id"]
    assert model[0]["_row_id"] == db.get_messages("tip")[0]["id"]
    rich = display[1]
    assert rich["tool_calls"] == [call]
    assert rich["reasoning"] == "Reasoning"
    assert rich["api_content"] == " Exact API bytes "
    assert rich["display_kind"] == "artifact"
    assert rich["display_metadata"] == {"turn_id": "turn"}
    assert display[2]["tool_call_id"] == "real-call"
    # Safety counts materialized rows before display dedupe, as before.
    assert db.get_resume_message_count("tip") == 5


def test_tip_alternation_repair_does_not_remove_display_or_duplicate_prefix(db):
    seed_fork(db)
    db.append_message("tip", "assistant", content="Verification candidate", finish_reason="verification_required")
    db.append_message("tip", "assistant", content="Verified reply", finish_reason="stop")
    model, display = db.get_resume_conversations("tip")
    assert not any(r["content"] == "Verification candidate" for r in model)
    assert not any(r["content"].startswith("Fork") for r in model)
    assert [r["content"] for r in display] == [
        "Fork user", "Fork assistant", "Tip user", "Tip assistant", "Verification candidate", "Verified reply",
    ]
    assert [r["content"] for r in db.get_ancestor_display_prefix("tip")] == ["Fork user", "Fork assistant"]
