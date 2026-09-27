import os
from pathlib import Path

import pytest
import yaml
from werkzeug.security import generate_password_hash

from app import bootstrap, config_editor, config_persistence
from app.config_migration import CURRENT_CONFIG_VERSION
from app.config_persistence import ConfigPersistenceTransaction, ConfigWriteError


@pytest.fixture
def migration_path(tmp_path, monkeypatch):
    path = tmp_path / "config.yml"
    monkeypatch.setenv("WHOSATMYFEEDER_CONFIG", str(path))
    monkeypatch.delenv("WAMF_SECRET_KEY", raising=False)
    return path


def embedded_config(secret="MIGRATION-SECRET"):
    return {
        "config_version": CURRENT_CONFIG_VERSION,
        "mqtt": {
            "host": "broker",
            "topic_prefix": "frigate",
            "authentication": {
                "enabled": True,
                "username": "bird-user",
                "password": secret,
            },
        },
        "admin": {
            "auth_enabled": True,
            "session_secret": "stable-session",
            "password_hash": generate_password_hash("existing-password"),
        },
        "api": {"token_auth_enabled": True, "token_hash": "api-token-hash"},
        "retention": {"config_backups_max_files": 2},
    }


def test_extraction_persists_secrets_before_sanitized_config(
    migration_path, monkeypatch
):
    migration_path.write_text(yaml.safe_dump(embedded_config()), encoding="utf-8")
    writes = []
    real_transaction = ConfigPersistenceTransaction

    class TrackingTransaction(real_transaction):
        def write(self, *args, **kwargs):
            writes.append(self.config_path.name)
            return super().write(*args, **kwargs)

    monkeypatch.setattr(bootstrap, "ConfigPersistenceTransaction", TrackingTransaction)
    runtime = bootstrap.bootstrap_config()

    assert writes == ["secrets.yml", "config.yml"]
    assert runtime["mqtt"]["authentication"]["password"] == "MIGRATION-SECRET"
    persisted = yaml.safe_load(migration_path.read_text())
    assert persisted["mqtt"]["authentication"] == {"enabled": True}
    assert persisted["admin"] == {"auth_enabled": True}
    assert persisted["api"] == {"token_auth_enabled": True}


def test_secrets_write_failure_never_changes_config(migration_path, monkeypatch):
    original = yaml.safe_dump(embedded_config(), sort_keys=False)
    migration_path.write_text(original, encoding="utf-8")
    real_transaction = ConfigPersistenceTransaction

    class FailSecretsTransaction(real_transaction):
        def write(self, *args, **kwargs):
            if self.config_path.name == "secrets.yml":
                raise ConfigWriteError("controlled secrets failure")
            return super().write(*args, **kwargs)

    monkeypatch.setattr(bootstrap, "ConfigPersistenceTransaction", FailSecretsTransaction)
    with pytest.raises(ConfigWriteError, match="controlled secrets failure"):
        bootstrap.bootstrap_config()
    assert migration_path.read_text() == original
    assert not (migration_path.parent / "secrets.yml").exists()
    assert config_editor.get_config_backup_paths(migration_path) == []


def test_partial_migration_recovers_without_reverting_or_rewriting_secrets(
    migration_path, monkeypatch
):
    original = yaml.safe_dump(embedded_config("NEW-EMBEDDED"), sort_keys=False)
    migration_path.write_text(original, encoding="utf-8")
    real_transaction = ConfigPersistenceTransaction

    class FailConfigTransaction(real_transaction):
        def write(self, *args, **kwargs):
            if self.config_path == migration_path:
                raise ConfigWriteError("controlled config failure")
            return super().write(*args, **kwargs)

    monkeypatch.setattr(bootstrap, "ConfigPersistenceTransaction", FailConfigTransaction)
    with pytest.raises(ConfigWriteError, match="controlled config failure"):
        bootstrap.bootstrap_config()

    secrets_path = migration_path.parent / "secrets.yml"
    first_secrets = secrets_path.read_bytes()
    assert yaml.safe_load(first_secrets)["mqtt"]["password"] == "NEW-EMBEDDED"
    assert migration_path.read_text() == original

    # Simulate an older embedded value after the interrupted first attempt.
    older = embedded_config("OLDER-EMBEDDED")
    migration_path.write_text(yaml.safe_dump(older, sort_keys=False), encoding="utf-8")
    monkeypatch.setattr(bootstrap, "ConfigPersistenceTransaction", real_transaction)
    runtime = bootstrap.bootstrap_config()
    assert runtime["mqtt"]["authentication"]["password"] == "NEW-EMBEDDED"
    assert secrets_path.read_bytes() == first_secrets
    assert config_editor.get_config_backup_paths(secrets_path) == []
    assert "password" not in yaml.safe_load(migration_path.read_text())["mqtt"]["authentication"]

    config_after_recovery = migration_path.read_bytes()
    config_backups = config_editor.get_config_backup_paths(migration_path)
    runtime_again = bootstrap.bootstrap_config()
    assert runtime_again == runtime
    assert migration_path.read_bytes() == config_after_recovery
    assert secrets_path.read_bytes() == first_secrets
    assert config_editor.get_config_backup_paths(migration_path) == config_backups
    assert config_editor.get_config_backup_paths(secrets_path) == []


def test_v1_alias_migration_precedes_secret_extraction(migration_path):
    legacy = {
        "frigate": {
            "mqtt_server": "legacy-broker",
            "mqtt_port": 1884,
            "main_topic": "legacy-topic",
            "mqtt_auth": True,
            "mqtt_username": "legacy-user",
            "mqtt_password": "legacy-password",
        },
        "admin": {"auth_enabled": False},
    }
    migration_path.write_text(yaml.safe_dump(legacy, sort_keys=False), encoding="utf-8")
    runtime = bootstrap.bootstrap_config()
    persisted = yaml.safe_load(migration_path.read_text())
    secrets = yaml.safe_load((migration_path.parent / "secrets.yml").read_text())

    assert persisted["config_version"] == 2
    assert persisted["mqtt"] == {
        "host": "legacy-broker",
        "port": 1884,
        "topic_prefix": "legacy-topic",
        "authentication": {"enabled": True},
    }
    assert not any(key.startswith("mqtt_") for key in persisted["frigate"])
    assert secrets["mqtt"] == {
        "username": "legacy-user",
        "password": "legacy-password",
    }
    assert runtime["mqtt"]["authentication"]["username"] == "legacy-user"


def test_active_lock_backup_and_config_migration_backup_permissions(migration_path):
    sentinel = "PROTECTED-BACKUP-SECRET"
    original = yaml.safe_dump(embedded_config(sentinel), sort_keys=False)
    migration_path.write_text(original, encoding="utf-8")
    migration_path.chmod(0o644)
    bootstrap.bootstrap_config()

    secrets_path = migration_path.parent / "secrets.yml"
    artifacts = [
        migration_path,
        Path(f"{migration_path}.lock"),
        secrets_path,
        Path(f"{secrets_path}.lock"),
    ]
    config_backups = [Path(path) for path in config_editor.get_config_backup_paths(migration_path)]
    assert len(config_backups) == 1
    assert sentinel in config_backups[0].read_text()
    artifacts.extend(config_backups)
    assert all((path.stat().st_mode & 0o777) == 0o600 for path in artifacts)


def test_secrets_backups_are_private_and_follow_retention(tmp_path):
    path = tmp_path / "secrets.yml"
    for value in range(5):
        config_persistence.persist_config(
            {"secrets_version": 1, "api": {"token_hash": f"hash-{value}"}},
            path,
            backup_limit=2,
        )
    backups = [Path(item) for item in config_persistence.get_config_backup_paths(path)]
    assert len(backups) == 2
    assert all((item.stat().st_mode & 0o777) == 0o600 for item in backups)
    assert (path.stat().st_mode & 0o777) == 0o600


def test_secret_temporary_file_is_0600(tmp_path, monkeypatch):
    path = tmp_path / "secrets.yml"
    observed_modes = []
    real_replace = os.replace

    def inspect_then_replace(source, target):
        observed_modes.append(Path(source).stat().st_mode & 0o777)
        return real_replace(source, target)

    monkeypatch.setattr(config_persistence.os, "replace", inspect_then_replace)
    config_persistence.persist_config(
        {"secrets_version": 1, "admin": {"session_secret": "secret"}},
        path,
    )
    assert observed_modes == [0o600]


def test_first_secrets_creation_has_no_backup(migration_path):
    migration_path.write_text(
        yaml.safe_dump({"config_version": 2, "admin": {"auth_enabled": False}}),
        encoding="utf-8",
    )
    bootstrap.bootstrap_config()
    secrets_path = migration_path.parent / "secrets.yml"
    assert yaml.safe_load(secrets_path.read_text()) == {"secrets_version": 1}
    assert config_editor.get_config_backup_paths(secrets_path) == []


def test_startup_error_and_logs_do_not_expose_malformed_secret_value(
    migration_path, caplog
):
    sentinel = "STARTUP-SECRET-SENTINEL"
    migration_path.write_text(
        yaml.safe_dump({"config_version": 2, "admin": {"auth_enabled": False}}),
        encoding="utf-8",
    )
    (migration_path.parent / "secrets.yml").write_text(
        f"secrets_version: 1\nmqtt: [{sentinel}\n",
        encoding="utf-8",
    )

    with pytest.raises(SystemExit) as error:
        bootstrap.prepare_native_startup()

    assert sentinel not in str(error.value)
    assert sentinel not in caplog.text
