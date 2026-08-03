"""Host-profile model and store for whole-instance migration.

A profile names a machine and how to reach it. It deliberately holds NO secret:
only a path to a private key. The store is 0600 anyway, because the set of
machines you can reach is itself worth protecting.
"""

import json
import stat

import pytest


class TestValidateTarget:
    def test_minimal_profile_normalizes(self):
        from hermes_cli.migration_admin import validate_target

        got = validate_target({"id": "prod", "host": "10.0.0.5", "user": "hermes"})
        assert got["id"] == "prod"
        assert got["host"] == "10.0.0.5"
        assert got["user"] == "hermes"
        assert got["port"] == 22, "port must default to 22"
        assert got["label"] == "prod", "label defaults to the id"
        assert got["host_fingerprint"] is None, "unknown until first preflight"

    def test_id_must_be_a_slug(self):
        from hermes_cli.migration_admin import validate_target

        # The id becomes a filename-safe key and appears in URLs.
        for bad in ("has space", "has/slash", "", "UPPER"):
            with pytest.raises(ValueError, match="id"):
                validate_target({"id": bad, "host": "h", "user": "u"})

    def test_host_and_user_are_required(self):
        from hermes_cli.migration_admin import validate_target

        with pytest.raises(ValueError, match="host"):
            validate_target({"id": "a", "user": "u"})
        with pytest.raises(ValueError, match="user"):
            validate_target({"id": "a", "host": "h"})

    def test_port_must_be_a_valid_tcp_port(self):
        from hermes_cli.migration_admin import validate_target

        for bad in (0, 65536, -1, "twenty-two"):
            with pytest.raises(ValueError, match="port"):
                validate_target({"id": "a", "host": "h", "user": "u", "port": bad})

    def test_password_is_rejected_outright(self):
        from hermes_cli.migration_admin import validate_target

        # Supporting passwords would force a plaintext secret into the store to
        # save one ssh-copy-id. Reject the key rather than silently drop it, so
        # nobody believes they configured something that is not in effect.
        with pytest.raises(ValueError, match="password"):
            validate_target(
                {"id": "a", "host": "h", "user": "u", "password": "hunter2"}
            )

    def test_identity_file_is_expanded_not_read(self):
        from hermes_cli.migration_admin import validate_target

        got = validate_target(
            {"id": "a", "host": "h", "user": "u", "identity_file": "~/.ssh/id_ed25519"}
        )
        assert got["identity_file"].endswith("/.ssh/id_ed25519")
        assert not got["identity_file"].startswith("~"), "must be expanded"


class TestTargetsStore:
    def test_missing_file_reads_as_empty(self, tmp_path):
        from hermes_cli.migration_admin import load_targets

        assert load_targets(tmp_path / "nope.json") == {}

    def test_corrupt_file_reads_as_empty(self, tmp_path):
        from hermes_cli.migration_admin import load_targets

        # A hand-mangled store must not take down the whole page.
        p = tmp_path / "migration_targets.json"
        p.write_text("{not json", encoding="utf-8")
        assert load_targets(p) == {}

    def test_round_trip(self, tmp_path):
        from hermes_cli.migration_admin import load_targets, save_targets, validate_target

        p = tmp_path / "migration_targets.json"
        prof = validate_target({"id": "prod", "host": "h", "user": "u"})
        save_targets(p, {"prod": prof})
        assert load_targets(p) == {"prod": prof}

    def test_store_is_written_0600(self, tmp_path):
        from hermes_cli.migration_admin import save_targets, validate_target

        p = tmp_path / "migration_targets.json"
        save_targets(p, {"prod": validate_target({"id": "prod", "host": "h", "user": "u"})})
        assert stat.S_IMODE(p.stat().st_mode) == 0o600

    def test_saved_json_carries_no_secret_fields(self, tmp_path):
        from hermes_cli.migration_admin import save_targets, validate_target

        p = tmp_path / "migration_targets.json"
        save_targets(p, {"prod": validate_target(
            {"id": "prod", "host": "h", "user": "u", "identity_file": "/k/id"}
        )})
        raw = json.loads(p.read_text(encoding="utf-8"))
        assert "password" not in json.dumps(raw)
        assert raw["prod"]["identity_file"] == "/k/id", "path only, never key material"
