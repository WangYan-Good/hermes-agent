"""Real standalone Ink/stdio gateway/Agent/SessionDB smoke against the P7 HTTP provider.

Start chat_native_server.py, build ui-tui, then run this in a disposable container.
No Dashboard terminal or presentation environment is used.
"""
import fcntl
import json
import os
from pathlib import Path
import pty
import select
import signal
import sqlite3
import struct
import tempfile
import termios
import time
import urllib.request

root = Path(__file__).resolve().parents[2]
url = os.environ.get("CHAT_E2E_URL", "http://127.0.0.1:8765")
fixture = json.load(urllib.request.urlopen(url + "/p7-evidence"))
home = Path(tempfile.mkdtemp(prefix="p7-tui-"))
(home / "config.yaml").write_text((Path(fixture["home"]) / "config.yaml").read_text())
transcripts = []


def rows():
    if not (home / "state.db").exists():
        return []
    with sqlite3.connect(home / "state.db") as db:
        return db.execute("SELECT session_id, role, content, tool_name FROM messages ORDER BY id").fetchall()


class Tui:
    def __init__(self, resume=None):
        env = {**os.environ, "HERMES_HOME": str(home), "TERM": "xterm-256color",
               "HERMES_PYTHON": os.environ.get("HERMES_PYTHON", "/opt/hermes/.venv/bin/python"),
               "HERMES_PYTHON_SRC_ROOT": str(root), "HERMES_TUI_DIR": str(root / "ui-tui")}
        for key in ("HERMES_TUI_PRESENTATION_URL", "HERMES_TUI_DASHBOARD", "HERMES_TUI_GATEWAY_URL", "HERMES_TUI_SIDECAR_URL", "HERMES_TUI_QUERY", "HERMES_TUI_RESUME"):
            env.pop(key, None)
        if resume:
            env["HERMES_TUI_RESUME"] = resume
        self.output = ""
        self.pid, self.fd = pty.fork()
        if self.pid == 0:
            os.chdir(root)
            fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
            os.execvpe("node", ["node", str(root / "ui-tui/dist/entry.js")], env)
        self.pump(1)

    def pump(self, duration=.2):
        end = time.monotonic() + duration
        while time.monotonic() < end:
            if select.select([self.fd], [], [], .05)[0]:
                try:
                    data = os.read(self.fd, 65536)
                except OSError:
                    raise AssertionError("Standalone TUI exited before completing the flow")
                if not data:
                    raise AssertionError("Standalone TUI transport closed")
                self.output += data.decode(errors="replace")

    def wait(self, predicate, label, timeout=60):
        deadline = time.monotonic() + timeout
        while not predicate():
            if time.monotonic() >= deadline:
                raise AssertionError(f"Timed out: {label}; screen tail: {self.output[-2500:]}")
            self.pump()

    def send(self, text):
        os.write(self.fd, b"\x1b[200~" + text.encode() + b"\x1b[201~")
        self.pump(.3)
        os.write(self.fd, b"\r")

    def close(self):
        transcripts.append(self.output)
        try:
            os.kill(self.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        os.waitpid(self.pid, 0)
        os.close(self.fd)


tui = Tui()
try:
    # The actual screen becomes interactive after the gateway's initial create.
    tui.wait(lambda: "p7-model" in tui.output, "initial create/ready screen")
    tui.pump(2)
    tui.send("P7-TUI terminal")
    tui.wait(lambda: any(r[1] == "tool" and r[3] == "terminal" and "P7 real terminal output" in (r[2] or "") for r in rows()), "real terminal tool row")
    tui.wait(lambda: any(r[1] == "assistant" and r[2] == "P7 controlled answer." for r in rows()), "terminal final answer")
    first = rows()
    sid = first[0][0]
    tui.pump(1)
    tui.send("P7-CLARIFY interaction")
    tui.wait(lambda: "P7 choose a path" in tui.output, "actual clarify overlay")
    os.write(tui.fd, b"1")
    tui.wait(lambda: any(r[1] == "tool" and r[3] == "clarify" and "Continue" in (r[2] or "") for r in rows()), "clarify response accepted")
    tui.wait(lambda: sum(r[1] == "assistant" and r[2] == "P7 controlled answer." for r in rows()) == 2, "interaction final answer")
finally:
    tui.close()

before = rows()
tui = Tui(sid)
try:
    tui.wait(lambda: "P7-TUI" in tui.output, "durable resume screen")
    tui.pump(2)
    assert rows() == before, "Resume replayed or mutated durable history"
    tui.send("P7 explicit resumed message")
    tui.wait(lambda: sum(r[1] == "assistant" and r[2] == "P7 controlled answer." for r in rows()) == 3, "explicit resumed submit")
    tui.pump(.5)
    assert "P7 controlled answer." in tui.output
finally:
    tui.close()

result = {"status": "PASS", "home": str(home), "stored_id": sid,
          "transport": "real Ink -> stdio -> tui_gateway -> AIAgent",
          "presentation_env": False, "rows": rows(), "terminal": True, "clarify": True, "resume_no_replay": True}
if os.environ.get("CHAT_E2E_EVIDENCE"):
    Path(os.environ["CHAT_E2E_EVIDENCE"]).write_text(json.dumps(result, indent=2))
print(json.dumps(result, indent=2))
