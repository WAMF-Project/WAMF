import os
from pathlib import Path

import yaml

from app import config_editor
from app.config_migration import CURRENT_CONFIG_VERSION


def _write_backup(path, age_offset):
    path.write_text("backup\n")
    timestamp = 1_700_000_000 + age_offset
    os.utime(path, (timestamp, timestamp))


def test_write_config_preserving_admin_prunes_old_config_backups(
    monkeypatch,
    tmp_path,
):
    config_path = tmp_path / "config.yml"
    config_path.write_text("""
frigate:
  mqtt_server: localhost
retention:
  config_backups_max_files: 2
admin:
  auth_enabled: true
  session_secret: keep-me
  password_hash: keep-hash
api:
  token_auth_enabled: true
  token_hash: keep-token
""".lstrip())
    monkeypatch.setenv("WHOSATMYFEEDER_CONFIG", str(config_path))

    oldest_backup = tmp_path / "config.yml.20260101-000000.bak"
    middle_backup = tmp_path / "config.yml.20260102-000000.bak"
    newest_backup = tmp_path / "config.yml.20260103-000000.bak"
    _write_backup(oldest_backup, 1)
    _write_backup(middle_backup, 2)
    _write_backup(newest_backup, 3)

    config_editor.write_config_preserving_admin("""
frigate:
  mqtt_server: mqtt.local
retention:
  config_backups_max_files: 2
""".lstrip())

    backup_names = {
        Path(path).name
        for path in config_editor.get_config_backup_paths(str(config_path))
    }
    updated = yaml.safe_load(config_path.read_text())

    assert len(backup_names) == 2
    assert oldest_backup.name not in backup_names
    assert middle_backup.name not in backup_names
    assert newest_backup.name in backup_names
    assert updated["config_version"] == CURRENT_CONFIG_VERSION
    assert updated["mqtt"]["host"] == "mqtt.local"
    assert "mqtt_server" not in updated["frigate"]
    assert updated["admin"] == {"auth_enabled": True}
    assert updated["api"] == {"token_auth_enabled": True}
    secrets = yaml.safe_load((tmp_path / "secrets.yml").read_text())
    assert secrets["admin"]["session_secret"] == "keep-me"
    assert secrets["admin"]["password_hash"] == "keep-hash"
    assert secrets["api"]["token_hash"] == "keep-token"


def test_prune_config_backups_allows_zero_retained_backups(tmp_path):
    config_path = tmp_path / "config.yml"
    config_path.write_text("config_version: 2\nmqtt:\n  host: localhost\n")
    backup_path = tmp_path / "config.yml.20260101-000000.bak"
    backup_path.write_text("backup\n")

    deleted_count = config_editor.prune_config_backups(
        str(config_path),
        max_files=0,
    )

    assert deleted_count == 1
    assert not backup_path.exists()


def test_raw_editor_blank_mqtt_secrets_preserve_and_new_values_replace(
    monkeypatch, tmp_path
):
    config_path = tmp_path / "config.yml"
    config_path.write_text(
        "config_version: 2\n"
        "mqtt:\n"
        "  host: broker\n"
        "  authentication:\n"
        "    enabled: true\n",
        encoding="utf-8",
    )
    secrets_path = tmp_path / "secrets.yml"
    secrets_path.write_text(
        "secrets_version: 1\n"
        "mqtt:\n"
        "  username: existing-user\n"
        "  password: existing-password\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("WHOSATMYFEEDER_CONFIG", str(config_path))

    config_editor.write_config_preserving_admin(
        "config_version: 2\n"
        "mqtt:\n"
        "  host: broker\n"
        "  authentication:\n"
        "    enabled: true\n"
        "    username: ''\n"
        "    password: ''\n"
    )
    assert yaml.safe_load(secrets_path.read_text())["mqtt"] == {
        "username": "existing-user",
        "password": "existing-password",
    }

    config_editor.write_config_preserving_admin(
        "config_version: 2\n"
        "mqtt:\n"
        "  host: broker\n"
        "  authentication:\n"
        "    enabled: true\n"
        "    username: replacement-user\n"
        "    password: replacement-password\n"
    )
    stored = yaml.safe_load(secrets_path.read_text())
    assert stored["mqtt"] == {
        "username": "replacement-user",
        "password": "replacement-password",
    }
    persisted = yaml.safe_load(config_path.read_text())
    assert persisted["mqtt"]["authentication"] == {"enabled": True}
