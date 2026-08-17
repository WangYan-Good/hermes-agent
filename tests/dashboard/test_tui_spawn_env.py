from hermes_cli.dashboard_tui_env import apply_dashboard_tui_render_env


def test_dashboard_tui_env_uses_fixed_viewport_and_routes_wheel_to_ink() -> None:
    env = {
        "HERMES_TUI_DISABLE_MOUSE": "1",
        "HERMES_TUI_INLINE": "1",
    }

    apply_dashboard_tui_render_env(env)

    assert env["HERMES_TUI_DASHBOARD"] == "1"
    assert env["HERMES_TUI_INLINE"] == "0"
    assert env["HERMES_TUI_MOUSE_TRACKING"] == "wheel"
    assert "HERMES_TUI_DISABLE_MOUSE" not in env
