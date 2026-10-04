from copy import deepcopy

import pytest

from app.config_migration import migrate_config
from app.config_validation import validate_config


def ready_config():
    return {
        "frigate": {
            "frigate_url": "http://frigate:5000",
            "camera": ["birdcam"],
        },
        "mqtt": {
            "host": "mqtt.local",
            "port": 1883,
            "topic_prefix": "frigate",
            "authentication": {"enabled": False},
            "tls": {
                "enabled": True,
                "insecure": False,
                "ca_certs": "/certs/ca.pem",
            },
        },
        "classification": {"model": "model.tflite", "threshold": 0.7},
        "webui": {"host": "0.0.0.0", "port": 7767},
    }


def issue_fields(issues):
    return [issue.field for issue in issues]


def test_valid_ready_configuration_has_independent_outcomes():
    result = validate_config(ready_config())

    assert result.is_valid is True
    assert result.is_ready is True
    assert result.errors == ()
    assert result.readiness_issues == ()


def test_valid_incomplete_configuration_is_not_invalid():
    result = validate_config({})

    assert result.is_valid is True
    assert result.is_ready is False
    assert result.errors == ()
    assert set(issue_fields(result.readiness_issues)) >= {
        "frigate.frigate_url",
        "frigate.camera",
        "mqtt.host",
        "mqtt.topic_prefix",
        "classification.model",
        "classification.threshold",
    }


def test_invalid_values_return_multiple_structural_errors():
    config = ready_config()
    config["webui"]["port"] = "7767"
    config["mqtt"]["port"] = 0
    config["classification"]["threshold"] = 2

    result = validate_config(config)

    assert result.is_valid is False
    assert result.is_ready is False
    assert issue_fields(result.errors) == [
        "webui.port",
        "classification.threshold",
        "mqtt.port",
    ]


def test_migrated_legacy_config_is_validated_as_canonical():
    config = ready_config()
    config.pop("mqtt")
    config["frigate"].update(mqtt_server="mqtt", main_topic="frigate")

    migration = migrate_config(config)
    result = validate_config(migration.config)

    assert migration.migrated is True
    assert result.is_valid is True
    assert result.is_ready is True
    assert result.warnings == ()


def test_validation_does_not_mutate_input():
    config = ready_config()
    original = deepcopy(config)

    validate_config(config)

    assert config == original


def test_validation_does_not_interpret_conflicting_legacy_mqtt_values():
    config = ready_config()
    config["frigate"].update(
        mqtt_server="",
        mqtt_port=0,
        main_topic="",
        mqtt_auth=True,
        mqtt_username="",
        mqtt_password="",
    )

    result = validate_config(config)

    assert result.is_valid is True
    assert result.is_ready is True
    assert result.warnings == ()


def test_representative_legacy_mqtt_configuration_is_migrated_before_validation():
    config = ready_config()
    config.pop("mqtt")
    config["frigate"].update(
        mqtt_server="mqtt.local",
        mqtt_port=8883,
        main_topic="frigate",
        mqtt_auth=True,
        mqtt_username="bird-user",
        mqtt_password="secret",
        mqtt_use_tls=True,
        mqtt_tls_insecure=False,
        mqtt_tls_ca_certs="/certs/ca.pem",
    )

    migration = migrate_config(config)
    result = validate_config(migration.config)

    assert migration.migrated is True
    assert result.is_valid is True
    assert result.is_ready is True


@pytest.mark.parametrize("port", [True, 0, 65536, "1883"])
def test_invalid_mqtt_ports_are_structural_errors(port):
    config = ready_config()
    config["mqtt"]["port"] = port

    result = validate_config(config)

    assert "mqtt.port" in issue_fields(result.errors)


@pytest.mark.parametrize("threshold", [0, 1, 0.5])
def test_classification_threshold_boundaries_are_valid(threshold):
    config = ready_config()
    config["classification"]["threshold"] = threshold

    assert validate_config(config).is_ready is True


@pytest.mark.parametrize("threshold", [True, -0.01, 1.01, "0.7"])
def test_invalid_classification_threshold_is_structural(threshold):
    config = ready_config()
    config["classification"]["threshold"] = threshold

    result = validate_config(config)

    assert "classification.threshold" in issue_fields(result.errors)


def test_missing_and_placeholder_frigate_settings_are_readiness_findings():
    config = ready_config()
    config["frigate"] = {
        "frigate_url": "<frigate-url>",
        "camera": ["your-camera-name"],
    }

    result = validate_config(config)

    assert result.is_valid is True
    assert set(issue_fields(result.readiness_issues)) == {
        "frigate.frigate_url",
        "frigate.camera",
    }


def test_secret_values_never_appear_in_diagnostics():
    secret = "SENTINEL-MQTT-PASSWORD"
    config = ready_config()
    config["mqtt"]["authentication"] = {
        "enabled": True,
        "username": "",
        "password": secret,
    }
    config["mqtt"]["port"] = "invalid"

    result = validate_config(config)
    diagnostics = repr(result)

    assert secret not in diagnostics
    assert "mqtt.authentication.username" in diagnostics
    assert "mqtt.port" in diagnostics


@pytest.mark.parametrize(
    ("retention", "expected_field"),
    [
        ({"snapshots_days": -1}, "retention.snapshots_days"),
        ({"clips_days": True}, "retention.clips_days"),
        (
            {"species_overrides": {"Turdus merula": {"clips_days": None}}},
            "retention.species_overrides['Turdus merula'].clips_days",
        ),
        (
            {"species_overrides": {"Turdus merula": None}},
            "retention.species_overrides['Turdus merula']",
        ),
    ],
)
def test_validation_reuses_canonical_retention_policy_rules(
    retention, expected_field
):
    config = ready_config()
    config["retention"] = retention

    result = validate_config(config)

    assert expected_field in issue_fields(result.errors)


def test_validation_rejects_canonical_normalized_species_duplicates():
    config = ready_config()
    config["retention"] = {
        "species_overrides": {
            "Turdus merula": {"snapshots_days": 10},
            " turdus MERULA ": {"clips_days": 20},
        }
    }

    result = validate_config(config)

    assert issue_fields(result.errors) == ["retention.species_overrides"]
    assert "duplicate scientific name" in result.errors[0].message
