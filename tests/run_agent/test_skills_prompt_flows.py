"""Scripted provider requests exercise real skill loading, not model intelligence."""

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent import prompt_builder as pb
from hermes_state import SessionDB
from tests.agent.test_skills_prompt_contract import write_skill
from tests.run_agent.test_semantic_progress_guard import ProgressProvider, call, json_ok
from tests.run_agent.test_transport_recovery_e2e import e2e_agent  # noqa: F401


@pytest.mark.parametrize("case", ["coding", "explicit-focus", "recalled", "no-relevant"])
def test_skill_discovery_load_and_finalize(e2e_agent, tmp_path, monkeypatch, case):
    from tools.skills_tool import SKILL_VIEW_SCHEMA, skill_view

    home = tmp_path / "home"
    skills = home / "skills"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "pyproject.toml").write_text('[project]\nname="fixture"\n', encoding="utf-8")
    write_skill(skills, "coding", "coding-guide")
    write_skill(skills, "health", "health-guide")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("TERMINAL_CWD", str(workspace))
    pb.clear_skills_system_prompt_cache()
    name = "coding-guide" if case == "coding" else "health-guide"
    question = {
        "coding": "Use coding-guide to implement this code change.",
        "explicit-focus": "Use health-guide to plan my exercise routine.",
        "recalled": "Use the workflow named in my memory.",
        "no-relevant": "Explain what a mutex is; no library skill matches this conceptual question.",
    }[case]
    script = [] if case == "no-relevant" else [call(tool="skill_view", args={"name": name})]
    script.append(json_ok(content="Completed the requested response."))
    server = ProgressProvider(script)
    db = SessionDB(db_path=home / "state.db")
    try:
        agent = e2e_agent(server)
        agent._session_db = db
        agent.model = "gpt-5"
        agent.platform = "cli"
        agent.stream_delta_callback = None
        agent._disable_streaming = True
        agent.valid_tool_names = {"read_file", "terminal", "skill_view"}
        agent.tools = [{"type": "function", "function": SKILL_VIEW_SCHEMA}]
        agent._task_completion_guidance = True
        agent._tool_use_enforcement = "auto"
        if case == "recalled":
            agent._memory_store = SimpleNamespace(format_for_system_prompt=lambda kind: "Recall health-guide.")
            agent._memory_enabled = True
            agent._user_profile_enabled = False
        with patch("agent.coding_context._coding_mode", return_value="auto" if case == "coding" else "focus"):
            agent._cached_system_prompt = agent._build_system_prompt()
        loaded = []

        def execute(tool, args, *unused, **kwargs):
            assert tool == "skill_view"
            result = skill_view(**args)
            assert f"Execute the {name} workflow." in json.loads(result)["content"]
            loaded.append(result)
            return result

        with patch("run_agent.handle_function_call", side_effect=execute), patch.object(agent, "_cleanup_task_resources"):
            result = agent.run_conversation(question)
        assert result["final_response"] == "Completed the requested response."
        assert len(loaded) == (case != "no-relevant")
        assert len(server.bodies) == len(script)
        prompt = server.bodies[0]["messages"][0]["content"]
        assert name in prompt
        if case == "coding":
            assert "Workflow for coding-guide" in prompt
        else:
            assert "Workflow for health-guide" not in prompt
        if case == "recalled":
            assert "Recall health-guide." in prompt
        if loaded:
            assert any(f"Execute the {name} workflow." in str(m.get("content", ""))
                       for m in server.bodies[1]["messages"] if m["role"] == "tool")
        assert all(body["messages"][0]["content"] == prompt for body in server.bodies)
        assert server.overruns == 0
    finally:
        db.close()
        server.close()
        pb.clear_skills_system_prompt_cache()
