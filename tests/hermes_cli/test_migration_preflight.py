"""Preflight verdicts, driven by a fake executor.

Preflight must never modify the target, so every check here is a read. The
two-tier split matters: blocking means "this migration cannot succeed",
warning means "this will probably work but you should look".
"""

import pytest


class FakeResult:
    def __init__(self, rc=0, stdout="", stderr=""):
        self.rc, self.stdout, self.stderr = rc, stdout, stderr


class FakeExecutor:
    """Answers commands from a dict of substring -> FakeResult."""

    def __init__(self, responses=None, os_info=None):
        self.responses = responses or {}
        self.os_info = os_info or {"os": "linux", "arch": "x86_64"}
        self.commands = []

    def run(self, command, timeout=60):
        self.commands.append(command)
        for needle, result in self.responses.items():
            if needle in command:
                return result
        return FakeResult(rc=0, stdout="")

    def detect_os(self):
        return self.os_info


def _by_name(results):
    return {r.name: r for r in results}


class TestPreflightBlocking:
    def test_passes_on_a_clean_empty_target(self):
        from hermes_cli.migration_admin import preflight_blocks, run_preflight

        ex = FakeExecutor({
            "df ": FakeResult(stdout="100000000\n"),   # plenty of free bytes
            "command -v python3": FakeResult(rc=0, stdout="/usr/bin/python3\n"),
            "ls -A": FakeResult(rc=0, stdout=""),      # target home empty
        })
        results = run_preflight(
            ex, target_home="/home/h/.hermes", archive_bytes=1000, source_version="0.19.0"
        )
        assert not preflight_blocks(results)

    def test_missing_python3_blocks(self):
        from hermes_cli.migration_admin import preflight_blocks, run_preflight

        ex = FakeExecutor({
            "df ": FakeResult(stdout="100000000\n"),
            "command -v python3": FakeResult(rc=1, stdout=""),
            "ls -A": FakeResult(rc=0, stdout=""),
        })
        results = run_preflight(
            ex, target_home="/h", archive_bytes=1000, source_version="0.19.0"
        )
        assert preflight_blocks(results)
        assert _by_name(results)["python3"].tier == "blocking"

    def test_insufficient_space_blocks_and_requires_twice_the_archive(self):
        from hermes_cli.migration_admin import run_preflight

        # 1500 free vs a 1000-byte archive: enough to hold it once, not enough
        # to hold it while unpacking. The check is 2x for that reason.
        ex = FakeExecutor({
            "df ": FakeResult(stdout="1500\n"),
            "command -v python3": FakeResult(rc=0),
            "ls -A": FakeResult(rc=0, stdout=""),
        })
        disk = _by_name(run_preflight(
            ex, target_home="/h", archive_bytes=1000, source_version="0.19.0"
        ))["disk_space"]
        assert disk.ok is False
        assert disk.tier == "blocking"

    def test_pristine_target_home_passes(self):
        from hermes_cli.migration_admin import run_preflight

        # A target where Hermes was just installed has a config.yaml and
        # nothing else. Rejecting that would reject the whole "already
        # installed" case the feature exists to support.
        ex = FakeExecutor({
            "df ": FakeResult(stdout="100000000\n"),
            "command -v python3": FakeResult(rc=0),
            "ls -A": FakeResult(rc=0, stdout="config.yaml\n"),
        })
        home = _by_name(run_preflight(
            ex, target_home="/h", archive_bytes=1000, source_version="0.19.0"
        ))["target_home"]
        assert home.ok is True

    def test_target_home_with_real_state_blocks(self):
        from hermes_cli.migration_admin import run_preflight

        # auth.json means someone is logged in on that box. Overwriting it
        # destroys their instance, so it blocks until explicitly confirmed.
        ex = FakeExecutor({
            "df ": FakeResult(stdout="100000000\n"),
            "command -v python3": FakeResult(rc=0),
            "ls -A": FakeResult(rc=0, stdout="config.yaml\nauth.json\nsessions\n"),
        })
        home = _by_name(run_preflight(
            ex, target_home="/h", archive_bytes=1000, source_version="0.19.0"
        ))["target_home"]
        assert home.ok is False
        assert home.tier == "blocking"

    def test_nonexistent_target_home_checks_parent_filesystem(self):
        from hermes_cli.migration_admin import run_preflight

        # target_home=/h/doesnotexist doesn't exist, but parent filesystem has space.
        # df command walks up to find /h which exists.
        # This should NOT produce a blocking disk_space failure.
        ex = FakeExecutor({
            "df ": FakeResult(stdout="100000000\n"),  # Walk-up finds /h, df on it
            "command -v python3": FakeResult(rc=0),
            "ls -A": FakeResult(rc=0, stdout=""),     # /h/doesnotexist is absent
        })
        results = run_preflight(
            ex, target_home="/h/doesnotexist", archive_bytes=1000, source_version="0.19.0"
        )
        by_name = _by_name(results)
        # target_home absent is OK (fresh install case)
        assert by_name["target_home"].ok is True
        # disk_space must not block just because target_home doesn't exist
        assert by_name["disk_space"].ok is True
        assert by_name["disk_space"].tier == "blocking"


class TestPreflightWarnings:
    def test_clock_skew_warns_but_does_not_block(self):
        from hermes_cli.migration_admin import preflight_blocks, run_preflight

        # auth.json holds expiry-sensitive OAuth tokens: a skewed target shows
        # up as "login expired", which is near-impossible to diagnose after the
        # fact and trivial to detect now.
        import time

        skewed = int(time.time()) + 600
        ex = FakeExecutor({
            "df ": FakeResult(stdout="100000000\n"),
            "command -v python3": FakeResult(rc=0),
            "ls -A": FakeResult(rc=0, stdout=""),
            "date +%s": FakeResult(stdout=f"{skewed}\n"),
        })
        results = run_preflight(
            ex, target_home="/h", archive_bytes=1000, source_version="0.19.0"
        )
        clock = _by_name(results)["clock_skew"]
        assert clock.ok is False
        assert clock.tier == "warning"
        assert not preflight_blocks(results), "a warning must not block"

    def test_version_mismatch_warns(self):
        from hermes_cli.migration_admin import run_preflight

        ex = FakeExecutor({
            "df ": FakeResult(stdout="100000000\n"),
            "command -v python3": FakeResult(rc=0),
            "ls -A": FakeResult(rc=0, stdout=""),
            "hermes version": FakeResult(rc=0, stdout="0.18.0\n"),
        })
        ver = _by_name(run_preflight(
            ex, target_home="/h", archive_bytes=1000, source_version="0.19.0"
        ))["hermes_version"]
        assert ver.tier == "warning"


class TestPreflightIsReadOnly:
    def test_no_check_mutates_the_target(self):
        from hermes_cli.migration_admin import run_preflight

        ex = FakeExecutor({
            "df ": FakeResult(stdout="100000000\n"),
            "command -v python3": FakeResult(rc=0),
            "ls -A": FakeResult(rc=0, stdout=""),
        })
        run_preflight(ex, target_home="/h", archive_bytes=1, source_version="0.19.0")
        forbidden = ("rm ", "mkdir", "mv ", "install", "cp ", ">", "chmod")
        for cmd in ex.commands:
            assert not any(f in cmd for f in forbidden), f"preflight mutated: {cmd}"
