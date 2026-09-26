"""Presentation config survives real loading and the dashboard HTTP boundary."""

import pytest
import yaml


@pytest.mark.parametrize(
    "dashboard, expected",
    [
        ({}, None),
        ({"theme": "midnight"}, None),
        ({"chat": {}}, None),
        ({"chat": {"default_mode": "terminal"}}, "terminal"),
        ({"chat": {"default_mode": "native"}}, "native"),
        ({"chat": {"default_mode": "future-mode"}}, "future-mode"),
    ],
)
def test_chat_mode_config_contract(dashboard, expected, _isolate_hermes_home):
    from hermes_cli.config import DEFAULT_CONFIG, get_config_path, load_config
    from hermes_cli.web_server import app, _SESSION_HEADER_NAME, _SESSION_TOKEN
    from starlette.testclient import TestClient

    path = get_config_path()
    original = yaml.safe_dump({"dashboard": dashboard} if dashboard else {})
    path.write_text(original, encoding="utf-8")

    assert "chat" not in DEFAULT_CONFIG["dashboard"]
    loaded = load_config()
    assert loaded["dashboard"].get("chat", {}).get("default_mode") == expected
    if "theme" in dashboard:
        assert loaded["dashboard"]["theme"] == dashboard["theme"]

    client = TestClient(app)
    client.headers[_SESSION_HEADER_NAME] = _SESSION_TOKEN
    response = client.get("/api/config")
    assert response.status_code == 200
    assert response.json()["dashboard"].get("chat", {}).get("default_mode") == expected
    defaults = client.get("/api/config/defaults")
    assert defaults.status_code == 200
    assert "chat" not in defaults.json()["dashboard"]
    schema = client.get("/api/config/schema")
    assert schema.status_code == 200
    assert "dashboard.chat.default_mode" not in schema.json()["fields"]
    assert path.read_text(encoding="utf-8") == original
