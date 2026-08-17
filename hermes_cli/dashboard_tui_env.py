"""Rendering environment for the browser-embedded TUI."""

from collections.abc import MutableMapping


def apply_dashboard_tui_render_env(env: MutableMapping[str, str]) -> None:
    """Use Ink-owned scrolling inside a fixed dashboard terminal viewport."""
    env.pop("HERMES_TUI_DISABLE_MOUSE", None)
    env["HERMES_TUI_DASHBOARD"] = "1"
    env["HERMES_TUI_INLINE"] = "0"
    env["HERMES_TUI_MOUSE_TRACKING"] = "wheel"
