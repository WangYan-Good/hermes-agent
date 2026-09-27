"""UI-C2 destructive behavioral gates. Run only inside the validation container."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
SWITCH = 'web/src/pages/chat/chat-switch.ts'
NATIVE = 'web/src/pages/chat/native/native-session.ts'
MUTATIONS = [
    ('premature-preference', SWITCH, 'this.previous = this.state.mounted; this.commit = commit;', 'commit?.(); this.previous = this.state.mounted; this.commit = commit;', 'src/pages/chat/chat-switch.test.ts'),
    ('overlapping-owners', SWITCH, 'await surface.release();', 'void surface.release();', 'src/pages/chat/chat-switch.test.ts'),
    ('ambiguous-submit-replay', NATIVE, '      const storedId = this.state.storedId || this.target;', '      if (this.uncertainSubmit) void gateway.request("prompt.submit", { session_id: this.state.runtimeId, text: "replayed" });\n      const storedId = this.state.storedId || this.target;', 'src/pages/chat/native/native-session.test.ts'),
    ('silent-draft-discard', NATIVE, '  prepare = async (): Promise<SurfaceStatus> => {', '  prepare = async (): Promise<SurfaceStatus> => {\n    this.setDraft("");', 'src/pages/chat/native/native-session.test.ts'),
    ('automatic-interaction-response', NATIVE, '  prepare = async (): Promise<SurfaceStatus> => {', '  prepare = async (): Promise<SurfaceStatus> => {\n    for (const r of Object.values(this.state.interactions)) if (r.kind === "approval") await this.respondApproval(r, "once"); else if (r.kind === "clarify") await this.respondClarify(r, "automatic");', 'src/pages/chat/native/native-session.test.ts'),
    ('stale-profile-callback', SWITCH, 'const current = () => !this.stopped && this.surface === surface && this.state.generation === generation;', 'const current = () => true;', 'src/pages/chat/chat-switch.test.ts'),
    ('stale-terminal-generation', 'hermes_cli/pty_session.py', 'self.generation != generation', 'False', 'tests/hermes_cli/test_pty_foundation.py'),
    ('unconfirmed-cleanup-reopens', SWITCH, 'if (this.surface) await this.surface.dispose();', 'if (this.surface) await this.surface.dispose().catch(() => {});', 'src/pages/chat/chat-switch.test.ts'),
    ('url-double-surface', 'web/src/pages/chat/ChatSurfaceRouter.tsx', "{state.mounted === 'native' ? <Native", "{state.mounted === 'terminal' ? <><Native isActive={isActive} /><Terminal profile={profile} /></> : state.mounted === 'native' ? <Native", 'src/pages/ChatPage.test.tsx'),
    ('plugin-mounts-builtin', 'web/src/lib/chat-activation.ts', "if (!embedded || overridden) return 'suppressed';", "if (!embedded) return 'suppressed';", 'src/lib/chat-activation.test.ts'),
    ('native-cancel-release-latch', NATIVE, '    this.releasing = false;', '    // mutant: release latch remains set', 'src/pages/chat/native/native-session.test.ts'),
    ('terminal-cancel-release-latch', 'web/src/pages/chat/terminal-lifecycle.ts', '      this.releaseStarted = false;', '      // mutant: release latch remains set', 'src/pages/chat/terminal-lifecycle.test.ts'),
    ('host-keeps-pty-viewer', 'web/src/pages/chat/terminal-lifecycle.ts', 'if (this.options.managed) { this.detach(); return; }', 'if (this.options.managed) { return; }', 'src/pages/chat/terminal-lifecycle.test.ts'),
    ('native-orphan-never-retires', 'tui_gateway/native_presentation.py', '    state.completed = time.monotonic()\n    state.retirement_pending = False', '    state.completed = 0.0\n    state.retirement_pending = False', 'tests/tui_gateway/test_native_presentation.py'),
    ('url-consumed-before-return', 'web/src/pages/chat/ChatSurfaceRouter.tsx', 'if (!isActive) { urlAttempt.current = null; return; }', 'if (!isActive) return;', 'src/pages/ChatPage.test.tsx'),
]


def run():
    results = []
    with tempfile.TemporaryDirectory(prefix='ui-c2-mutants-') as temp:
        repo = Path(temp) / 'repo'
        shutil.copytree(ROOT, repo, ignore=shutil.ignore_patterns('.git', '.venv', 'node_modules', '__pycache__', '.pytest_cache', 'web_dist', 'dist'))
        (repo / 'node_modules').symlink_to(ROOT / 'node_modules', target_is_directory=True)
        env = {**os.environ, 'PYTHONPATH': str(repo), 'HERMES_PYTHON': sys.executable}
        for name, file, before, after, test in MUTATIONS:
            path = repo / file
            source = path.read_text(encoding='utf-8')
            assert source.count(before) == 1, (name, 'mutation site changed')
            command = ['bash', 'scripts/run_tests.sh', '-j', '2', test, '-q'] if test.endswith('.py') else ['npm', 'test', '--prefix', 'web', '--', test]
            def execute():
                return subprocess.run(command, cwd=repo, env=env, capture_output=True, text=True)
            baseline = execute()
            if baseline.returncode:
                raise RuntimeError(f'{name} baseline failed:\n{baseline.stdout}\n{baseline.stderr}')
            try:
                path.write_text(source.replace(before, after, 1), encoding='utf-8')
                mutant = execute()
                detected = mutant.returncode == 1 and 'failed' in mutant.stdout.lower()
                results.append({'mutation': name, 'detected': detected})
                print(json.dumps(results[-1]), flush=True)
                if not detected:
                    print(mutant.stdout, mutant.stderr, flush=True)
            finally:
                path.write_text(source, encoding='utf-8')
            restored = execute()
            if restored.returncode:
                raise RuntimeError(f'{name} restored source failed:\n{restored.stdout}\n{restored.stderr}')
        assert all(item['detected'] for item in results), results
        print(f'{len(results)}/{len(MUTATIONS)} mutations detected', flush=True)


if __name__ == '__main__':
    run()
