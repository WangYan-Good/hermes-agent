"""CLI handlers for ``hermes migrate ...``.

Exposes ``hermes migrate xai`` — diagnoses and (with --apply) rewrites
references to xAI models retired on May 15, 2026 — and ``hermes migrate
host`` — migrates a whole Hermes instance to another host over SSH.
"""
from __future__ import annotations

import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Dict

from hermes_cli.colors import Colors, color
from hermes_cli.config import load_config
from hermes_cli.migration_admin import (
    MigrationAborted,
    install_command,
    load_targets,
    recovery_for,
    targets_path,
)

_REMOTE_ARCHIVE = "/tmp/hermes-migration.zip"


def cmd_migrate(args: Any) -> int:
    """Dispatcher for ``hermes migrate <subtype>``."""
    sub = getattr(args, "migrate_type", None)
    if sub == "xai":
        return cmd_migrate_xai(args)
    if sub == "host":
        return cmd_migrate_host(args)

    print("usage: hermes migrate [xai|host] ...", file=sys.stderr)
    return 2


def cmd_migrate_xai(args: Any) -> int:
    """Run xAI May-15 model migration in dry-run or apply mode."""
    from hermes_cli.xai_retirement import (
        MIGRATION_GUIDE_URL,
        RETIREMENT_DATE,
        apply_migration,
        find_retired_xai_refs,
        format_issue,
    )

    apply = bool(getattr(args, "apply", False))
    no_backup = bool(getattr(args, "no_backup", False))

    config = load_config()
    issues = find_retired_xai_refs(config)

    print()
    print(color(
        f"◆ xAI Model Retirement Migration ({RETIREMENT_DATE})",
        Colors.CYAN, Colors.BOLD,
    ))
    print()

    if not issues:
        print(f"  {color('✓', Colors.GREEN)} No retired xAI models in config — nothing to migrate.")
        return 0

    print(f"  Found {len(issues)} retired xAI model reference(s):")
    print()
    for issue in issues:
        print(f"    {color('⚠', Colors.YELLOW)} {format_issue(issue)}")
    print()
    print(f"    {color('→', Colors.CYAN)} Migration guide: {MIGRATION_GUIDE_URL}")
    print()

    config_path = _resolve_config_path()

    if not apply:
        print(color("Dry-run mode — no changes written.", Colors.DIM))
        print(color(
            "Re-run with `hermes migrate xai --apply` to rewrite "
            f"{config_path} in-place (backup created automatically).",
            Colors.DIM,
        ))
        return 0

    if not config_path or not config_path.exists():
        print(
            f"  {color('✗', Colors.RED)} Could not locate config.yaml "
            f"(looked at: {config_path})",
            file=sys.stderr,
        )
        return 1

    try:
        result = apply_migration(
            config_path=config_path,
            issues=issues,
            backup=not no_backup,
        )
    except Exception as exc:
        print(
            f"  {color('✗', Colors.RED)} Migration failed: {exc}",
            file=sys.stderr,
        )
        return 1

    if not result.config_changed:
        print(f"  {color('⚠', Colors.YELLOW)} No changes written.")
        return 0

    if result.backup_path is not None:
        print(f"  {color('✓', Colors.GREEN)} Backup: {result.backup_path}")
    print(
        f"  {color('✓', Colors.GREEN)} Updated {len(result.issues_resolved)} "
        f"slot(s) in {result.file_path}"
    )
    print()
    print(color(
        "Run `hermes doctor` to confirm no retired xAI models remain.",
        Colors.DIM,
    ))
    return 0


def _resolve_config_path() -> Path:
    """Best-effort: locate the active config.yaml on disk."""
    from hermes_cli.config import get_hermes_home

    return get_hermes_home() / "config.yaml"


def _run_source_backup(archive_path: str) -> int:
    """Produce a full backup at *archive_path* via a subprocess.

    Deliberately not an in-process call: ``run_backup()`` is a CLI entrypoint
    that calls ``sys.exit(1)`` on failure, which would kill this process and
    skip the archive cleanup in ``execute_migration``'s ``finally``.

    Full backup, not ``--quick``: quick captures only critical state and would
    silently drop skills/, memories, plans and projects — user content a
    migration must carry.
    """
    return subprocess.run(
        [sys.executable, "-m", "hermes_cli.main", "backup", "-o", archive_path],
        check=False,
    ).returncode


def _stop_source_gateway() -> int:
    """Stop this instance's gateway via a subprocess.

    Same reasoning as ``_run_source_backup``: ``hermes gateway stop`` can call
    ``sys.exit(1)`` itself (e.g. it refuses to stop the gateway from inside
    the gateway process), and an in-process call would take this process down
    with it, skipping the archive cleanup in ``execute_migration``'s
    ``finally``.
    """
    return subprocess.run(
        [sys.executable, "-m", "hermes_cli.main", "gateway", "stop"],
        check=False,
    ).returncode


def execute_migration(
    executor,
    profile: Dict[str, Any],
    *,
    home: Path,
    confirm_overwrite: bool,
    emit: Callable[[str, str, str], None],
) -> int:
    """Run every stage in order. Raises ``MigrationAborted`` on the first failure.

    The source is only ever stopped, never modified — that is what makes every
    failure recoverable by restarting it.
    """
    target_home = profile.get("target_home") or "~/.hermes"
    quoted_target_home = shlex.quote(target_home)
    archive = Path(home) / f"hermes-migration-{os.getpid()}.zip"

    def _step(stage: str, detail: str = "") -> None:
        emit(stage, "start", detail)

    def _fail(stage: str, detail: str) -> None:
        emit(stage, "fail", f"{detail} — {recovery_for(stage)}")
        raise MigrationAborted(stage, detail)

    try:
        # 1. install (source untouched, still serving)
        _step("install")
        cmd = install_command(executor.detect_os())
        got = executor.run(f"command -v hermes >/dev/null 2>&1 || ({cmd})", timeout=1800)
        if got.rc != 0:
            _fail("install", got.stderr.strip() or "installer returned non-zero")
        emit("install", "ok", "")

        # 2. stop the source — downtime starts here
        _step("stop_source")
        stop_rc = _stop_source_gateway()
        if stop_rc != 0:
            _fail("stop_source", f"gateway stop exited {stop_rc}")
        emit("stop_source", "ok", "")

        # 3. backup
        _step("backup")
        if _run_source_backup(str(archive)) != 0 or not archive.is_file():
            _fail("backup", "hermes backup did not produce an archive")
        os.chmod(archive, 0o600)
        emit("backup", "ok", f"{archive.stat().st_size} bytes")

        # 4. transfer
        _step("transfer")
        put = executor.put_file(archive, _REMOTE_ARCHIVE)
        if put.rc != 0:
            _fail("transfer", put.stderr.strip() or "transfer failed")
        executor.run(f"chmod 600 {_REMOTE_ARCHIVE}")
        emit("transfer", "ok", "")

        # 5. restore
        _step("restore")
        flag = " --force" if confirm_overwrite else ""
        got = executor.run(f"hermes import{flag} {_REMOTE_ARCHIVE}", timeout=1800)
        if got.rc != 0:
            _fail("restore", got.stderr.strip() or "hermes import returned non-zero")
        emit("restore", "ok", "")

        # 6. verify — read-only assertions about what landed
        _step("verify")
        checks = executor.run(
            f"test -f {quoted_target_home}/config.yaml && "
            f"stat -c '%a' {quoted_target_home}/auth.json 2>/dev/null || echo missing"
        )
        emit("verify", "ok", checks.stdout.strip())
        return 0
    finally:
        # The archive holds plaintext .env and auth.json. Remove both copies on
        # every path, including failures.
        try:
            archive.unlink(missing_ok=True)
        except OSError:
            pass
        try:
            executor.run(f"rm -f {_REMOTE_ARCHIVE}")
        except Exception:  # noqa: BLE001 - cleanup must never mask the real error
            pass


def cmd_migrate_host(args: Any) -> int:
    """``hermes migrate host <id> [--confirm-overwrite]``."""
    from hermes_constants import get_default_hermes_root
    from hermes_cli.remote_exec import SshExecutor

    home = get_default_hermes_root()
    targets = load_targets(targets_path(home))
    profile = targets.get(args.target_id)
    if not profile:
        print(f"Unknown migration target: {args.target_id}", file=sys.stderr)
        return 2

    executor = SshExecutor(profile)

    def emit(stage: str, status: str, detail: str) -> None:
        print(f"[{stage}] {status} {detail}".rstrip(), flush=True)

    try:
        return execute_migration(
            executor, profile, home=home,
            confirm_overwrite=bool(getattr(args, "confirm_overwrite", False)),
            emit=emit,
        )
    except MigrationAborted as exc:
        print(f"Migration aborted at {exc.stage}: {exc.detail}", file=sys.stderr)
        print(recovery_for(exc.stage), file=sys.stderr)
        return 1
