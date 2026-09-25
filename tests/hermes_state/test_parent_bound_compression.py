"""Resume and display classify the immediate edge, not inherited markers."""
import pytest

from hermes_state import SessionDB
from hermes_state_common import _RESET_END_REASONS


@pytest.fixture
def db(tmp_path):
    with SessionDB(tmp_path / "state.db") as store:
        yield store


@pytest.mark.parametrize("marker", ["_branched_from", "_delegate_from", "_reset_from"])
def test_old_fork_id_resolves_its_own_compression_without_importing_root(db, marker):
    db.create_session("root", source="webui")
    db.append_message("root", "assistant", content="Root only")
    db.end_session("root", "compression")
    cfg = {marker: "root"}
    db.create_session("fork", source="webui", parent_session_id="root", model_config=cfg)
    db.append_message("fork", "assistant", content="Fork A")
    db.end_session("fork", "compression")
    db.create_session("tip", source="webui", parent_session_id="fork", model_config=cfg)
    db.append_message("tip", "assistant", content="Fork B")

    assert db.get_compression_tip("fork") == "tip"
    assert db.resolve_resume_session_id("fork") == "tip"
    assert db.get_compression_tip("root") == "root"
    assert db.resolve_resume_session_id("root") == "root"
    assert db._is_explicit_fork_child_row(db.get_session("fork"))
    assert not db._is_compression_child_row(db.get_session("fork"))
    assert db._is_compression_child_row(db.get_session("tip"))
    assert [r["content"] for r in db.get_display_messages("tip")] == ["Fork A", "Fork B"]


@pytest.mark.parametrize("marker,source", [
    ("_branched_from", "webui"), ("_delegate_from", "webui"),
    ("_reset_from", "webui"), (None, "tool"),
])
@pytest.mark.parametrize("sibling_first", [False, True])
def test_competing_forks_never_win_continuation_ranking(db, marker, source, sibling_first):
    db.create_session("parent", source="webui")
    db.append_message("parent", "assistant", content="Parent")
    db.end_session("parent", "compression")
    order = ["sibling", "continuation"] if sibling_first else ["continuation", "sibling"]
    for name in order:
        db.create_session(name, source=source if name == "sibling" else "webui",
                          parent_session_id="parent",
                          model_config={marker: "parent"} if name == "sibling" and marker else None)
        db.append_message(name, "assistant", content=name)
    # A sibling that is itself compression-ended ranks first unless excluded.
    db.end_session("sibling", "compression")
    assert db.get_compression_tip("parent") == "continuation"
    assert db.resolve_resume_session_id("parent") == "continuation"


def test_tool_only_child_cannot_hijack_parent_resume(db):
    db.create_session("parent", source="webui")
    db.end_session("parent", "compression")
    db.create_session("tool", source="tool", parent_session_id="parent")
    db.append_message("tool", "assistant", content="Tool only")
    assert db.get_compression_tip("parent") == "parent"
    assert db.resolve_resume_session_id("parent") == "parent"


@pytest.mark.parametrize("end_reason", [None, "normal", *_RESET_END_REASONS])
def test_noncompression_and_legacy_reset_edges_are_not_resume_continuations(db, end_reason):
    db.create_session("parent", source="webui", session_key="same-routing-key")
    if end_reason:
        db.end_session("parent", end_reason)
    db.create_session("child", source="webui", parent_session_id="parent", session_key="same-routing-key")
    db.append_message("child", "assistant", content="Independent child")
    assert db.get_compression_tip("parent") == "parent"
    assert db.resolve_resume_session_id("parent") == "parent"
    assert not db._is_compression_child_row(db.get_session("child"))


def test_normal_three_segment_chain_keeps_tip_and_full_display(db):
    for name, parent in [("A", None), ("B", "A"), ("C", "B")]:
        if parent:
            db.end_session(parent, "compression")
        db.create_session(name, source="webui", parent_session_id=parent)
        db.append_message(name, "assistant", content=name)
    assert db.get_compression_tip("A") == db.resolve_resume_session_id("A") == "C"
    assert [r["content"] for r in db.get_display_messages("C")] == ["A", "B", "C"]
