"""Structured Administration Settings form mapping for canonical config v2."""

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass

import yaml

from app.config_migration import CURRENT_CONFIG_VERSION, migrate_config
from app.config_normalization import normalize_config
from app.config_secrets import sanitize_config


@dataclass(frozen=True)
class SettingsField:
    name: str
    path: tuple[str, ...]
    kind: str = "string"
    default: object = ""
    optional: bool = False
    choices: tuple[str, ...] = ()


class SettingsFormError(ValueError):
    """A safe, field-specific form conversion error."""

    def __init__(self, field, message):
        super().__init__(message)
        self.field = field
        self.message = message


FIELDS = (
    SettingsField("webui_host", ("webui", "host"), default="0.0.0.0"),
    SettingsField("webui_port", ("webui", "port"), "integer", 7767),
    SettingsField("database_path", ("storage", "database_path"), default="./data/speciesid.db"),
    SettingsField("snapshots_path", ("media", "snapshots_path"), default="./media/wamf/snapshots"),
    SettingsField("clips_path", ("media", "clips_path"), default="./media/wamf/clips"),
    SettingsField("retention_enabled", ("retention", "enabled"), "boolean", True),
    SettingsField("snapshots_days", ("retention", "snapshots_days"), "integer", 90),
    SettingsField("clips_days", ("retention", "clips_days"), "integer", 90),
    SettingsField("delete_media", ("retention", "delete_media"), "boolean", False),
    SettingsField("orphan_scan_enabled", ("retention", "orphan_scan_enabled"), "boolean", True),
    SettingsField("delete_orphaned_media", ("retention", "delete_orphaned_media"), "boolean", False),
    SettingsField("system_events_days", ("retention", "system_events_days"), "integer", 90),
    SettingsField("system_events_min_rows", ("retention", "system_events_min_rows"), "integer", 1000),
    SettingsField("config_backups_max_files", ("retention", "config_backups_max_files"), "integer", 10),
    SettingsField("mqtt_host", ("mqtt", "host")),
    SettingsField("mqtt_port", ("mqtt", "port"), "integer", 1883),
    SettingsField("mqtt_topic_prefix", ("mqtt", "topic_prefix"), default="frigate"),
    SettingsField("mqtt_authentication_enabled", ("mqtt", "authentication", "enabled"), "boolean", False),
    SettingsField("mqtt_tls_enabled", ("mqtt", "tls", "enabled"), "boolean", False),
    SettingsField("mqtt_tls_insecure", ("mqtt", "tls", "insecure"), "boolean", False),
    SettingsField("mqtt_tls_ca_certs", ("mqtt", "tls", "ca_certs"), optional=True),
    SettingsField("frigate_url", ("frigate", "frigate_url")),
    SettingsField("frigate_cameras", ("frigate", "camera"), "lines", ()),
    SettingsField("frigate_object", ("frigate", "object"), default="bird"),
    SettingsField("classification_model", ("classification", "model"), default="model.tflite"),
    SettingsField("classification_threshold", ("classification", "threshold"), "float", 0.7),
    SettingsField("live_view_url", ("camera", "live_view_url")),
    SettingsField("bridge_enabled", ("bridge", "enabled"), "boolean", False),
    SettingsField("bridge_events_url", ("bridge", "events_url"), optional=True),
    SettingsField("bridge_timeout_seconds", ("bridge", "timeout_seconds"), "float", 1),
    SettingsField("bridge_health_check_interval_seconds", ("bridge", "health_check_interval_seconds"), "float", 60),
    SettingsField("admin_auth_enabled", ("admin", "auth_enabled"), "boolean", True),
    SettingsField(
        "admin_session_cookie_samesite",
        ("admin", "session_cookie_samesite"),
        "choice",
        "Lax",
        choices=("Lax", "Strict", "None"),
    ),
    SettingsField("admin_session_cookie_secure", ("admin", "session_cookie_secure"), "boolean", False),
    SettingsField("api_token_auth_enabled", ("api", "token_auth_enabled"), "boolean", True),
)


FIELD_BY_NAME = {field.name: field for field in FIELDS}


def _lookup(mapping, path, default=None):
    current = mapping
    for key in path:
        if not isinstance(current, Mapping) or key not in current:
            return deepcopy(default)
        current = current[key]
    return deepcopy(current)


def _set_path(mapping, path, value):
    current = mapping
    for key in path[:-1]:
        child = current.get(key)
        if not isinstance(child, dict):
            child = {}
            current[key] = child
        current = child
    current[path[-1]] = value


def _remove_path(mapping, path):
    parents = []
    current = mapping
    for key in path[:-1]:
        if not isinstance(current, dict) or key not in current:
            return
        parents.append((current, key))
        current = current[key]
    if not isinstance(current, dict):
        return
    current.pop(path[-1], None)
    for parent, key in reversed(parents):
        if parent.get(key) == {}:
            del parent[key]
        else:
            break


def _posted_boolean(form, name):
    values = form.getlist(name)
    return bool(values and values[-1].lower() in {"1", "true", "on", "yes"})


def _convert(field, form):
    raw = form.get(field.name, "")
    if field.kind == "boolean":
        return _posted_boolean(form, field.name)
    if field.kind == "integer":
        try:
            return int(raw)
        except (TypeError, ValueError) as exc:
            raise SettingsFormError(field.name, "Enter a whole number.") from exc
    if field.kind == "float":
        try:
            return float(raw)
        except (TypeError, ValueError) as exc:
            raise SettingsFormError(field.name, "Enter a number.") from exc
    if field.kind == "lines":
        return [line.strip() for line in raw.splitlines() if line.strip()]
    if field.kind == "choice":
        if raw not in field.choices:
            raise SettingsFormError(field.name, "Choose a supported value.")
        return raw
    return raw.strip()


def _candidate_path(field, candidate):
    """Keep the deployed Bridge shape while honoring canonical Perch inputs."""

    if not field.name.startswith("bridge_"):
        return field.path
    if field.name == "bridge_health_check_interval_seconds" and (
        "health" in candidate or "perch" in candidate
    ):
        return ("health", "check_interval_seconds")
    if "perch" in candidate:
        return ("perch", field.path[-1])
    return field.path


def build_settings_candidate(existing_config, form):
    """Apply submitted structured fields to a sanitized canonical-v2 copy."""

    candidate = sanitize_config(migrate_config(existing_config).config)
    candidate["config_version"] = CURRENT_CONFIG_VERSION

    for field in FIELDS:
        if field.name not in form:
            continue
        value = _convert(field, form)
        path = _candidate_path(field, candidate)
        if field.optional and value == "":
            _remove_path(candidate, path)
        else:
            _set_path(candidate, path, value)

    if "live_view_url" in form:
        _remove_path(candidate, ("live_view", "url"))

    if "retention_species_overrides" in form:
        raw_overrides = form.get("retention_species_overrides", "").strip()
        try:
            overrides = yaml.safe_load(raw_overrides) if raw_overrides else {}
        except yaml.YAMLError as exc:
            raise SettingsFormError(
                "retention_species_overrides",
                "Species overrides must be valid YAML.",
            ) from exc
        if not isinstance(overrides, Mapping):
            raise SettingsFormError(
                "retention_species_overrides",
                "Species overrides must be a mapping of species names to settings.",
            )
        _set_path(candidate, ("retention", "species_overrides"), dict(overrides))

    username = form.get("mqtt_username", "")
    password = form.get("mqtt_password", "")
    if username:
        _set_path(candidate, ("mqtt", "authentication", "username"), username)
    if password:
        _set_path(candidate, ("mqtt", "authentication", "password"), password)

    return candidate


def settings_values(config, submitted_form=None):
    """Return template-safe values; secret inputs are always blank."""

    sanitized = sanitize_config(migrate_config(config).config)
    normalized = normalize_config(sanitized)
    values = {}
    for field in FIELDS:
        value = _lookup(sanitized, field.path, field.default)
        if field.name == "live_view_url":
            value = normalized.config["camera"]["live_view_url"]
        elif field.name == "bridge_enabled":
            value = normalized.config["perch"]["enabled"]
        elif field.name == "bridge_events_url":
            value = normalized.config["perch"]["events_url"] or ""
        elif field.name == "bridge_timeout_seconds":
            value = normalized.config["perch"]["timeout_seconds"]
        elif field.name == "bridge_health_check_interval_seconds":
            value = normalized.config["health"]["check_interval_seconds"]
        if field.kind == "lines":
            value = "\n".join(value) if isinstance(value, list) else ""
        values[field.name] = value

    overrides = _lookup(sanitized, ("retention", "species_overrides"), {})
    values["retention_species_overrides"] = yaml.safe_dump(
        overrides if isinstance(overrides, Mapping) else {},
        sort_keys=False,
    ).strip()

    if submitted_form is not None:
        for field in FIELDS:
            if field.name not in submitted_form:
                continue
            if field.kind == "boolean":
                values[field.name] = _posted_boolean(submitted_form, field.name)
            else:
                values[field.name] = submitted_form.get(field.name, "")
        if "retention_species_overrides" in submitted_form:
            values["retention_species_overrides"] = submitted_form.get(
                "retention_species_overrides", ""
            )

    values["mqtt_username"] = ""
    values["mqtt_password"] = ""
    return values
