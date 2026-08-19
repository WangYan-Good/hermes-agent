from hermes_cli.dashboard_tui_env import apply_dashboard_tui_render_env


def test_dashboard_tui_env_uses_inline_scrollback_without_mouse_tracking() -> None:
    env = {
        "HERMES_TUI_DISABLE_MOUSE": "0",
        "HERMES_TUI_INLINE": "0",
        "HERMES_TUI_MOUSE_TRACKING": "wheel",
    }

    apply_dashboard_tui_render_env(env)

    assert env["HERMES_TUI_DASHBOARD"] == "1"
    assert env["HERMES_TUI_INLINE"] == "1"
    assert env["HERMES_TUI_DISABLE_MOUSE"] == "1"
    assert "HERMES_TUI_MOUSE_TRACKING" not in env
