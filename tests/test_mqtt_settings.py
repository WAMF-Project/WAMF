from copy import deepcopy

import pytest

from app.config_normalization import normalize_config
from app.mqtt_settings import mqtt_settings_from_config


def _settings(source):
    return mqtt_settings_from_config(normalize_config(source).config)


def test_plain_lan_mqtt_uses_runtime_defaults():
    settings = _settings({"mqtt": {"host": "mqtt.lan"}})

    assert settings.host == "mqtt.lan"
    assert settings.port == 1883
    assert settings.topic_prefix == "frigate"
    assert settings.authentication_enabled is False
    assert settings.tls_enabled is False


def test_explicit_connection_settings_are_represented():
    settings = _settings(
        {
            "mqtt": {
                "host": "broker.example",
                "port": 8883,
                "topic_prefix": "kingfisher",
            }
        }
    )

    assert settings.host == "broker.example"
    assert settings.port == 8883
    assert settings.topic_prefix == "kingfisher"


def test_authentication_settings_are_represented():
    settings = _settings(
        {
            "mqtt": {
                "authentication": {
                    "enabled": True,
                    "username": "bird-user",
                    "password": "secret-password",
                }
            }
        }
    )

    assert settings.authentication_enabled is True
    assert settings.username == "bird-user"
    assert settings.password == "secret-password"


@pytest.mark.parametrize("insecure", [True, False])
def test_tls_settings_preserve_insecure_boolean(insecure):
    settings = _settings(
        {"mqtt": {"tls": {"enabled": True, "insecure": insecure}}}
    )

    assert settings.tls_enabled is True
    assert settings.tls_insecure is insecure


def test_ca_certificate_setting_is_represented():
    settings = _settings(
        {"mqtt": {"tls": {"enabled": True, "ca_certs": "/certs/ca.pem"}}}
    )

    assert settings.ca_certs == "/certs/ca.pem"


def test_explicit_false_and_empty_values_are_preserved():
    settings = _settings(
        {
            "mqtt": {
                "topic_prefix": "",
                "authentication": {"enabled": False, "username": ""},
                "tls": {"enabled": False, "insecure": False, "ca_certs": ""},
            }
        }
    )

    assert settings.topic_prefix == ""
    assert settings.authentication_enabled is False
    assert settings.username == ""
    assert settings.tls_enabled is False
    assert settings.tls_insecure is False
    assert settings.ca_certs == ""


def test_password_is_excluded_from_string_representations():
    password = "SENTINEL-MQTT-PASSWORD"
    settings = _settings(
        {"mqtt": {"authentication": {"enabled": True, "password": password}}}
    )

    assert password not in repr(settings)
    assert password not in str(settings)
    assert "password" not in repr(settings)


def test_building_settings_does_not_mutate_input():
    config = normalize_config(
        {
            "mqtt": {
                "host": "mqtt.lan",
                "authentication": {"enabled": False},
                "tls": {"enabled": False},
            }
        }
    ).config
    original = deepcopy(config)

    mqtt_settings_from_config(config)

    assert config == original
