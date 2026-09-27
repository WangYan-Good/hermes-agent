"""Run the nine UI-C1 destructive gates in a disposable repository copy.

Explicit runner, not a recursively collected pytest test. Run only in the
validation container after the normal tests pass. Mutants never touch source.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
PY_TEST = 'tests/hermes_cli/test_pty_foundation.py'
MUTATIONS = [
    ('auth', 'hermes_cli/pty_transport.py', 'if not web._ws_auth_ok(ws):', 'if False:', 'test_invalid_handshake_never_spawns[missing-auth]'),
    ('binary-as-control', 'hermes_cli/pty_session.py', 'self.accepted_input = True', "if data.startswith(b'{'):\n                self.bridge.close()\n                return\n            self.accepted_input = True", 'test_attach_reconnect_reap_and_input_are_single_owner'),
    ('prepare-admission', 'hermes_cli/pty_control.py', "if action in {'prepare', 'release', 'abort'}:", "if action in {'release', 'abort'}:", 'test_prepare_freezes_before_owner_response_and_cancel_requires_ack'),
    ('release-ack', 'hermes_cli/pty_control.py', "if result.get('released') is not True:", 'if False:', 'test_release_without_ack_cannot_cleanup'),
    ('abort-input', 'hermes_cli/pty_control.py', 'if session.accepted_input:', 'if False:', 'test_abort_vs_first_input_and_release_ack_order'),
    ('double-viewer', 'hermes_cli/pty_session.py', 'if self.closing or self.viewer is not None:', 'if self.closing:', 'test_attach_reconnect_reap_and_input_are_single_owner'),
    ('reap-leak', 'hermes_cli/pty_session.py', 'await asyncio.to_thread(session.bridge.close)', 'pass  # mutant leaks process', 'test_attach_reconnect_reap_and_input_are_single_owner'),
    ('profile-attach', 'hermes_cli/pty_session.py', '(existing.principal, existing.profile) != (principal, profile)', 'existing.principal != principal', 'test_cross_scope_attachment_rejected'),
    ('native-route', 'web/src/pages/ChatPage.tsx', 'return <NativeChatPage {...props} />;', 'return <section className="xterm" aria-label="Terminal Chat" />;', None),
]


def run():
    results = []
    with tempfile.TemporaryDirectory(prefix='ui-c1-mutants-') as temp:
        repo = Path(temp) / 'repo'
        shutil.copytree(ROOT, repo, ignore=shutil.ignore_patterns('.git', '.venv', 'node_modules', '__pycache__', '.pytest_cache', 'web_dist', 'dist'))
        (repo / 'node_modules').symlink_to(ROOT / 'node_modules', target_is_directory=True)
        env = {**os.environ, 'PYTHONPATH': str(repo)}
        for name, file, original, replacement, test in MUTATIONS:
            path = repo / file
            source = path.read_text()
            assert source.count(original) == 1, (name, 'mutation site changed')
            command = ([sys.executable, '-m', 'pytest', f'{PY_TEST}::{test}', '-q'] if test else ['npm', 'test', '--prefix', 'web', '--', 'src/pages/ChatPage.test.tsx'])
            baseline = subprocess.run(command, cwd=repo, env=env, capture_output=True, text=True)
            if baseline.returncode:
                raise RuntimeError(f'{name} baseline failed:\n{baseline.stdout}\n{baseline.stderr}')
            try:
                path.write_text(source.replace(original, replacement, 1))
                mutant = subprocess.run(command, cwd=repo, env=env, capture_output=True, text=True)
                detected = mutant.returncode == 1 and ('failed' in mutant.stdout.lower())
                results.append({'mutation': name, 'detected': detected})
                print(json.dumps(results[-1]), flush=True)
                if not detected:
                    print(mutant.stdout, mutant.stderr)
            finally:
                path.write_text(source)
            restored = subprocess.run(command, cwd=repo, env=env, capture_output=True, text=True)
            if restored.returncode:
                raise RuntimeError(f'{name} restored source failed:\n{restored.stdout}\n{restored.stderr}')
        assert all(item['detected'] for item in results), results
        print(f'{len(results)}/{len(MUTATIONS)} mutations detected', flush=True)


if __name__ == '__main__':
    run()
