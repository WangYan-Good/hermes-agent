"""Rendering environment for the browser-embedded TUI."""

from collections.abc import MutableMapping


def apply_dashboard_tui_render_env(env: MutableMapping[str, str]) -> None:
    """Use xterm's primary-buffer scrollback for the dashboard TUI."""
    env["HERMES_TUI_DASHBOARD"] = "1"
    env["HERMES_TUI_INLINE"] = "1"
    env["HERMES_TUI_DISABLE_MOUSE"] = "1"
    env.pop("HERMES_TUI_MOUSE_TRACKING", None)
