from copy import deepcopy
from pathlib import Path

import pytest
import yaml

from app import config_migration
from app.config_normalization import normalize_config
from app.config_validation import validate_config
from app.mqtt_settings import mqtt_settings_from_config


LEGACY_KEYS = {legacy for legacy, _ in config_migration.LEGACY_MQTT_LEAVES}


def complete_legacy_config():
    return {
        "frigate": {
            "frigate_url": "http://frigate",
            "camera": ["birdcam"],
            "object": "bird",
            "mqtt_server": "legacy-broker",
            "mqtt_port": 1884,
            "main_topic": "legacy-topic",
            "mqtt_auth": True,
            "mqtt_username": "legacy-user",
            "mqtt_password": "legacy-password",
            "mqtt_use_tls": True,
            "mqtt_tls_insecure": True,
            "mqtt_tls_ca_certs": "/legacy/ca.pem",
            "unrelated_frigate": "preserved",
        },
        "classification": {"model": "model.tflite", "threshold": 0.7},
        "unrelated": {"keep": ["exactly", "this"]},
    }


def runtime_settings(config):
    return mqtt_settings_from_config(normalize_config(config).config)


def test_unversioned_legacy_config_migrates_all_mqtt_fields_without_mutation():
    source = complete_legacy_config()
    original = deepcopy(source)

    result = config_migration.migrate_config(source)

    assert result.source_version == config_migration.LEGACY_CONFIG_VERSION
    assert result.target_version == config_migration.CURRENT_CONFIG_VERSION
    assert result.migrated is True
    assert result.config["config_version"] == config_migration.CURRENT_CONFIG_VERSION
    assert result.config["mqtt"] == {
        "host": "legacy-broker",
        "port": 1884,
        "topic_prefix": "legacy-topic",
        "authentication": {
            "enabled": True,
            "username": "legacy-user",
            "password": "legacy-password",
        },
        "tls": {
            "enabled": True,
            "insecure": True,
            "ca_certs": "/legacy/ca.pem",
        },
    }
    assert not LEGACY_KEYS.intersection(result.config["frigate"])
    assert result.config["frigate"] == {
        "frigate_url": "http://frigate",
        "camera": ["birdcam"],
        "object": "bird",
        "unrelated_frigate": "preserved",
    }
    assert result.config["unrelated"] == source["unrelated"]
    assert source == original


def test_current_configuration_is_unchanged_and_migration_is_idempotent():
    current = config_migration.migrate_config(complete_legacy_config()).config
    original = deepcopy(current)

    second = config_migration.migrate_config(current)

    assert second.migrated is False
    assert second.config == original
    assert current == original
    assert second.config is not current


def test_migration_does_not_materialize_absent_defaults():
    result = config_migration.migrate_config(
        {"frigate": {"frigate_url": "http://frigate"}}
    )

    assert "mqtt" not in result.config


def test_falsey_canonical_values_win_and_legacy_fills_missing_leaves():
    source = {
        "mqtt": {
            "host": None,
            "port": 0,
            "topic_prefix": "",
            "authentication": {"enabled": False},
            "tls": {"enabled": False, "insecure": False, "ca_certs": None},
        },
        "frigate": {
            "mqtt_server": "legacy-host",
            "mqtt_port": 1884,
            "main_topic": "legacy-topic",
            "mqtt_auth": True,
            "mqtt_username": "legacy-user",
            "mqtt_password": "legacy-password",
            "mqtt_use_tls": True,
            "mqtt_tls_insecure": True,
            "mqtt_tls_ca_certs": "/legacy/ca.pem",
        },
    }

    mqtt = config_migration.migrate_config(source).config["mqtt"]

    assert mqtt == {
        "host": None,
        "port": 0,
        "topic_prefix": "",
        "authentication": {
            "enabled": False,
            "username": "legacy-user",
            "password": "legacy-password",
        },
        "tls": {"enabled": False, "insecure": False, "ca_certs": None},
    }


def test_mixed_authentication_and_tls_migrate_independently_per_leaf():
    source = {
        "mqtt": {
            "host": "canonical-host",
            "authentication": {"username": "canonical-user"},
            "tls": {"enabled": False},
        },
        "frigate": {
            "mqtt_server": "legacy-host",
            "mqtt_port": 3883,
            "mqtt_auth": True,
            "mqtt_username": "legacy-user",
            "mqtt_password": "legacy-password",
            "mqtt_use_tls": True,
            "mqtt_tls_insecure": True,
            "mqtt_tls_ca_certs": "/legacy/ca.pem",
        },
    }

    mqtt = config_migration.migrate_config(source).config["mqtt"]

    assert mqtt == {
        "host": "canonical-host",
        "port": 3883,
        "authentication": {
            "username": "canonical-user",
            "enabled": True,
            "password": "legacy-password",
        },
        "tls": {
            "enabled": False,
            "insecure": True,
            "ca_certs": "/legacy/ca.pem",
        },
    }


def test_future_version_is_rejected_without_mutating_source():
    source = {
        "config_version": config_migration.CURRENT_CONFIG_VERSION + 1,
        "mqtt": {"authentication": {"password": "SENTINEL-SECRET"}},
    }
    original = deepcopy(source)

    with pytest.raises(
        config_migration.ConfigVersionError,
        match="newer than this WAMF version supports",
    ) as error:
        config_migration.migrate_config(source)

    assert source == original
    assert "SENTINEL-SECRET" not in str(error.value)


@pytest.mark.parametrize(
    "version",
    ["2", 2.0, True, False, -1, 0, None, [], {}],
)
def test_malformed_or_unsupported_present_version_is_not_legacy(version):
    with pytest.raises(config_migration.ConfigVersionError):
        config_migration.migrate_config(
            {
                "config_version": version,
                "frigate": {"mqtt_server": "legacy-host"},
            }
        )


def test_sequential_migration_registry_runs_in_version_order(monkeypatch):
    calls = []
    original_first = config_migration.MIGRATIONS[1]

    def first(config):
        calls.append(1)
        return original_first(config)

    def second(config):
        calls.append(2)
        migrated = deepcopy(config)
        migrated["config_version"] = 3
        return migrated

    monkeypatch.setattr(config_migration, "CURRENT_CONFIG_VERSION", 3)
    monkeypatch.setattr(config_migration, "MIGRATIONS", {1: first, 2: second})

    result = config_migration.migrate_config(
        {"frigate": {"mqtt_server": "legacy-host"}}
    )

    assert calls == [1, 2]
    assert result.target_version == 3
    assert result.config["config_version"] == 3


def test_migrated_ready_and_incomplete_configs_keep_slice_a_states():
    ready = complete_legacy_config()
    ready_result = validate_config(config_migration.migrate_config(ready).config)
    incomplete_result = validate_config(config_migration.migrate_config({}).config)

    assert ready_result.is_ready is True
    assert incomplete_result.is_valid is True
    assert incomplete_result.is_ready is False


def test_structurally_invalid_migration_error_does_not_expose_secret():
    source = complete_legacy_config()
    source["frigate"]["mqtt_port"] = "SENTINEL-SECRET"
    migration = config_migration.migrate_config(source)
    validation = validate_config(migration.config)

    with pytest.raises(config_migration.ConfigMigrationValidationError) as error:
        config_migration.require_structurally_valid_migration(
            migration,
            validation,
        )

    assert "mqtt.port" in str(error.value)
    assert "SENTINEL-SECRET" not in str(error.value)


@pytest.mark.parametrize(
    "source",
    [
        pytest.param(complete_legacy_config(), id="legacy-only"),
        pytest.param(
            {
                **complete_legacy_config(),
                "mqtt": {
                    "host": "canonical-host",
                    "authentication": {"username": "canonical-user"},
                },
            },
            id="mixed",
        ),
        pytest.param(
            {
                **complete_legacy_config(),
                "mqtt": {
                    "host": "canonical-host",
                    "port": 2883,
                    "topic_prefix": "canonical-topic",
                    "authentication": {"enabled": False},
                    "tls": {"enabled": False},
                },
            },
            id="conflicting",
        ),
        pytest.param(
            {
                "mqtt": {
                    "host": None,
                    "port": 0,
                    "topic_prefix": "",
                    "authentication": {"enabled": False},
                    "tls": {"enabled": False, "ca_certs": None},
                },
                "frigate": {
                    "mqtt_server": "legacy-host",
                    "mqtt_port": 1884,
                    "main_topic": "legacy-topic",
                    "mqtt_auth": True,
                    "mqtt_use_tls": True,
                    "mqtt_tls_ca_certs": "/legacy/ca.pem",
                },
            },
            id="falsey-canonical",
        ),
        pytest.param({}, id="absent-defaults"),
    ],
)
def test_migration_preserves_actual_runtime_mqtt_settings(source):
    before = runtime_settings(source)
    migrated = config_migration.migrate_config(source).config
    after = runtime_settings(migrated)

    assert after == before


def test_shipped_wamf_examples_are_current_canonical_and_have_no_legacy_aliases():
    root = Path(__file__).resolve().parent.parent

    for relative_path in (
        "config/config.yml.example",
        "config/config.docker.yml.example",
    ):
        config = yaml.safe_load((root / relative_path).read_text())
        assert config["config_version"] == config_migration.CURRENT_CONFIG_VERSION
        assert isinstance(config["mqtt"], dict)
        assert not LEGACY_KEYS.intersection(config["frigate"])
