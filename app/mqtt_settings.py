"""Runtime settings derived from canonical MQTT configuration."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class MqttSettings:
    """The MQTT connection settings used by WAMF clients."""

    host: str | None
    port: int = 1883
    topic_prefix: str = "frigate"
    authentication_enabled: bool = False
    username: str | None = None
    password: str | None = field(default=None, repr=False)
    tls_enabled: bool = False
    tls_insecure: bool = False
    ca_certs: str | None = None


def mqtt_settings_from_config(config: Mapping[str, Any]) -> MqttSettings:
    """Build runtime settings from an already-normalized configuration."""

    mqtt = config["mqtt"]
    authentication = mqtt.get("authentication", {})
    tls = mqtt.get("tls", {})

    topic_prefix = mqtt.get("topic_prefix")
    if topic_prefix is None:
        topic_prefix = "frigate"

    return MqttSettings(
        host=mqtt.get("host"),
        port=mqtt.get("port", 1883),
        topic_prefix=topic_prefix,
        authentication_enabled=authentication.get("enabled", False),
        username=authentication.get("username"),
        password=authentication.get("password"),
        tls_enabled=tls.get("enabled", False),
        tls_insecure=tls.get("insecure", False),
        ca_certs=tls.get("ca_certs"),
    )
