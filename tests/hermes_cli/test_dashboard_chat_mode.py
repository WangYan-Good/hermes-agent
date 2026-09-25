"""Presentation config survives real loading and the dashboard HTTP boundary."""

import pytest
import yaml


@pytest.mark.parametrize(
    "dashboard, expected",
    [
        ({}, "native"),
        ({"theme": "midnight"}, "native"),
        ({"chat": {}}, "native"),
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

    assert DEFAULT_CONFIG["dashboard"]["chat"]["default_mode"] == "native"
    loaded = load_config()
    assert loaded["dashboard"]["chat"]["default_mode"] == expected
    if "theme" in dashboard:
        assert loaded["dashboard"]["theme"] == dashboard["theme"]

    client = TestClient(app)
    client.headers[_SESSION_HEADER_NAME] = _SESSION_TOKEN
    response = client.get("/api/config")
    assert response.status_code == 200
    assert response.json()["dashboard"]["chat"]["default_mode"] == expected
    defaults = client.get("/api/config/defaults")
    assert defaults.status_code == 200
    assert defaults.json()["dashboard"]["chat"]["default_mode"] == "native"
    schema = client.get("/api/config/schema")
    assert schema.status_code == 200
    field = schema.json()["fields"]["dashboard.chat.default_mode"]
    assert field["type"] == "select"
    assert set(field["options"]) == {"native", "terminal"}
    assert path.read_text(encoding="utf-8") == original


def test_python_and_browser_default_are_the_same():
    import re
    from pathlib import Path
    from hermes_cli.config import DEFAULT_CONFIG
    source = (Path(__file__).resolve().parents[2] / 'web/src/pages/chat/chat-mode.ts').read_text(encoding='utf-8')
    match = re.search(r'DEFAULT_CHAT_MODE: ChatMode = "(native|terminal)"', source)
    assert match, 'The browser must export its canonical fallback'
    assert match.group(1) == DEFAULT_CONFIG['dashboard']['chat']['default_mode']


def test_chat_mode_write_yaml_export_and_reset(_isolate_hermes_home):
    from hermes_cli.web_server import app, _SESSION_HEADER_NAME, _SESSION_TOKEN
    from starlette.testclient import TestClient
    client = TestClient(app)
    client.headers[_SESSION_HEADER_NAME] = _SESSION_TOKEN
    assert client.put('/api/config', json={'config': {'dashboard': {'chat': {'default_mode': 'terminal'}}}}).status_code == 200
    assert client.get('/api/config').json()['dashboard']['chat']['default_mode'] == 'terminal'
    raw = client.get('/api/config/raw').json()
    assert yaml.safe_load(raw['yaml'])['dashboard']['chat']['default_mode'] == 'terminal'
    # YAML import uses the same raw endpoint as the editor; reset saves defaults.
    assert client.put('/api/config/raw', json={'yaml_text': 'dashboard:\n  chat:\n    default_mode: native\n'}).status_code == 200
    assert client.get('/api/config').json()['dashboard']['chat']['default_mode'] == 'native'
    defaults = client.get('/api/config/defaults').json()
    assert client.put('/api/config', json={'config': defaults}).status_code == 200
    assert client.get('/api/config').json()['dashboard']['chat'] == defaults['dashboard']['chat']
