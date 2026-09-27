from copy import deepcopy

import pytest
import yaml

from app.config_loader import load_runtime_config
from app.config_secrets import (
    CURRENT_SECRETS_VERSION,
    SecretsValidationError,
    apply_secrets,
    empty_secrets,
    load_secrets,
    merge_embedded_secrets,
    sanitize_config,
    secret_updates,
    validate_secrets,
)
from app.mqtt_settings import mqtt_settings_from_config
from app.config_normalization import normalize_config


def complete_secrets(**mqtt):
    return {
        "secrets_version": CURRENT_SECRETS_VERSION,
        "mqtt": mqtt or {"username": "bird", "password": "seed"},
        "admin": {"password_hash": "hash", "session_secret": "session"},
        "api": {"token_hash": "token-hash"},
    }


def test_load_secrets_missing_and_valid_file(tmp_path):
    path = tmp_path / "secrets.yml"
    assert load_secrets(path) == {"secrets_version": 1}
    path.write_text(yaml.safe_dump(complete_secrets()), encoding="utf-8")
    assert load_secrets(path) == complete_secrets()


@pytest.mark.parametrize(
    "document, message",
    [
        ({}, "secrets_version"),
        ({"secrets_version": True}, "secrets_version"),
        ({"secrets_version": 2}, "not supported"),
        ({"secrets_version": 1, "mqtt": []}, "mqtt must"),
        ({"secrets_version": 1, "mqtt": {"password": 7}}, "must be a string"),
        ({"secrets_version": 1, "mqtt": {"host": "forbidden"}}, "mqtt.host"),
        ({"secrets_version": 1, "unknown": {}}, "unknown"),
    ],
)
def test_validate_secrets_rejects_bad_versions_structures_types_and_keys(
    document, message
):
    with pytest.raises(SecretsValidationError, match=message):
        validate_secrets(document)


def test_load_secrets_wraps_malformed_yaml_without_value_disclosure(tmp_path):
    sentinel = "SECRET-SENTINEL-LOAD"
    path = tmp_path / "secrets.yml"
    path.write_text(f"secrets_version: 1\nmqtt: [{sentinel}\n", encoding="utf-8")
    with pytest.raises(SecretsValidationError) as error:
        load_secrets(path)
    assert str(error.value) == "Secrets could not be loaded."
    assert sentinel not in str(error.value)


def test_apply_secrets_is_strict_non_mutating_and_returns_independent_data():
    config = {
        "mqtt": {
            "host": "config-broker",
            "authentication": {"enabled": True},
        },
        "admin": {"auth_enabled": True},
        "api": {"token_auth_enabled": True},
    }
    secrets = complete_secrets(username="", password="")
    original_config = deepcopy(config)
    original_secrets = deepcopy(secrets)

    runtime = apply_secrets(config, secrets)

    assert runtime["mqtt"]["host"] == "config-broker"
    assert runtime["mqtt"]["authentication"]["username"] == ""
    assert runtime["mqtt"]["authentication"]["password"] == ""
    assert runtime["admin"]["password_hash"] == "hash"
    assert runtime["api"]["token_hash"] == "token-hash"
    assert config == original_config
    assert secrets == original_secrets
    runtime["mqtt"]["authentication"]["username"] = "changed"
    assert config == original_config
    assert secrets == original_secrets


def test_ordinary_config_cannot_be_overridden_through_secrets():
    config = {"mqtt": {"host": "real", "authentication": {"enabled": False}}}
    with pytest.raises(SecretsValidationError, match="mqtt.host"):
        apply_secrets(
            config,
            {"secrets_version": 1, "mqtt": {"host": "hostile"}},
        )


def test_existing_falsey_secret_wins_over_embedded_value_by_presence():
    config = {
        "mqtt": {
            "authentication": {
                "enabled": True,
                "username": "embedded-user",
                "password": "embedded-password",
            }
        }
    }
    existing = {
        "secrets_version": 1,
        "mqtt": {"username": "", "password": ""},
    }
    result = merge_embedded_secrets(config, existing)
    assert result.secrets["mqtt"] == {"username": "", "password": ""}
    assert result.config == {"mqtt": {"authentication": {"enabled": True}}}


def test_sanitize_config_removes_only_supported_secret_paths():
    source = {
        "mqtt": {
            "host": "broker",
            "authentication": {
                "enabled": True,
                "username": "user",
                "password": "password",
            },
        },
        "admin": {
            "auth_enabled": True,
            "password_hash": "hash",
            "session_secret": "session",
        },
        "api": {"token_auth_enabled": True, "token_hash": "token"},
        "bridge": {"events_url": "http://example.invalid/events"},
    }
    original = deepcopy(source)
    sanitized = sanitize_config(source)
    assert sanitized["mqtt"] == {
        "host": "broker",
        "authentication": {"enabled": True},
    }
    assert sanitized["admin"] == {"auth_enabled": True}
    assert sanitized["api"] == {"token_auth_enabled": True}
    assert sanitized["bridge"] == original["bridge"]
    assert source == original


def test_admin_blank_or_omitted_secret_update_preserves_and_new_value_replaces():
    existing = {"secrets_version": 1, "mqtt": {"username": "old", "password": "old-pass"}}
    blank = {
        "mqtt": {"authentication": {"username": "", "password": ""}}
    }
    assert secret_updates(blank, existing) == existing
    assert secret_updates({}, existing) == existing
    replaced = secret_updates(
        {"mqtt": {"authentication": {"username": "new", "password": "new-pass"}}},
        existing,
    )
    assert replaced["mqtt"] == {"username": "new", "password": "new-pass"}


def test_runtime_loader_applies_mqtt_admin_and_api_secrets(tmp_path):
    config_path = tmp_path / "config.yml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "config_version": 2,
                "mqtt": {
                    "host": "broker",
                    "authentication": {"enabled": True},
                },
                "admin": {"auth_enabled": True},
                "api": {"token_auth_enabled": True},
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "secrets.yml").write_text(
        yaml.safe_dump(complete_secrets(username="mqtt-user", password="mqtt-pass")),
        encoding="utf-8",
    )
    runtime = load_runtime_config(config_path)
    settings = mqtt_settings_from_config(normalize_config(runtime).config)
    assert settings.username == "mqtt-user"
    assert settings.password == "mqtt-pass"
    assert runtime["admin"]["session_secret"] == "session"
    assert runtime["admin"]["password_hash"] == "hash"
    assert runtime["api"]["token_hash"] == "token-hash"


def test_schema_versions_remain_independent():
    assert CURRENT_SECRETS_VERSION == 1
    assert empty_secrets() == {"secrets_version": 1}
