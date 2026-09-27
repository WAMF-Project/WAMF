from copy import deepcopy
import multiprocessing
from pathlib import Path
import re

import pytest
import yaml

from app import config_persistence


def _hold_config_lock(config_path, ready, release):
    with config_persistence.ConfigPersistenceTransaction(config_path):
        ready.set()
        release.wait(5)


def _mode(path):
    return Path(path).stat().st_mode & 0o777


def test_atomic_write_preserves_mapping_and_creates_restrictive_backup(tmp_path):
    config_path = tmp_path / "config.yml"
    previous = "# previous config\nvalue: old\n"
    config_path.write_text(previous)
    config_path.chmod(0o644)
    candidate = {"value": "new", "nested": {"enabled": True}}
    original = deepcopy(candidate)

    result = config_persistence.persist_config(candidate, config_path)

    assert yaml.safe_load(config_path.read_text()) == candidate
    assert candidate == original
    assert _mode(config_path) == 0o600
    assert result.backup_path.read_text() == previous
    assert _mode(result.backup_path) == 0o600
    assert _mode(f"{config_path}.lock") == 0o600
    assert re.fullmatch(
        r"config\.yml\.\d{8}-\d{6}-\d{6}\.bak",
        result.backup_path.name,
    )
    assert not list(tmp_path.glob(".config.yml.*.tmp"))


def test_rendered_yaml_preserves_existing_representation(tmp_path):
    config_path = tmp_path / "config.yml"
    config_path.write_text("value: old\n")
    rendered = "# retained comment\nvalue: new\n"

    config_persistence.persist_config(
        {"value": "new"},
        config_path,
        rendered_content=rendered,
    )

    assert config_path.read_text() == rendered


def test_serialization_failure_leaves_active_config_untouched(tmp_path):
    config_path = tmp_path / "config.yml"
    previous = b"value: old\n"
    config_path.write_bytes(previous)

    with pytest.raises(config_persistence.ConfigSerializationError):
        config_persistence.persist_config({"value": object()}, config_path)

    assert config_path.read_bytes() == previous
    assert config_persistence.get_config_backup_paths(config_path) == []


def test_temporary_write_failure_cleans_up_and_leaves_active_config(
    tmp_path,
    monkeypatch,
):
    config_path = tmp_path / "config.yml"
    previous = b"value: old\n"
    config_path.write_bytes(previous)
    monkeypatch.setattr(
        config_persistence.os,
        "fsync",
        lambda _descriptor: (_ for _ in ()).throw(OSError("write failed")),
    )

    with pytest.raises(config_persistence.ConfigWriteError):
        config_persistence.persist_config(
            {"value": "new"},
            config_path,
            create_backup=False,
        )

    assert config_path.read_bytes() == previous
    assert not list(tmp_path.glob(".config.yml.*.tmp"))


def test_backup_failure_leaves_active_config_untouched(tmp_path, monkeypatch):
    config_path = tmp_path / "config.yml"
    previous = b"value: old\n"
    config_path.write_bytes(previous)

    def fail_backup(_config_path):
        raise config_persistence.ConfigBackupError("Backup failed.")

    monkeypatch.setattr(config_persistence, "_create_backup", fail_backup)

    with pytest.raises(config_persistence.ConfigBackupError):
        config_persistence.persist_config({"value": "new"}, config_path)

    assert config_path.read_bytes() == previous
    assert not list(tmp_path.glob(".config.yml.*.tmp"))


def test_replacement_failure_cleans_up_and_leaves_active_config(
    tmp_path,
    monkeypatch,
):
    config_path = tmp_path / "config.yml"
    previous = b"value: old\n"
    config_path.write_bytes(previous)

    def fail_replace(_source, _target):
        raise OSError("replace failed")

    monkeypatch.setattr(config_persistence.os, "replace", fail_replace)

    with pytest.raises(config_persistence.ConfigWriteError):
        config_persistence.persist_config({"value": "new"}, config_path)

    assert config_path.read_bytes() == previous
    assert not list(tmp_path.glob(".config.yml.*.tmp"))


def test_process_lock_timeout_is_controlled_and_does_not_mutate(tmp_path):
    config_path = tmp_path / "config.yml"
    previous = b"value: old\n"
    config_path.write_bytes(previous)
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    release = context.Event()
    holder = context.Process(
        target=_hold_config_lock,
        args=(config_path, ready, release),
    )
    holder.start()
    try:
        assert ready.wait(2)
        with pytest.raises(config_persistence.ConfigLockTimeoutError):
            config_persistence.persist_config(
                {"value": "new"},
                config_path,
                lock_timeout=0,
            )
        assert config_path.read_bytes() == previous
    finally:
        release.set()
        holder.join(2)
        if holder.is_alive():
            holder.terminate()
            holder.join(2)
    assert holder.exitcode == 0


def test_persistence_errors_do_not_expose_config_values(tmp_path):
    secret = "SENTINEL-CONFIG-SECRET"
    config_path = tmp_path / "config.yml"
    config_path.write_text("value: old\n")

    with pytest.raises(config_persistence.ConfigSerializationError) as error:
        config_persistence.persist_config(
            {"password": secret},
            config_path,
            rendered_content="password: different\n",
        )

    assert secret not in str(error.value)
