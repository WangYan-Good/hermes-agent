"""Isolated real Dashboard/TUI with a deterministic OpenAI-compatible provider.

Run in a disposable container, then run web/e2e/chat-interface.cjs.
Only the model HTTP boundary is synthetic; auth, agent, DB and transports are real.
"""

import os
from pathlib import Path
import tempfile

root = Path(__file__).resolve().parents[3]
__import__("sys").path.insert(0, str(root))
home = Path(tempfile.mkdtemp(prefix="p6-home-"))
os.environ.update(
    HERMES_HOME=str(home),
    HERMES_DASHBOARD_SESSION_TOKEN="p6-local",
    HERMES_WEB_DIST=str(root / "hermes_cli/web_dist"),
    HERMES_TUI_DIR=str(root / "ui-tui"),
    HERMES_PYTHON_SRC_ROOT=str(root),
    HERMES_PYTHON=__import__("sys").executable,
)
(home / "config.yaml").write_text(
    """model:
  default: p6-model
  provider: custom:p6
custom_providers:
  - name: p6
    base_url: http://127.0.0.1:8765/v1
    api_key: p6-local
    api_mode: chat_completions
toolsets: []
compression:
  enabled: false
""",
    encoding="utf-8",
)
from hermes_cli import web_server as w
from fastapi import Request
from fastapi.responses import JSONResponse, StreamingResponse
import asyncio
import json
import time

calls = []
(home / "roundtrip-tool.txt").write_text("P6 real tool output", encoding="utf-8")
from tui_gateway import server as gateway

submissions = []
run_submit = gateway._run_prompt_submit


def counted_submit(*args, **kwargs):
    submissions.append({
        "runtime": args[1],
        "automatic": kwargs.get("display_kind") == "auto_continue",
        "attachments": len(
            (kwargs.get("display_metadata") or {}).get("attachments", [])
        ),
    })
    return run_submit(*args, **kwargs)


gateway._run_prompt_submit = counted_submit


@w.app.post("/v1/chat/completions")
async def completion(request: Request):
    body = await request.json()
    calls.append(body)
    text = str(
        next(
            (m["content"] for m in reversed(body["messages"]) if m["role"] == "user"),
            "",
        )
    )
    if "P6-BUSY" in text:
        await asyncio.sleep(5)
    answer = "P6 controlled answer."
    tool = (
        "P6-IDLE" in text
        and body["messages"][-1]["role"] != "tool"
        and any(
            t.get("function", {}).get("name") == "read_file"
            for t in body.get("tools", [])
        )
    )
    tool_call = {
        "index": 0,
        "id": "p6-read",
        "type": "function",
        "function": {
            "name": "read_file",
            "arguments": json.dumps({"path": str(home / "roundtrip-tool.txt")}),
        },
    }
    deltas = (
        [{"role": "assistant"}, {"tool_calls": [tool_call]}]
        if tool
        else [{"role": "assistant"}, {"content": answer}]
    )
    finish = "tool_calls" if tool else "stop"

    if body.get("stream"):

        async def stream():
            for delta in deltas:
                yield (
                    "data: "
                    + json.dumps({
                        "id": "p6",
                        "object": "chat.completion.chunk",
                        "created": int(time.time()),
                        "model": "p6-model",
                        "choices": [
                            {"index": 0, "delta": delta, "finish_reason": None}
                        ],
                    })
                    + "\n\n"
                )
            yield (
                "data: "
                + json.dumps({
                    "id": "p6",
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": "p6-model",
                    "choices": [{"index": 0, "delta": {}, "finish_reason": finish}],
                })
                + "\n\n"
            )
            yield "data: [DONE]\n\n"

        return StreamingResponse(stream(), media_type="text/event-stream")
    return JSONResponse({
        "id": "p6",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": "p6-model",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": answer},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 20, "completion_tokens": 5, "total_tokens": 25},
    })


@w.app.get("/p6-evidence")
async def evidence():
    from tui_gateway import server

    return {
        "provider_calls": len(calls),
        "wire_metadata": any(
            "display_metadata" in m or "display_kind" in m
            for body in calls
            for m in body.get("messages", [])
        ),
        "submissions": submissions,
        "tool_calls": sum(
            1
            for body in calls
            for message in body.get("messages", [])
            if message.get("role") == "tool"
            and message.get("tool_call_id") == "p6-read"
        ),
        "sessions": [
            {
                "runtime": sid,
                "stored": s.get("session_key"),
                "running": s.get("running"),
                "workers": s.get("_presentation_workers"),
                "handoff": bool(s.get("presentation_handoff")),
            }
            for sid, s in server._sessions.items()
        ],
        "home": str(home),
    }


@w.app.post("/p6-control")
async def control(request: Request):
    body = await request.json()
    if body.get("action") == "history":
        from hermes_state import SessionDB
        from tui_gateway.turn_marker import record_turn_start

        with SessionDB(home / "state.db") as db:
            db.create_session("p6-root", source="webui")
            db.append_message(
                "p6-root", "assistant", content="Excluded branch ancestor"
            )
            cfg = {"_branched_from": "p6-root"}
            db.create_session(
                "p6-branch-a",
                source="webui",
                parent_session_id="p6-root",
                model_config=cfg,
            )
            db.append_message("p6-branch-a", "user", content="P6 lineage user")
            db.append_message(
                "p6-branch-a",
                "assistant",
                content="",
                tool_calls=[
                    {
                        "id": "p6-lineage-read",
                        "type": "function",
                        "function": {
                            "name": "read_file",
                            "arguments": '{"path":"lineage.txt"}',
                        },
                    }
                ],
            )
            db.end_session("p6-branch-a", "compression")
            db.create_session(
                "p6-branch-b",
                source="webui",
                parent_session_id="p6-branch-a",
                model_config=cfg,
            )
            db.append_message(
                "p6-branch-b",
                "tool",
                content="P6 lineage tool",
                tool_call_id="p6-lineage-read",
                tool_name="read_file",
            )
            db.append_message("p6-branch-b", "assistant", content="P6 lineage answer")
        record_turn_start(home, "p6-branch-b", "DO NOT REPLAY THIS CRASH MARKER")
        return {"ok": True}
    if body.get("action") == "profiles":
        from hermes_cli.profiles import create_profile
        import yaml

        for name, mode in [("p6-a", "native"), ("p6-b", "terminal")]:
            directory = create_profile(name, no_alias=True, no_skills=True)
            cfg = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))
            cfg["dashboard"] = {"chat": {"default_mode": mode}}
            (directory / "config.yaml").write_text(
                yaml.safe_dump(cfg), encoding="utf-8"
            )
        return {"ok": True}
    plugin = home / "plugins/p6-override/dashboard"
    if body.get("action") == "plugin":
        import yaml

        cfg = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))
        cfg["plugins"] = {"enabled": ["p6-override"]}
        (home / "config.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")
        plugin.mkdir(parents=True, exist_ok=True)
        (plugin / "manifest.json").write_text(
            json.dumps({
                "name": "p6-override",
                "label": "P6 override",
                "tab": {"path": "/p6-plugin", "override": "/chat"},
                "entry": "index.js",
            }),
            encoding="utf-8",
        )
        (plugin / "index.js").write_text(
            "window.__HERMES_PLUGINS__.register('p6-override', function(){ return window.__HERMES_PLUGIN_SDK__.React.createElement('div', null, 'P6 plugin owns chat'); });",
            encoding="utf-8",
        )
    else:
        import shutil

        shutil.rmtree(home / "plugins/p6-override", ignore_errors=True)
    w._get_dashboard_plugins(force_rescan=True)
    return {"ok": True}


w.app.router.routes.sort(
    key=lambda route: 0
    if getattr(route, "path", "").startswith(("/p6-", "/v1/"))
    else 1
)
w.app.state.bound_host = "127.0.0.1"
w.app.state.bound_port = 8765
w.app.state.auth_required = False
import uvicorn

uvicorn.run(w.app, host="127.0.0.1", port=8765, log_level="warning")
