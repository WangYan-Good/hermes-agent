"""Real prompt assembly contracts and in-memory mutation sensitivity gates."""

import re
from contextlib import ExitStack
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from agent import prompt_builder as pb
from agent import system_prompt as sp
from agent.semantic_progress import SEMANTIC_PROGRESS_NUDGE
from tests.agent.test_system_prompt import _make_agent


MODELS = ("default", "gpt-5", "gpt-5-codex", "gemini-2.5-pro", "gemma-3",
          "claude-sonnet-4", "grok-4", "glm-5", "qwen-3", "deepseek-v4")
PERSISTENCE = re.compile(
    r"keep (?:working|going|calling tools)|(?:never|do not|don't) stop|"
    r"until (?:the task is )?(?:fully resolved|complete)", re.I,
)
# Categories recognize synonymous instructions, not a snapshot of one constant.
CATEGORIES = {
    "action": r"perform the requested action|use (?:your )?tools to take action|execute it now",
    "completion": r"stop tool work when|keep working until|keep going|never stop",
    "verification": r"verify proportionally|verify your work|confirm they pass before claiming|before finalizing.*correctness",
    "grounding": r"never fabricate|do not fabricate|never invent|never substitute.*fabricated",
    "context": r"retrieve missing context|missing information is retrievable|use the appropriate lookup tool",
    "prerequisite": r"resolve necessary prerequisites|do not skip prerequisite steps",
    "parallel": r"request them together|batch independent lookups|batch independent tool calls",
}


@pytest.fixture
def render(tmp_path, monkeypatch):
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    home.mkdir()
    workspace.mkdir()
    (workspace / "pyproject.toml").write_text("[project]\nname='fixture'\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("TERMINAL_CWD", str(workspace))

    def build(model="gpt-5", *, coding=True, tools=True, completion=True,
              enforcement="auto", parallel=True, parts=False):
        agent = _make_agent(
            model=model, platform="cli", skip_context_files=True,
            api_mode="codex_responses" if "codex" in model else "chat_completions",
            valid_tool_names=["read_file", "write_file", "terminal"] if tools else [],
            _task_completion_guidance=completion, _tool_use_enforcement=enforcement,
            _parallel_tool_call_guidance=parallel, _bot_mode_protocol=False,
        )
        with ExitStack() as stack:
            for target, value in {
                "run_agent.load_soul_md": "",
                "run_agent.build_nous_subscription_prompt": "",
                "run_agent.build_environment_hints": "",
                "run_agent.build_context_files_prompt": "",
                "agent.coding_context._coding_mode": "on" if coding else "off",
                "agent.coding_context.build_coding_workspace_block": "Workspace fixture" if coding else "",
                "hermes_time.now": datetime(2026, 9, 11, tzinfo=timezone.utc),
                "hermes_time.get_timezone": timezone.utc,
            }.items():
                stack.enter_context(patch(target, return_value=value))
            result = sp.build_system_prompt_parts(agent)
            flat = sp.build_system_prompt(agent)
            assert flat == "\n\n".join(v for v in result.values() if v)
        return result if parts else flat

    return build


def assert_unique_categories(prompt):
    for category, pattern in CATEGORIES.items():
        matches = re.findall(pattern, prompt, flags=re.I)
        assert len(matches) <= 1, (category, matches)
    assert not PERSISTENCE.search(prompt)


@pytest.mark.parametrize("model", MODELS)
def test_common_execution_categories_are_unique(render, model):
    prompt = render(model)
    assert_unique_categories(prompt)
    assert prompt.count("# Execution discipline") == 1
    for category in ("action", "completion", "verification", "grounding", "context"):
        assert re.search(CATEGORIES[category], prompt, re.I), category


def test_execution_contract_requires_verification(render):
    prompt = render()
    assert re.search(CATEGORIES["verification"], prompt, re.I)
    assert "checks actually run" in prompt
    assert "without new evidence or a state change" in prompt


def test_execution_contract_has_bounded_stop(render):
    prompt = render()
    assert not PERSISTENCE.search(prompt)
    for condition in ("complete and adequately verified", "blocker requires user input",
                      "user interrupts or redirects", "convergence controls require stopping"):
        assert condition in prompt


@pytest.mark.parametrize("model", MODELS)
def test_provider_operational_deltas_survive(render, model):
    prompt = render(model)
    google = "gemini" in model or "gemma" in model
    grounded = any(p in model for p in ("gpt", "codex", "grok"))
    assert ("Absolute paths" in prompt) == google
    assert ("Non-interactive commands" in prompt) == google
    assert ("<mandatory_tool_use>" in prompt) == grounded
    if grounded:
        for delta in ("Arithmetic", "Hashes", "Current time", "System state",
                      "File contents", "Git history", "Current facts",
                      "describe the USER, not the system"):
            assert delta in prompt
        assert prompt.count("<mandatory_tool_use>") == prompt.count("</mandatory_tool_use>") == 1
    assert "tool_persistence" not in prompt


def test_runtime_nudges_are_not_permanent(render):
    from agent.conversation_loop import (
        _LENGTH_CONTINUATION_NETWORK_STUB, _LENGTH_CONTINUATION_OUTPUT_LIMIT,
        _CODEX_INCOMPLETE_NUDGE, _CODEX_ACK_CONTINUATION_NUDGE,
        _DROPPED_TOOLCALL_NUDGE_CONTENT, _EMPTY_TOOL_RESPONSE_NUDGE,
    )

    for model in MODELS:
        prompt = render(model)
        assert SEMANTIC_PROGRESS_NUDGE not in prompt
        for nudge in (_LENGTH_CONTINUATION_NETWORK_STUB, _LENGTH_CONTINUATION_OUTPUT_LIMIT,
                      _CODEX_INCOMPLETE_NUDGE, _CODEX_ACK_CONTINUATION_NUDGE,
                      _DROPPED_TOOLCALL_NUDGE_CONTENT, _EMPTY_TOOL_RESPONSE_NUDGE):
            assert nudge not in prompt


@pytest.mark.parametrize("tools", [False, True])
def test_general_qa_does_not_require_coding_tools(render, tools):
    prompt = render("default", coding=False, tools=tools)
    assert "coding agent pairing" not in prompt
    assert "mandatory_tool_use" not in prompt
    assert not re.search(r"(?:always|must|required to) (?:use terminal|run tests|inspect files)", prompt, re.I)
    if tools:
        assert "Ordinary conceptual questions can be answered directly" in prompt
        assert "tool availability alone does not require" in prompt
    else:
        assert "# Execution discipline" not in prompt


def test_parallel_guidance_has_single_owner(render):
    prompt = render()
    assert len(re.findall(CATEGORIES["parallel"], prompt, re.I)) == 1
    assert "depends on an earlier" in prompt
    assert prompt.count(pb.PARALLEL_TOOL_CALL_GUIDANCE) == 1


@pytest.mark.parametrize("completion", [False, True])
@pytest.mark.parametrize("enforcement", [False, True, "auto", "off", "yes", ["gpt"], ["unmatched"]])
@pytest.mark.parametrize("model", ["gpt-5", "claude-sonnet-4"])
def test_independent_legacy_gates(render, completion, enforcement, model):
    enabled = enforcement in (True, "yes") or (
        enforcement == "auto" and model == "gpt-5"
    ) or (isinstance(enforcement, list) and any(x in model for x in enforcement))
    prompt = render(model, coding=False, completion=completion, enforcement=enforcement)
    assert ("# Execution discipline" in prompt) == (completion or enabled)
    assert ("mandatory_tool_use" in prompt) == (enabled and model == "gpt-5")
    full_contract = completion or (enabled and model == "gpt-5")
    for clause in (pb.TASK_COMPLETION_GUIDANCE, pb.EXECUTION_VERIFICATION_GUIDANCE,
                   pb.EXECUTION_CONTEXT_GUIDANCE):
        assert (clause in prompt) == full_contract
    assert_unique_categories(prompt)


@pytest.mark.parametrize("model", ["gpt-5", "gpt-5-codex", "grok-4", "gemini-2.5-pro", "gemma-3"])
@pytest.mark.parametrize("enforcement", [False, True])
def test_operational_only_gate_preserves_common_clauses(render, model, enforcement):
    prompt = render(model, coding=False, completion=False, enforcement=enforcement)
    for clause in (pb.TASK_COMPLETION_GUIDANCE, pb.EXECUTION_VERIFICATION_GUIDANCE,
                   pb.EXECUTION_CONTEXT_GUIDANCE):
        assert (clause in prompt) == enforcement


def test_coding_preserves_policy_when_general_gates_are_off(render):
    prompt = render("claude-sonnet-4", completion=False, enforcement=False, parallel=False)
    assert_unique_categories(prompt)
    assert pb.EXECUTION_VERIFICATION_GUIDANCE in prompt
    assert pb.PARALLEL_TOOL_CALL_GUIDANCE in prompt
    general = render("default", coding=False, completion=False, enforcement=False, parallel=False)
    assert "# Execution discipline" not in general
    assert pb.PARALLEL_TOOL_CALL_GUIDANCE not in general


@pytest.mark.parametrize("model", MODELS)
def test_all_model_deltas_require_tools(render, model):
    prompt = render(model, tools=False, enforcement=True)
    assert "# Execution discipline" not in prompt
    assert "mandatory_tool_use" not in prompt
    assert "Absolute paths" not in prompt
    assert pb.PARALLEL_TOOL_CALL_GUIDANCE not in prompt


def test_rebuild_preserves_stable_bytes_and_tier_order(render):
    for model in MODELS:
        first = render(model, parts=True)
        assert first == render(model, parts=True)
        assert "Workspace fixture" not in first["stable"]
        assert "Workspace fixture" in first["context"]
        assert "Execution discipline" not in first["context"] + first["volatile"]


def test_previous_prompt_text_survives_prefix_reconstruction(render):
    # Model an already persisted pre-consolidation scaffold while retaining the
    # real surrounding assembly. Restore must neither replace it nor advertise
    # the new static prefix as a matching cache boundary.
    stored = render().replace("# Execution discipline", "# Finishing the job")
    agent = _make_agent(
        model="gpt-5", platform="cli", skip_context_files=True,
        valid_tool_names=["read_file"], _task_completion_guidance=True,
        _tool_use_enforcement="auto", _use_prompt_caching=True,
        _cached_system_prompt=stored, _cached_system_prompt_static=None,
    )
    sp.reconstruct_static_prefix(agent)
    assert agent._cached_system_prompt == stored
    assert agent._cached_system_prompt_static is None
    assert agent._static_rebuild_failed_for == stored


@pytest.mark.parametrize("mutation", range(7))
def test_mutation_gates(render, monkeypatch, mutation):
    """Call the same named contract test before/after mutation; require AssertionError."""
    cases = [
        (sp, "OPENAI_MODEL_EXECUTION_GUIDANCE", sp.OPENAI_MODEL_EXECUTION_GUIDANCE +
         "\nKeep working until the task is complete.", test_common_execution_categories_are_unique),
        (pb, "EXECUTION_VERIFICATION_GUIDANCE", "", test_execution_contract_requires_verification),
        (pb, "EXECUTION_STOP_GUIDANCE", "Never stop; keep calling tools", test_execution_contract_has_bounded_stop),
        (sp, "GOOGLE_MODEL_OPERATIONAL_GUIDANCE", "", test_provider_operational_deltas_survive),
        (pb, "EXECUTION_STOP_GUIDANCE", pb.EXECUTION_STOP_GUIDANCE + SEMANTIC_PROGRESS_NUDGE,
         test_runtime_nudges_are_not_permanent),
        (pb, "TOOL_USE_ENFORCEMENT_GUIDANCE", pb.TOOL_USE_ENFORCEMENT_GUIDANCE +
         " Always use terminal and run tests for conceptual questions.", test_general_qa_does_not_require_coding_tools),
        (sp, "OPENAI_MODEL_EXECUTION_GUIDANCE", sp.OPENAI_MODEL_EXECUTION_GUIDANCE +
         "\nBatch independent tool calls.", test_parallel_guidance_has_single_owner),
    ]
    module, name, replacement, gate = cases[mutation]
    kwargs = {"model": "gpt-5"} if mutation == 0 else (
        {"model": "gemini-2.5-pro"} if mutation == 3 else (
            {"tools": True} if mutation == 5 else {}))
    gate(render, **kwargs)
    monkeypatch.setattr(module, name, replacement)
    with pytest.raises(AssertionError):
        gate(render, **kwargs)
