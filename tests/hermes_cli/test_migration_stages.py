"""Stage order, failure classification, installer selection.

The load-bearing property under test: the source is only ever stopped, never
modified. Everything an operator is told about recovery derives from knowing
whether a given stage runs before or after the stop.
"""

import pytest


class TestStageOrder:
    def test_install_precedes_stopping_the_source(self):
        from hermes_cli.migration_admin import STAGES

        # Install depends on no data and is the most failure-prone step
        # (network, dependencies, permissions). Running it while the source is
        # still serving makes that failure nearly free.
        assert STAGES.index("install") < STAGES.index("stop_source")

    def test_verify_is_last_and_start_is_absent(self):
        from hermes_cli.migration_admin import STAGES

        # The run halts at a verified-but-idle target; promotion is a human
        # decision, so there is deliberately no "start" stage.
        assert STAGES[-1] == "verify"
        assert "start" not in STAGES

    def test_expected_sequence(self):
        from hermes_cli.migration_admin import STAGES

        assert STAGES == (
            "install", "stop_source", "backup", "transfer", "restore", "verify",
        )


class TestFailureClassification:
    def test_source_still_running_before_the_stop(self):
        from hermes_cli.migration_admin import source_is_stopped_at

        assert source_is_stopped_at("install") is False

    def test_source_stopped_from_the_stop_onward(self):
        from hermes_cli.migration_admin import source_is_stopped_at

        for stage in ("stop_source", "backup", "transfer", "restore", "verify"):
            assert source_is_stopped_at(stage) is True, stage

    def test_recovery_text_before_stop_does_not_mention_restarting(self):
        from hermes_cli.migration_admin import recovery_for

        assert "restart" not in recovery_for("install").lower()

    def test_recovery_after_stop_tells_you_to_restart_the_source(self):
        from hermes_cli.migration_admin import recovery_for

        assert "restart" in recovery_for("transfer").lower()

    def test_restore_failure_warns_that_the_target_is_half_populated(self):
        from hermes_cli.migration_admin import recovery_for

        # hermes import overwrites, so a failed restore leaves a partial home
        # that must be cleared before retrying.
        text = recovery_for("restore").lower()
        assert "restart" in text
        assert "clear" in text or "empty" in text

    def test_unknown_stage_is_rejected(self):
        from hermes_cli.migration_admin import recovery_for

        with pytest.raises(ValueError, match="stage"):
            recovery_for("nonsense")


class TestInstallCommand:
    def test_linux_and_macos_use_the_shell_installer(self):
        from hermes_cli.migration_admin import install_command

        for os_name in ("linux", "macos"):
            cmd = install_command({"os": os_name, "arch": "x86_64"})
            assert "install.sh" in cmd

    def test_windows_uses_powershell(self):
        from hermes_cli.migration_admin import install_command

        cmd = install_command({"os": "windows", "arch": "AMD64"})
        assert "install.ps1" in cmd
        assert "powershell" in cmd.lower()

    def test_unknown_os_raises_rather_than_guessing(self):
        from hermes_cli.migration_admin import install_command

        with pytest.raises(ValueError, match="unsupported"):
            install_command({"os": "plan9", "arch": "x86"})


class TestMigrationAborted:
    def test_carries_the_stage_it_failed_at(self):
        from hermes_cli.migration_admin import MigrationAborted

        exc = MigrationAborted("transfer", "connection reset")
        assert exc.stage == "transfer"
        assert "connection reset" in str(exc)
