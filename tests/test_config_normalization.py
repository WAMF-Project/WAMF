import re
from copy import deepcopy

import pytest

from app.config_normalization import normalize_config


def test_complete_legacy_config_is_normalized_without_mutating_source():
    source = {
        "frigate": {
            "mqtt_server": "legacy-broker",
            "mqtt_port": 1884,
            "main_topic": "legacy-frigate",
            "mqtt_auth": True,
            "mqtt_username": "legacy-user",
            "mqtt_password": "legacy-password",
            "mqtt_use_tls": True,
            "mqtt_tls_insecure": True,
            "mqtt_tls_ca_certs": "/legacy/ca.pem",
            "object": "bird",
        },
        "bridge": {
            "enabled": True,
            "events_url": "https://bridge.example/events",
            "timeout_seconds": 3,
            "health_check_interval_seconds": 45,
        },
        "live_view": {"url": "https://camera.example/live"},
        "unrelated": {"keep": "me"},
    }
    original = deepcopy(source)

    normalized = normalize_config(source)

    assert normalized.config == {
        "mqtt": {
            "host": "legacy-broker",
            "port": 1884,
            "topic_prefix": "legacy-frigate",
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
        },
        "perch": {
            "enabled": True,
            "events_url": "https://bridge.example/events",
            "timeout_seconds": 3,
        },
        "health": {"check_interval_seconds": 45},
        "camera": {"live_view_url": "https://camera.example/live"},
    }
    assert normalized.provenance["mqtt.host"] == "legacy"
    assert normalized.source_paths["mqtt.host"] == "frigate.mqtt_server"
    assert normalized.raw["frigate"]["object"] == "bird"
    assert normalized.raw["unrelated"] == {"keep": "me"}
    assert source == original


def test_complete_canonical_config_is_used():
    source = {
        "mqtt": {
            "host": "broker",
            "port": 8883,
            "topic_prefix": "frigate",
            "authentication": {
                "enabled": True,
                "username": "user",
                "password": "password",
            },
            "tls": {"enabled": True, "insecure": False, "ca_certs": "/ca.pem"},
        },
        "perch": {
            "enabled": True,
            "events_url": "https://perch.example/events",
            "timeout_seconds": 2,
        },
        "health": {"check_interval_seconds": 30},
        "camera": {"live_view_url": "https://camera.example/live"},
    }

    normalized = normalize_config(source)

    assert normalized.config == source
    assert set(normalized.provenance.values()) == {"canonical"}
    assert normalized.warnings == ()


def test_mixed_config_falls_back_per_key():
    normalized = normalize_config(
        {
            "mqtt": {"host": "canonical-broker", "tls": {"enabled": True}},
            "frigate": {
                "mqtt_port": 1884,
                "main_topic": "legacy-topic",
                "mqtt_tls_insecure": True,
            },
            "perch": {"enabled": True},
            "bridge": {"events_url": "https://legacy.example/events"},
        }
    )

    assert normalized.config["mqtt"]["host"] == "canonical-broker"
    assert normalized.config["mqtt"]["port"] == 1884
    assert normalized.config["mqtt"]["topic_prefix"] == "legacy-topic"
    assert normalized.config["mqtt"]["tls"] == {
        "enabled": True,
        "insecure": True,
        "ca_certs": None,
    }
    assert normalized.config["perch"]["enabled"] is True
    assert normalized.config["perch"]["events_url"] == "https://legacy.example/events"
    assert normalized.provenance["mqtt.tls.ca_certs"] == "default"


def test_conflict_uses_canonical_and_reports_only_key_paths():
    normalized = normalize_config(
        {
            "mqtt": {"port": 1883},
            "frigate": {"mqtt_port": 1884},
        }
    )

    assert normalized.config["mqtt"]["port"] == 1883
    assert normalized.provenance["mqtt.port"] == "canonical"
    assert normalized.warnings == (
        "Both mqtt.port and frigate.mqtt_port are configured; using mqtt.port.",
    )


def test_falsey_canonical_values_do_not_fall_back():
    normalized = normalize_config(
        {
            "mqtt": {
                "port": 0,
                "authentication": {"enabled": False, "username": ""},
                "tls": {"enabled": False, "ca_certs": ""},
            },
            "frigate": {
                "mqtt_port": 1884,
                "mqtt_auth": True,
                "mqtt_username": "legacy-user",
                "mqtt_use_tls": True,
                "mqtt_tls_ca_certs": "/legacy/ca.pem",
            },
        }
    )

    assert normalized.config["mqtt"]["port"] == 0
    assert normalized.config["mqtt"]["authentication"]["enabled"] is False
    assert normalized.config["mqtt"]["authentication"]["username"] == ""
    assert normalized.config["mqtt"]["tls"]["enabled"] is False
    assert normalized.config["mqtt"]["tls"]["ca_certs"] == ""


@pytest.mark.parametrize(
    "source, expected, provenance, source_path",
    [
        (
            {
                "health": {"check_interval_seconds": 10},
                "perch": {"health_check_interval_seconds": 20},
                "bridge": {"health_check_interval_seconds": 30},
            },
            10,
            "canonical",
            "health.check_interval_seconds",
        ),
        (
            {
                "perch": {"health_check_interval_seconds": 20},
                "bridge": {"health_check_interval_seconds": 30},
            },
            20,
            "legacy",
            "perch.health_check_interval_seconds",
        ),
        (
            {"bridge": {"health_check_interval_seconds": 30}},
            30,
            "legacy",
            "bridge.health_check_interval_seconds",
        ),
        ({}, 60, "default", None),
    ],
)
def test_health_interval_precedence(source, expected, provenance, source_path):
    normalized = normalize_config(source)
    assert normalized.config["health"]["check_interval_seconds"] == expected
    assert normalized.provenance["health.check_interval_seconds"] == provenance
    assert normalized.source_paths["health.check_interval_seconds"] == source_path


@pytest.mark.parametrize(
    "source, expected, provenance",
    [
        (
            {"camera": {"live_view_url": "canonical"}, "live_view": {"url": "legacy"}},
            "canonical",
            "canonical",
        ),
        ({"live_view": {"url": "legacy"}}, "legacy", "legacy"),
        ({}, "", "default"),
    ],
)
def test_live_view_precedence(source, expected, provenance):
    normalized = normalize_config(source)
    assert normalized.config["camera"]["live_view_url"] == expected
    assert normalized.provenance["camera.live_view_url"] == provenance


@pytest.mark.parametrize(
    "source, path",
    [
        ({"mqtt": "invalid"}, "mqtt"),
        ({"mqtt": {"authentication": "invalid"}}, "mqtt.authentication"),
        ({"mqtt": {"tls": "invalid"}}, "mqtt.tls"),
        ({"perch": "invalid"}, "perch"),
        ({"health": "invalid"}, "health"),
    ],
)
def test_malformed_sections_are_rejected(source, path):
    with pytest.raises(
        ValueError, match=rf"^{re.escape(path)} must be a YAML mapping$"
    ):
        normalize_config(source)


def test_password_conflict_warning_redacts_both_values_and_callback_is_deduplicated():
    canonical_secret = "CANONICAL-SENTINEL-SECRET"
    legacy_secret = "LEGACY-SENTINEL-SECRET"
    emitted = []

    normalized = normalize_config(
        {
            "mqtt": {"authentication": {"password": canonical_secret}},
            "frigate": {"mqtt_password": legacy_secret},
        },
        warning_callback=emitted.append,
    )

    assert normalized.config["mqtt"]["authentication"]["password"] == canonical_secret
    assert emitted == list(normalized.warnings)
    assert len(emitted) == len(set(emitted)) == 1
    assert "mqtt.authentication.password" in emitted[0]
    assert "frigate.mqtt_password" in emitted[0]
    assert canonical_secret not in emitted[0]
    assert legacy_secret not in emitted[0]


def test_result_owns_independent_copies_of_source_and_selected_values():
    source = {"mqtt": {"host": "broker"}, "unknown": {"items": [1]}}
    normalized = normalize_config(source)

    source["unknown"]["items"].append(2)
    normalized.raw["unknown"]["items"].append(3)

    assert normalized.raw["unknown"]["items"] == [1, 3]
    assert source["unknown"]["items"] == [1, 2]
