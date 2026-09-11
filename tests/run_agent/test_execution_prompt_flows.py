"""Scripted provider + real loop evidence; no external model intelligence claim."""

import json
import subprocess
import sys
from unittest.mock import patch

import pytest

from tests.run_agent.test_semantic_progress_guard import (
    ProgressProvider, assert_durable_clean, call, json_ok, loop, e2e_agent, nudge_count,
)


def configure_prompt(agent, *, coding=True):
    agent.model = "gpt-5"
    agent.platform = "cli"
    agent._task_completion_guidance = True
    agent._tool_use_enforcement = "auto"
    with patch("agent.coding_context._coding_mode", return_value="on" if coding else "off"):
        agent._cached_system_prompt = agent._build_system_prompt()
    assert "# Execution discipline" in agent._cached_system_prompt
    assert ("coding agent pairing" in agent._cached_system_prompt) == coding


@pytest.mark.parametrize("fail_first", [False, True])
def test_inspect_edit_verify_finalize(loop, tmp_path, monkeypatch, fail_first):
    monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))
    artifact = tmp_path / "answer.py"
    artifact.write_text("answer = 0\n", encoding="utf-8")
    script = [call(str(artifact), call_id="inspect")]
    if fail_first:
        script.append(call(tool="patch", args={"path": str(artifact)}, call_id="failed-edit"))
    script.extend([
        call(tool="write_file", args={"path": str(artifact), "content": "answer = 42\n"}, call_id="edit"),
        call(tool="terminal", args={"command": "verify answer"}, call_id="verify"),
        json_ok(content="Changed answer to 42. Verification passed."),
    ])
    verified = []

    def execute(name, args):
        if name == "read_file":
            return artifact.read_text(encoding="utf-8")
        if name == "patch":
            return json.dumps({"error": "Patch context did not match; no changes applied."})
        if name == "write_file":
            artifact.write_text(args["content"], encoding="utf-8")
            return json.dumps({"success": True, "bytes_written": len(args["content"])})
        result = subprocess.run(
            [sys.executable, "-c", "import runpy,sys; assert runpy.run_path(sys.argv[1])['answer'] == 42", str(artifact)],
            capture_output=True, text=True, check=False,
        )
        verified.append(result.returncode)
        return json.dumps({"exit_code": result.returncode, "output": result.stdout + result.stderr})

    agent, server, result, executions, db = loop(script, output=execute, configure=configure_prompt)
    assert verified == [0]
    assert artifact.read_text(encoding="utf-8") == "answer = 42\n"
    assert len(server.bodies) == len(script)
    assert "coding agent pairing" in json.dumps(server.bodies[0]["messages"][0])
    assert len(executions) == len(script) - 1
    assert result["final_response"] == "Changed answer to 42. Verification passed."
    assert nudge_count(server) == 0
    assert_durable_clean(agent, result, db)


def test_genuine_blocker_can_finalize(loop):
    final = "The required project identifier is unavailable. Please provide it; no change was made."
    agent, server, result, executions, db = loop([
        call("missing-project", call_id="lookup"), json_ok(content=final),
    ], output=json.dumps({"error": "Required project identifier unavailable"}), configure=configure_prompt)
    assert result["final_response"] == final
    assert len(executions) == 1
    assert len(server.bodies) == 2
    assert nudge_count(server) == 0
    assert_durable_clean(agent, result, db)


def test_conceptual_question_finalizes_without_tools(e2e_agent):
    answer = "A mutex allows one owner at a time to access a shared resource."
    server = ProgressProvider([json_ok(content=answer)])
    try:
        agent = e2e_agent(server)
        agent.stream_delta_callback = None
        agent._disable_streaming = True
        agent.valid_tool_names.update({"read_file", "terminal"})
        configure_prompt(agent, coding=False)
        with patch("run_agent.handle_function_call") as execute, patch.object(agent, "_cleanup_task_resources"):
            result = agent.run_conversation("What is a mutex?")
        assert result["final_response"] == answer
        execute.assert_not_called()
        assert len(server.bodies) == 1
        assert nudge_count(server) == 0
    finally:
        server.close()
