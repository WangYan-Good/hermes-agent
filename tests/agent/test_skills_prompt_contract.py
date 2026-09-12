"""P5 contracts exercise real discovery, assembly and loading in isolated homes."""

import json
import inspect
from types import SimpleNamespace
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from agent import prompt_builder as pb, system_prompt as sp, skill_utils as sku
from tests.agent.test_system_prompt import _make_agent


def write_skill(root, category, name, description=None):
    directory = root / category / name
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "SKILL.md"
    path.write_text(
        f"---\nname: {name}\ndescription: {description or 'Workflow for ' + name}\n---\n"
        f"# {name}\nExecute the {name} workflow.\n", encoding="utf-8",
    )
    return path


@pytest.fixture
def library(tmp_path, monkeypatch):
    home = tmp_path / "home"
    skills = home / "skills"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "pyproject.toml").write_text('[project]\nname="fixture"\n', encoding="utf-8")
    (workspace / "AGENTS.md").write_text("AUTHORITY: preserve the public API.\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("TERMINAL_CWD", str(workspace))
    for category in ("coding", "github", "devops", "mcp", "security", "data-science",
                     "research", "diagramming", "testing", "database", "custom",
                     "health", "social-media/twitter"):
        write_skill(skills, category, category.replace("/", "-") + "-skill")
    pb.clear_skills_system_prompt_cache()
    yield skills
    pb.clear_skills_system_prompt_cache()


def render(skills, mode="auto", tools=("skills_list", "skill_view", "skill_manage")):
    agent = _make_agent(
        model="gpt-5", provider="openai", platform="cli",
        valid_tool_names=["read_file", "terminal", *tools],
        _task_completion_guidance=True, _tool_use_enforcement="auto",
        _bot_mode_protocol=False,
    )
    with (
        patch("agent.coding_context._coding_mode", return_value=mode),
        patch("agent.system_prompt._agent_skills_dir", return_value=skills),
        patch("run_agent.load_soul_md", return_value=""),
        patch("run_agent.build_nous_subscription_prompt", return_value=""),
        patch("run_agent.build_environment_hints", return_value=""),
        patch("hermes_time.now", return_value=datetime(2026, 9, 12, tzinfo=timezone.utc)),
        patch("hermes_time.get_timezone", return_value=timezone.utc),
    ):
        return sp.build_system_prompt_parts(agent)


def assert_owners(parts, tools):
    whole = "\n\n".join(parts.values())
    assert whole.count("skill_manage(action='patch')") == ("skill_manage" in tools)
    assert whole.count("record it with skill_manage") == ("skill_manage" in tools)
    assert whole.count("## Skill Safety Rule") == ("skill_view" in tools)
    if "skill_view" in tools:
        assert "\n\n## Skill Safety Rule\n" in whole
    assert whole.count("authoritative reference") == 1
    assert whole.count("Load the `hermes-agent` skill") == 1
    index = parts["volatile"]
    assert "skill_manage" not in index
    assert "hermes-agent` skill" not in index


@pytest.mark.parametrize("tools", [(), ("skills_list",), ("skill_view",), ("skill_manage",),
                                  ("skill_view", "skill_manage"),
                                  ("skills_list", "skill_view", "skill_manage")])
def test_semantic_owners_follow_independent_tool_gates(library, tools):
    assert_owners(render(library, tools=tools), tools)


@pytest.mark.parametrize("mode", ["off", "auto", "on"])
def test_default_modes_preserve_every_description(library, mode):
    parts = render(library, mode)
    assert "[names only]" not in parts["volatile"]
    for path in library.rglob("SKILL.md"):
        assert "Workflow for " + path.parent.name in parts["volatile"]


def assert_focus(parts):
    index = parts["volatile"]
    for name in ("health-skill", "social-media-twitter-skill"):
        assert name in index
        assert "Workflow for " + name not in index
    for category in ("coding", "github", "devops", "mcp", "security", "data-science",
                     "research", "diagramming", "testing", "database", "custom"):
        assert "Workflow for " + category + "-skill" in index
    assert "descriptions are intentionally omitted" in index
    assert "still exist" in index and "loadable" in index
    assert "inspect the relevant skill" in index
    assert "unavailable" not in index.lower()
    assert "<available_skills>" not in parts["stable"] + parts["context"]


def test_focus_preserves_discovery_and_loadability(library):
    from tools.skills_tool import skill_view

    assert_focus(render(library, "focus"))
    result = json.loads(skill_view(name="health-skill"))
    assert "Execute the health-skill workflow." in result["content"]


def test_focus_collision_keeps_both_warnings_and_bare_name_fails(library):
    from tools.skills_tool import skill_view

    org = library / sku.ORG_MIRROR_DIR_NAME
    write_skill(org / "org-1", "health", "health-skill")
    (org / sku.ORG_ACTIVE_MARKER).write_text("org-1", encoding="utf-8")
    parts = render(library, "focus")
    assert parts["volatile"].count("[name collision") == 2
    assert "[org-shared" in parts["volatile"]
    assert "load via category path" in parts["volatile"]
    assert "error" in json.loads(skill_view(name="health-skill"))


def test_cold_warm_and_cross_mode_bytes(library):
    auto = render(library)
    focus = render(library, "focus")
    assert auto != focus
    assert render(library) == auto
    assert render(library, "focus") == focus
    pb.clear_skills_system_prompt_cache()
    assert render(library) == auto  # disk snapshot
    assert render(library, "focus") == focus
    assert auto["stable"] == focus["stable"]
    assert auto["context"] == focus["context"]


def test_skill_change_invalidates_only_volatile_tail(library):
    before = render(library)
    write_skill(library, "health", "health-skill", "A newly revised health workflow")
    pb.clear_skills_system_prompt_cache()
    after = render(library)
    assert "A newly revised health workflow" in after["volatile"]
    assert before["volatile"] != after["volatile"]
    assert before["stable"] == after["stable"]
    assert before["context"] == after["context"]


def test_context_authority_survives_all_modes(library):
    for mode in ("off", "auto", "on", "focus"):
        assert "AUTHORITY: preserve the public API." in render(library, mode)["context"]


def test_empty_library_has_no_index(library, tmp_path):
    empty = tmp_path / "empty" / "skills"
    empty.mkdir(parents=True)
    assert "<available_skills>" not in render(empty)["volatile"]


def test_tool_and_toolset_requirements_do_not_cross_pollute_cache(library):
    path = write_skill(library, "custom", "conditional-skill")
    path.write_text(
        "---\nname: conditional-skill\ndescription: Conditional workflow\nmetadata:\n"
        "  hermes:\n    requires_tools: [terminal]\n    requires_toolsets: [terminal]\n---\nbody\n",
        encoding="utf-8",
    )
    for tools, toolsets in (({"terminal"}, {"terminal"}), (set(), {"terminal"}),
                            ({"terminal"}, set()), ({"terminal"}, {"terminal"})):
        index = pb.build_skills_system_prompt(available_tools=tools, available_toolsets=toolsets,
                                             skills_dir_override=library, compact_categories=frozenset({"health"}))
        assert ("conditional-skill" in index) == bool(tools and toolsets)


def test_platform_disabled_lists_do_not_cross_pollute_cache(library, monkeypatch):
    (library.parent / "config.yaml").write_text(
        "skills:\n  platform_disabled:\n    telegram: [health-skill]\n", encoding="utf-8",
    )
    for platform in ("cli", "telegram", "cli"):
        monkeypatch.setattr(pb, "_current_session_platform_hint", lambda: platform)
        index = pb.build_skills_system_prompt(skills_dir_override=library,
                                             compact_categories=frozenset({"health"}))
        assert ("health-skill" in index) == (platform == "cli")


def test_org_switch_invalidates_snapshot(library):
    org = library / sku.ORG_MIRROR_DIR_NAME
    for name in ("first", "second"):
        write_skill(org / name, "health", name + "-shared")
    marker = org / sku.ORG_ACTIVE_MARKER
    for active, absent in (("first", "second"), ("second", "first"), ("first", "second")):
        marker.write_text(active, encoding="utf-8")
        pb.clear_skills_system_prompt_cache()
        index = render(library, "focus")["volatile"]
        assert active + "-shared" in index
        assert absent + "-shared" not in index


def test_protected_memory_user_and_plugin_content(library, monkeypatch):
    from hermes_cli.plugins import RenderedPluginSystemPromptSection

    original = _make_agent

    def with_memory(**kwargs):
        return original(**kwargs, _memory_enabled=True, _user_profile_enabled=True,
            _memory_store=SimpleNamespace(format_for_system_prompt=lambda kind: f"PROTECTED {kind}"),
            _plugin_system_prompt_sections_snapshot=(RenderedPluginSystemPromptSection(
                id="fixture.rules", content="PROTECTED plugin", position="after_memory", plugin="fixture"),))

    monkeypatch.setattr(__import__(__name__, fromlist=["_make_agent"]), "_make_agent", with_memory)
    for mode in ("off", "auto", "focus", "on"):
        volatile = render(library, mode)["volatile"]
        for kind in ("memory", "user", "plugin"):
            assert f"PROTECTED {kind}" in volatile


def mutate_function(monkeypatch, module, name, old, new):
    """Compile a single mutated function in memory; never edit production files."""
    source = inspect.getsource(getattr(module, name))
    assert source.count(old) == 1, (name, old)
    namespace = dict(vars(module))
    exec(compile(source.replace(old, new), f"<p5-mutant:{name}>", "exec"), namespace)
    monkeypatch.setattr(module, name, namespace[name])


@pytest.mark.parametrize("mutation", range(1, 15))
def test_mutations_are_rejected(library, monkeypatch, tmp_path, mutation):
    from agent import coding_context as cc

    inner = "_build_skills_system_prompt_inner"
    check = lambda: assert_focus(render(library, "focus"))
    if mutation == 1:  # delete demoted skill names
        mutate_function(monkeypatch, pb, inner,
                        'names.append(" ".join([name, *annotations]))', 'pass')
    elif mutation in (2, 3, 4):
        categories = frozenset(cc._NON_CODING_SKILL_CATEGORIES)
        if mutation == 3:
            categories |= {"custom"}
        if mutation == 4:
            categories |= {"github"}
        monkeypatch.setattr(cc, "coding_compact_skill_categories", lambda **kwargs: categories)
        if mutation == 2:
            check = lambda: test_default_modes_preserve_every_description(library, "auto")
    elif mutation == 5:  # remove rendering policy from LRU key
        mutate_function(monkeypatch, pb, inner,
                        "tuple(sorted(compact_categories or ())),", "# missing policy")
        check = lambda: test_cold_warm_and_cross_mode_bytes(library)
    elif mutation == 6:  # ignore metadata manifest changes
        mutate_function(monkeypatch, pb, "_load_skills_snapshot",
                        'snapshot.get("manifest") != _build_skills_manifest(skills_dir)', 'False')
        check = lambda: test_skill_change_invalidates_only_volatile_tail(library)
    elif mutation == 7:  # ignore explicit profile home on a bare build thread
        from tests.agent.test_bot_profile_prompt_isolation import (
            test_skills_prompt_scoped_to_override_not_ambient_home,
        )
        mutate_function(monkeypatch, pb, "build_skills_system_prompt",
                        "if skills_dir_override is not None:", "if False:")
        check = lambda: test_skills_prompt_scoped_to_override_not_ambient_home(tmp_path, monkeypatch)
    elif mutation == 8:
        mutate_function(monkeypatch, pb, inner,
                        'collided = len(name_owners.get(fm, set())) > 1', 'collided = False')
        check = lambda: test_focus_collision_keeps_both_warnings_and_bare_name_fails(library)
    elif mutation == 9:
        monkeypatch.setattr(sp, "SKILLS_GUIDANCE", "")
        check = lambda: assert_owners(render(library), ("skill_view", "skill_manage"))
    elif mutation == 10:
        mutate_function(monkeypatch, pb, inner, '"## Skills (mandatory)\\n"',
                        '"## Skills (mandatory)\\n" + SKILLS_GUIDANCE +')
        check = lambda: assert_owners(render(library), ("skill_view", "skill_manage"))
    elif mutation == 11:
        mutate_function(monkeypatch, sp, "build_system_prompt_parts",
                        "volatile_parts.append(skills_prompt)", "stable_parts.append(skills_prompt)")
    elif mutation == 12:
        mutate_function(monkeypatch, sp, "build_system_prompt_parts",
                        "if agent._memory_store:", "if False:")
        check = lambda: test_protected_memory_user_and_plugin_content(library, monkeypatch)
    elif mutation == 13:
        mutate_function(monkeypatch, sp, "build_system_prompt_parts",
                        "context_parts.append(context_files_prompt)", "pass")
        check = lambda: test_context_authority_survives_all_modes(library)
    elif mutation == 14:
        mutate_function(monkeypatch, pb, inner, '"still exist and are loadable with skill_view(name). If your task falls "',
                        '"are unavailable. If your task falls "')
    with pytest.raises(AssertionError):
        check()
