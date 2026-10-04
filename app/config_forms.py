"""Structured Administration Settings form mapping for canonical config v2."""

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass

from app.config_migration import CURRENT_CONFIG_VERSION, migrate_config
from app.config_normalization import normalize_config
from app.config_secrets import sanitize_config
from app.retention_schedule import (
    DEFAULT_RETENTION_SCHEDULE_ENABLED,
    DEFAULT_RETENTION_SCHEDULE_TIME,
    DEFAULT_RETENTION_SCHEDULE_TIMEZONE,
)


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
    SettingsField(
        "retention_schedule_enabled",
        ("retention", "schedule", "enabled"),
        "boolean",
        DEFAULT_RETENTION_SCHEDULE_ENABLED,
    ),
    SettingsField(
        "retention_schedule_time",
        ("retention", "schedule", "time"),
        default=DEFAULT_RETENTION_SCHEDULE_TIME,
    ),
    SettingsField(
        "retention_schedule_timezone",
        ("retention", "schedule", "timezone"),
        default=DEFAULT_RETENTION_SCHEDULE_TIMEZONE,
    ),
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
MAX_SPECIES_ROW_ID = 9999
CANONICAL_RETENTION_INTEGER_FIELDS = {
    "snapshots_days",
    "clips_days",
    "system_events_days",
    "system_events_min_rows",
}


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


def _normalized_species_name(value):
    return value.strip().casefold()


def normalized_species_catalog(known_species):
    """Return one deterministic metadata record per normalized species name."""

    candidates = {}
    for species in known_species or ():
        if not isinstance(species, Mapping):
            continue
        raw_scientific_name = species.get("scientific_name")
        if not isinstance(raw_scientific_name, str):
            continue
        scientific_name = raw_scientific_name.strip()
        if not scientific_name:
            continue
        normalized_name = _normalized_species_name(scientific_name)
        common_name = species.get("common_name")
        common_name_sort = common_name if isinstance(common_name, str) else ""
        rank = (
            scientific_name,
            common_name_sort.casefold(),
            common_name_sort,
        )
        current = candidates.get(normalized_name)
        if current is not None and current[0] <= rank:
            continue
        canonical = dict(species)
        canonical["scientific_name"] = scientific_name
        candidates[normalized_name] = (rank, canonical)

    return {
        normalized_name: candidates[normalized_name][1]
        for normalized_name in sorted(candidates)
    }


def _known_species_by_name(known_species):
    return {
        normalized_name: species["scientific_name"]
        for normalized_name, species in normalized_species_catalog(
            known_species
        ).items()
    }


def _validated_species_row_ids(form):
    row_ids = []
    seen = set()
    for row_id in form.getlist("retention_species_row"):
        if (
            not isinstance(row_id, str)
            or not row_id
            or len(row_id) > 4
            or not row_id.isascii()
            or not row_id.isdecimal()
            or (len(row_id) > 1 and row_id.startswith("0"))
        ):
            raise SettingsFormError(
                "retention_species_editor", "Species override row is invalid."
            )
        row_number = int(row_id)
        if row_number > MAX_SPECIES_ROW_ID or row_number in seen:
            raise SettingsFormError(
                "retention_species_editor", "Species override row is invalid."
            )
        seen.add(row_number)
        row_ids.append(row_id)
    return row_ids


def _existing_species_overrides(config):
    retention = config.get("retention", {})
    if not isinstance(retention, Mapping):
        return {}
    overrides = retention.get("species_overrides", {})
    return dict(overrides) if isinstance(overrides, Mapping) else {}


def _override_day_value(form, field_name):
    raw = form.get(field_name, "").strip()
    if raw == "":
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise SettingsFormError(
            field_name, "Enter zero or a positive whole number, or leave blank."
        ) from exc
    if value < 0:
        raise SettingsFormError(
            field_name, "Enter zero or a positive whole number, or leave blank."
        )
    return value


def _build_species_overrides(existing_config, form, known_species):
    retention = existing_config.get("retention", {})
    raw_existing = (
        retention.get("species_overrides", {})
        if isinstance(retention, Mapping)
        else {}
    )
    if not isinstance(raw_existing, Mapping):
        raise SettingsFormError(
            "retention_species_editor",
            "Existing species overrides must be a mapping before Settings can save.",
        )
    existing = _existing_species_overrides(existing_config)
    existing_by_name = {
        _normalized_species_name(name): (name, values)
        for name, values in existing.items()
        if isinstance(name, str) and name.strip()
    }
    known_by_name = _known_species_by_name(known_species)
    overrides = {}
    submitted_names = set()

    for row_id in _validated_species_row_ids(form):
        name_field = f"retention_species_scientific_name_{row_id}"
        submitted_name = form.get(name_field, "").strip()
        normalized_name = _normalized_species_name(submitted_name)
        if not normalized_name:
            raise SettingsFormError(name_field, "Choose a species.")
        if normalized_name in submitted_names:
            raise SettingsFormError(
                name_field, "This species already has a retention override."
            )
        submitted_names.add(normalized_name)

        existing_entry = existing_by_name.get(normalized_name)
        if existing_entry is not None:
            scientific_name, raw_values = existing_entry
            if not isinstance(raw_values, Mapping):
                raise SettingsFormError(
                    "retention_species_editor",
                    f"The existing override for {scientific_name} is not a mapping.",
                )
        elif normalized_name in known_by_name:
            scientific_name = known_by_name[normalized_name]
            raw_values = {}
        else:
            raise SettingsFormError(
                name_field,
                "Choose a species from the cached metadata list.",
            )

        values = deepcopy(dict(raw_values)) if isinstance(raw_values, Mapping) else {}
        snapshots_field = f"retention_species_snapshots_days_{row_id}"
        clips_field = f"retention_species_clips_days_{row_id}"
        snapshots_days = _override_day_value(form, snapshots_field)
        clips_days = _override_day_value(form, clips_field)
        if snapshots_days is not None:
            values["snapshots_days"] = snapshots_days
        elif (
            "snapshots_days" not in values
            or _valid_override_day(values["snapshots_days"])
        ):
            values.pop("snapshots_days", None)
        if clips_days is not None:
            values["clips_days"] = clips_days
        elif "clips_days" not in values or _valid_override_day(
            values["clips_days"]
        ):
            values.pop("clips_days", None)
        overrides[scientific_name] = values

    return overrides


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


def _valid_override_day(value):
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


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


def build_settings_candidate(existing_config, form, known_species=()):
    """Apply submitted structured fields to a sanitized canonical-v2 copy."""

    candidate = sanitize_config(migrate_config(existing_config).config)
    candidate["config_version"] = CURRENT_CONFIG_VERSION

    for field in FIELDS:
        if field.name not in form:
            continue
        if (
            field.name in CANONICAL_RETENTION_INTEGER_FIELDS
            and form.get(field.name, "").strip() == ""
            and not _valid_override_day(_lookup(candidate, field.path))
        ):
            # Keep an invalid existing A1 value intact until the user submits
            # an explicit valid correction; browser number-input sanitization
            # must not turn it into a different form-level error or omission.
            continue
        value = _convert(field, form)
        path = _candidate_path(field, candidate)
        if field.optional and value == "":
            _remove_path(candidate, path)
        else:
            _set_path(candidate, path, value)

    if "live_view_url" in form:
        _remove_path(candidate, ("live_view", "url"))

    if "retention_species_overrides_present" in form:
        overrides = _build_species_overrides(existing_config, form, known_species)
        _set_path(candidate, ("retention", "species_overrides"), overrides)

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

    if submitted_form is not None:
        for field in FIELDS:
            if field.name not in submitted_form:
                continue
            if field.kind == "boolean":
                values[field.name] = _posted_boolean(submitted_form, field.name)
            else:
                values[field.name] = submitted_form.get(field.name, "")
    values["mqtt_username"] = ""
    values["mqtt_password"] = ""
    return values


def settings_species_override_rows(config, submitted_form=None):
    """Return stable structured rows without treating metadata as policy state."""

    if (
        submitted_form is not None
        and "retention_species_overrides_present" in submitted_form
    ):
        try:
            submitted_row_ids = _validated_species_row_ids(submitted_form)
        except SettingsFormError:
            submitted_row_ids = None

        if submitted_row_ids is not None:
            rows = []
            for rendered_row_id, submitted_row_id in enumerate(submitted_row_ids):
                rows.append(
                    {
                        "row_id": str(rendered_row_id),
                        "submitted_row_id": submitted_row_id,
                        "scientific_name": submitted_form.get(
                            f"retention_species_scientific_name_{submitted_row_id}", ""
                        ),
                        "snapshots_days": submitted_form.get(
                            f"retention_species_snapshots_days_{submitted_row_id}", ""
                        ),
                        "clips_days": submitted_form.get(
                            f"retention_species_clips_days_{submitted_row_id}", ""
                        ),
                    }
                )
            return rows

    rows = []
    for row_id, (scientific_name, raw_values) in enumerate(
        _existing_species_overrides(config).items()
    ):
        values = raw_values if isinstance(raw_values, Mapping) else {}
        rows.append(
            {
                "row_id": str(row_id),
                "scientific_name": scientific_name,
                "snapshots_days": values.get("snapshots_days", ""),
                "clips_days": values.get("clips_days", ""),
            }
        )
    return rows
