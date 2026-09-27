"""Small, versioned transformations for persisted WAMF configuration."""

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass


LEGACY_CONFIG_VERSION = 1
CANONICAL_MQTT_CONFIG_VERSION = 2
CURRENT_CONFIG_VERSION = CANONICAL_MQTT_CONFIG_VERSION


class ConfigMigrationError(Exception):
    """Base class for controlled configuration migration failures."""


class ConfigVersionError(ConfigMigrationError):
    """An explicit persisted configuration version is not supported."""


class ConfigMigrationValidationError(ConfigMigrationError):
    """A migrated configuration failed structural validation."""


@dataclass(frozen=True)
class ConfigMigrationResult:
    config: dict
    source_version: int
    target_version: int
    migrated: bool


LEGACY_MQTT_LEAVES = (
    ("mqtt_server", ("host",)),
    ("mqtt_port", ("port",)),
    ("main_topic", ("topic_prefix",)),
    ("mqtt_auth", ("authentication", "enabled")),
    ("mqtt_username", ("authentication", "username")),
    ("mqtt_password", ("authentication", "password")),
    ("mqtt_use_tls", ("tls", "enabled")),
    ("mqtt_tls_insecure", ("tls", "insecure")),
    ("mqtt_tls_ca_certs", ("tls", "ca_certs")),
)


def get_config_version(config):
    """Return the explicit version, or the defined legacy generation."""

    if not isinstance(config, Mapping):
        raise ConfigVersionError("Configuration must be a YAML mapping.")
    if "config_version" not in config:
        return LEGACY_CONFIG_VERSION

    version = config["config_version"]
    if isinstance(version, bool) or not isinstance(version, int):
        raise ConfigVersionError("config_version must be a supported integer.")
    if version > CURRENT_CONFIG_VERSION:
        raise ConfigVersionError(
            f"Configuration version {version} is newer than this WAMF version supports."
        )
    if version < LEGACY_CONFIG_VERSION:
        raise ConfigVersionError(
            f"Configuration version {version} is not supported by this WAMF version."
        )
    return version


def _leaf_is_present(mqtt, path):
    current = mqtt
    for key in path[:-1]:
        if not isinstance(current, Mapping) or key not in current:
            return False
        current = current[key]
    return isinstance(current, Mapping) and path[-1] in current


def _set_missing_leaf(mqtt, path, value):
    current = mqtt
    for key in path[:-1]:
        child = current.get(key)
        if child is None:
            child = {}
            current[key] = child
        elif not isinstance(child, Mapping):
            return
        current = child
    current[path[-1]] = deepcopy(value)


def _migrate_v1_to_v2(config):
    migrated = deepcopy(dict(config))
    frigate = migrated.get("frigate")
    if isinstance(frigate, Mapping):
        legacy_values = {
            legacy_key: frigate[legacy_key]
            for legacy_key, _ in LEGACY_MQTT_LEAVES
            if legacy_key in frigate
        }
        mqtt = migrated.get("mqtt")
        if legacy_values and mqtt is None:
            mqtt = {}
            migrated["mqtt"] = mqtt

        if isinstance(mqtt, Mapping):
            for legacy_key, canonical_path in LEGACY_MQTT_LEAVES:
                if (
                    legacy_key in legacy_values
                    and not _leaf_is_present(mqtt, canonical_path)
                ):
                    _set_missing_leaf(
                        mqtt,
                        canonical_path,
                        legacy_values[legacy_key],
                    )

        for legacy_key, _ in LEGACY_MQTT_LEAVES:
            frigate.pop(legacy_key, None)

    migrated.pop("config_version", None)
    return {"config_version": CANONICAL_MQTT_CONFIG_VERSION, **migrated}


MIGRATIONS = {
    LEGACY_CONFIG_VERSION: _migrate_v1_to_v2,
}


def migrate_config(config):
    """Return an independent current-version mapping after sequential migration."""

    source_version = get_config_version(config)
    migrated = deepcopy(dict(config))
    version = source_version
    changed = False

    while version < CURRENT_CONFIG_VERSION:
        migration = MIGRATIONS.get(version)
        if migration is None:
            raise ConfigVersionError(
                f"Configuration version {version} is not supported by this WAMF version."
            )
        migrated = migration(migrated)
        version += 1
        changed = True
        if migrated.get("config_version") != version:
            raise ConfigMigrationError(
                "Configuration migration did not produce the expected version."
            )

    return ConfigMigrationResult(
        config=migrated,
        source_version=source_version,
        target_version=version,
        migrated=changed,
    )


def require_structurally_valid_migration(migration, validation):
    """Reject a migrated result using Slice A field-only diagnostics."""

    if migration.migrated and validation.errors:
        fields = ", ".join(issue.field for issue in validation.errors)
        raise ConfigMigrationValidationError(
            f"Migrated configuration is structurally invalid: {fields}."
        )
