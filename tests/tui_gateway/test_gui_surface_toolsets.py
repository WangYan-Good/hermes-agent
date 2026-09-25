"""GUI capability follows the SESSION's client, not the backend's process env.

The desktop app is a client. It can drive a backend that Electron spawned
locally, one reached over SSH, one behind a plain URL+token, or Hermes Cloud —
and only the first two run with ``HERMES_DESKTOP=1`` in their environment.
Gating the pane/browser/reaction tools on that env var therefore stripped every
one of them from URL and cloud gateways, while the same backend still told the
model "You are chatting inside the Hermes desktop app".

These tests pin the contract that replaced it: eligibility is resolved from the
session's own ``source`` (``session.create``'s ``source: 'desktop'``), so the
answer is identical on every connection topology.
"""

import pytest

import tui_gateway.server as server
from toolsets import TOOLSETS, resolve_toolset

GUI_TOOLS = {
    "close_terminal",
    "focus_pane",
    "open_preview",
    "read_preview",
    "read_terminal",
    "read_window_below",
    "react_to_message",
}


@pytest.fixture
def no_desktop_env(monkeypatch):
    """A backend nobody told about the desktop — i.e. every remote gateway."""
    monkeypatch.delenv("HERMES_DESKTOP", raising=False)
    monkeypatch.delenv("HERMES_DESKTOP_TERMINAL", raising=False)
    monkeypatch.delenv("HERMES_TUI_TOOLSETS", raising=False)
    return monkeypatch


class TestDesktopUiToolset:
    def test_holds_exactly_the_gui_affordances(self):
        assert set(resolve_toolset("desktop_ui")) == GUI_TOOLS

    def test_stays_off_the_core_tool_list(self):
        """Core ships on every API call — a GUI-only tool must not be there."""
        from toolsets import _HERMES_CORE_TOOLS

        assert GUI_TOOLS.isdisjoint(_HERMES_CORE_TOOLS)

    def test_no_platform_bundle_carries_it(self):
        """Messaging/CLI bundles must not pick these up by listing them."""
        for name, spec in TOOLSETS.items():
            if name == "desktop_ui":
                continue
            assert GUI_TOOLS.isdisjoint(set(spec.get("tools") or ())), name


class TestSurfaceResolution:
    def test_desktop_session_gets_them_with_no_desktop_env(self, no_desktop_env):
        """THE regression: a desktop client on a remote/cloud backend."""
        assert "desktop_ui" in server._gui_surface_toolsets("desktop")

    def test_tui_session_does_not(self, no_desktop_env):
        assert "desktop_ui" not in server._gui_surface_toolsets("tui")

    def test_desktop_env_alone_does_not_grant_them(self, no_desktop_env):
        """A desktop-spawned backend serving a TUI session stays clean.

        The embedded terminal pane runs `hermes --tui` against this same
        backend; env-keyed gating handed it GUI tools it cannot answer.
        """
        no_desktop_env.setenv("HERMES_DESKTOP", "1")
        assert "desktop_ui" not in server._gui_surface_toolsets("tui")

    def test_project_tools_ride_on_every_gui_surface(self, no_desktop_env):
        for platform in ("desktop", "tui"):
            assert "project" in server._gui_surface_toolsets(platform)


class TestResolverPlumbing:
    def test_posture_path_folds_in_the_session_surface(self, no_desktop_env):
        """Focus-mode returns early — the surface toolsets must survive it."""
        import agent.coding_context as cc

        no_desktop_env.setattr(cc, "coding_selection", lambda **_: ["coding"])

        assert server._load_enabled_toolsets("desktop") == [
            "coding",
            "desktop_ui",
            "gui_interactions",
            "project",
        ]
        assert server._load_enabled_toolsets("tui") == ["coding", "project"]

    def test_config_path_folds_in_the_session_surface(self, no_desktop_env):
        import agent.coding_context as cc
        import hermes_cli.config as config_mod

        no_desktop_env.setattr(cc, "coding_selection", lambda **_: None)
        no_desktop_env.setattr(
            config_mod, "load_config", lambda: {"platform_toolsets": {"cli": ["memory"]}}
        )

        desktop = server._load_enabled_toolsets("desktop")
        tui = server._load_enabled_toolsets("tui")

        assert desktop is not None and tui is not None
        assert "desktop_ui" in desktop
        assert "desktop_ui" not in tui

    def test_explicit_env_pin_still_wins(self, no_desktop_env):
        """HERMES_TUI_TOOLSETS is an operator override; surface can't re-add."""
        no_desktop_env.setenv("HERMES_TUI_TOOLSETS", "web,memory")

        assert server._load_enabled_toolsets("desktop") == ["web", "memory"]


@pytest.mark.parametrize("source", ["desktop", "webui", "tui", "telegram", "discord", "api_server", "cli", "webhook"])
@pytest.mark.parametrize("posture", [True, False])
def test_actual_agent_definitions_follow_session_surface(no_desktop_env, tmp_path, source, posture):
    """Use real discovery + AIAgent definitions, not synthetic renderer events."""
    import agent.coding_context as cc
    import hermes_cli.config as config_mod
    from run_agent import AIAgent
    from model_tools import get_tool_definitions

    no_desktop_env.setenv("HERMES_HOME", str(tmp_path))
    no_desktop_env.setattr(cc, "coding_selection", lambda **_: ["file"] if posture else None)
    no_desktop_env.setattr(config_mod, "load_config", lambda: {
        "platform_toolsets": {"cli": ["file"]}, "tools": {"tool_search": {"enabled": "off"}},
    })
    toolsets = server._load_enabled_toolsets(source)
    agent = AIAgent(
        api_key="local-test", base_url="http://127.0.0.1:1/v1", model="test",
        platform=source, enabled_toolsets=toolsets, quiet_mode=True,
        skip_memory=True, skip_context_files=True,
    )
    # Disable schema deferral through supported config; never mock discovery
    # or the actual AIAgent tool-definition construction.
    names = {tool["function"]["name"] for tool in get_tool_definitions(toolsets, quiet_mode=True)}
    assert ("setup_mcp" in names) == (source in {"desktop", "webui"})
    assert ("setup_mcp" in agent.valid_tool_names) == (source in {"desktop", "webui"})
    if source == "webui":
        assert "gui_interactions" in toolsets
        assert "project" in toolsets
        assert GUI_TOOLS.isdisjoint(names)


def test_interaction_tool_is_not_core_and_explicit_pin_wins(no_desktop_env):
    from model_tools import get_tool_definitions
    from toolsets import _HERMES_CORE_TOOLS
    from tools.setup_mcp_tool import SETUP_MCP_SCHEMA
    from agent.prompt_builder import PLATFORM_HINTS

    assert "setup_mcp" not in _HERMES_CORE_TOOLS
    assert "setup_mcp" in resolve_toolset("gui_interactions")
    assert "setup_mcp" not in resolve_toolset("desktop_ui")
    assert "Web Native Chat" in SETUP_MCP_SCHEMA["description"]
    assert "setup_mcp if available" in PLATFORM_HINTS["webui"]
    no_desktop_env.setenv("HERMES_TUI_TOOLSETS", "file")
    for source in ("desktop", "webui"):
        selected = server._load_enabled_toolsets(source)
        assert selected == ["file"]
        assert "setup_mcp" not in {t["function"]["name"] for t in get_tool_definitions(selected, quiet_mode=True)}
