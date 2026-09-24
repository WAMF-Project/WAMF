"""Canonical, non-mutating views of configuration compatibility aliases."""

from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass


@dataclass(frozen=True)
class NormalizedConfig:
    """A canonical view alongside an independent copy of its raw source."""

    raw: dict
    config: dict
    provenance: dict
    source_paths: dict
    warnings: tuple


def _mapping_section(parent, key, path):
    if key not in parent:
        return {}
    section = parent[key]
    if not isinstance(section, Mapping):
        # Match bootstrap's malformed-YAML exception contract.
        raise ValueError(f"{path} must be a YAML mapping")  # noqa: TRY004
    return section


def normalize_config(
    source,
    warning_callback: Callable[[str], None] | None = None,
):
    """Return the Kingfisher canonical view without changing ``source``.

    This API intentionally performs compatibility resolution rather than broad
    semantic validation. Existing bootstrap validation remains authoritative.
    Warnings contain key paths only, so secret values cannot be disclosed.
    """
    if not isinstance(source, Mapping):
        # Match bootstrap's malformed-YAML exception contract.
        raise ValueError("Configuration must be a YAML mapping")  # noqa: TRY004

    raw = deepcopy(dict(source))
    mqtt = _mapping_section(raw, "mqtt", "mqtt")
    authentication = _mapping_section(mqtt, "authentication", "mqtt.authentication")
    tls = _mapping_section(mqtt, "tls", "mqtt.tls")
    frigate = _mapping_section(raw, "frigate", "frigate")
    perch = _mapping_section(raw, "perch", "perch")
    bridge = _mapping_section(raw, "bridge", "bridge")
    health = _mapping_section(raw, "health", "health")
    camera = _mapping_section(raw, "camera", "camera")
    live_view = _mapping_section(raw, "live_view", "live_view")

    warning_messages = []
    warning_set = set()
    provenance = {}
    source_paths = {}

    def record_warning(message):
        if message not in warning_set:
            warning_set.add(message)
            warning_messages.append(message)

    def resolve(canonical_path, candidates, default):
        present = [
            (kind, path, container[key])
            for kind, path, container, key in candidates
            if key in container
        ]
        if present:
            selected_kind, selected_path, selected_value = present[0]
            provenance[canonical_path] = selected_kind
            source_paths[canonical_path] = selected_path
            if selected_kind == "legacy":
                record_warning(
                    f"Legacy config key {selected_path} is deprecated; "
                    f"use {canonical_path}."
                )
            for _, other_path, other_value in present[1:]:
                if other_value != selected_value:
                    record_warning(
                        f"Both {selected_path} and {other_path} are configured; "
                        f"using {selected_path}."
                    )
                else:
                    record_warning(
                        f"Legacy config key {other_path} is deprecated; "
                        f"use {canonical_path}."
                    )
            return deepcopy(selected_value)

        provenance[canonical_path] = "default"
        source_paths[canonical_path] = None
        return deepcopy(default)

    def standard(
        canonical_path,
        canonical_section,
        canonical_key,
        legacy_path,
        legacy_section,
        legacy_key,
        default,
    ):
        return resolve(
            canonical_path,
            (
                ("canonical", canonical_path, canonical_section, canonical_key),
                ("legacy", legacy_path, legacy_section, legacy_key),
            ),
            default,
        )

    canonical = {
        "mqtt": {
            "host": standard(
                "mqtt.host",
                mqtt,
                "host",
                "frigate.mqtt_server",
                frigate,
                "mqtt_server",
                None,
            ),
            "port": standard(
                "mqtt.port",
                mqtt,
                "port",
                "frigate.mqtt_port",
                frigate,
                "mqtt_port",
                1883,
            ),
            "topic_prefix": standard(
                "mqtt.topic_prefix",
                mqtt,
                "topic_prefix",
                "frigate.main_topic",
                frigate,
                "main_topic",
                None,
            ),
            "authentication": {
                "enabled": standard(
                    "mqtt.authentication.enabled",
                    authentication,
                    "enabled",
                    "frigate.mqtt_auth",
                    frigate,
                    "mqtt_auth",
                    False,
                ),
                "username": standard(
                    "mqtt.authentication.username",
                    authentication,
                    "username",
                    "frigate.mqtt_username",
                    frigate,
                    "mqtt_username",
                    None,
                ),
                "password": standard(
                    "mqtt.authentication.password",
                    authentication,
                    "password",
                    "frigate.mqtt_password",
                    frigate,
                    "mqtt_password",
                    None,
                ),
            },
            "tls": {
                "enabled": standard(
                    "mqtt.tls.enabled",
                    tls,
                    "enabled",
                    "frigate.mqtt_use_tls",
                    frigate,
                    "mqtt_use_tls",
                    False,
                ),
                "insecure": standard(
                    "mqtt.tls.insecure",
                    tls,
                    "insecure",
                    "frigate.mqtt_tls_insecure",
                    frigate,
                    "mqtt_tls_insecure",
                    False,
                ),
                "ca_certs": standard(
                    "mqtt.tls.ca_certs",
                    tls,
                    "ca_certs",
                    "frigate.mqtt_tls_ca_certs",
                    frigate,
                    "mqtt_tls_ca_certs",
                    None,
                ),
            },
        },
        "perch": {
            "enabled": standard(
                "perch.enabled",
                perch,
                "enabled",
                "bridge.enabled",
                bridge,
                "enabled",
                False,
            ),
            "events_url": standard(
                "perch.events_url",
                perch,
                "events_url",
                "bridge.events_url",
                bridge,
                "events_url",
                None,
            ),
            "timeout_seconds": standard(
                "perch.timeout_seconds",
                perch,
                "timeout_seconds",
                "bridge.timeout_seconds",
                bridge,
                "timeout_seconds",
                1,
            ),
        },
        "health": {
            "check_interval_seconds": resolve(
                "health.check_interval_seconds",
                (
                    (
                        "canonical",
                        "health.check_interval_seconds",
                        health,
                        "check_interval_seconds",
                    ),
                    (
                        "legacy",
                        "perch.health_check_interval_seconds",
                        perch,
                        "health_check_interval_seconds",
                    ),
                    (
                        "legacy",
                        "bridge.health_check_interval_seconds",
                        bridge,
                        "health_check_interval_seconds",
                    ),
                ),
                60,
            ),
        },
        "camera": {
            "live_view_url": standard(
                "camera.live_view_url",
                camera,
                "live_view_url",
                "live_view.url",
                live_view,
                "url",
                "",
            ),
        },
    }

    if warning_callback is not None:
        for message in warning_messages:
            warning_callback(message)

    return NormalizedConfig(
        raw=raw,
        config=canonical,
        provenance=provenance,
        source_paths=source_paths,
        warnings=tuple(warning_messages),
    )
