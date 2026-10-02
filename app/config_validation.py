"""Pure structured validation for already-loaded WAMF configuration."""

from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit

from app.config_normalization import normalize_config
from app.retention_schedule import RetentionScheduleError, parse_retention_schedule


@dataclass(frozen=True)
class ValidationIssue:
    """A field-specific configuration diagnostic."""

    field: str
    message: str

    def __str__(self):
        return f"{self.field}: {self.message}"


@dataclass(frozen=True)
class ConfigValidationResult:
    """The independent structural, readiness, and warning outcomes."""

    errors: tuple[ValidationIssue, ...] = ()
    readiness_issues: tuple[ValidationIssue, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def is_valid(self):
        return not self.errors

    @property
    def is_ready(self):
        return self.is_valid and not self.readiness_issues


def is_placeholder(value):
    """Return whether a required string is blank or an example placeholder."""

    if not isinstance(value, str) or not value.strip():
        return True
    value = value.strip().lower()
    return "<" in value or value.startswith(
        ("your-", "your_", "change-me", "changeme")
    )


def _mapping_errors(config):
    errors = []
    sections = (
        "frigate",
        "mqtt",
        "classification",
        "webui",
        "storage",
        "media",
        "admin",
        "api",
        "retention",
        "perch",
        "bridge",
        "health",
        "camera",
        "live_view",
    )
    for section in sections:
        if section in config and not isinstance(config[section], Mapping):
            errors.append(ValidationIssue(section, "must be a YAML mapping"))

    mqtt = config.get("mqtt")
    if isinstance(mqtt, Mapping):
        for subsection in ("authentication", "tls"):
            if subsection in mqtt and not isinstance(mqtt[subsection], Mapping):
                errors.append(
                    ValidationIssue(
                        f"mqtt.{subsection}",
                        "must be a YAML mapping",
                    )
                )
    return errors


def _validate_webui(config, errors, readiness_issues):
    webui = config.get("webui")
    if webui is not None and not isinstance(webui, Mapping):
        return
    webui = webui or {}

    port = webui.get("port", 7767)
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        errors.append(
            ValidationIssue("webui.port", "must be an integer from 1 to 65535")
        )

    host = webui.get("host", "0.0.0.0")
    if host is not None and not isinstance(host, str):
        errors.append(ValidationIssue("webui.host", "must be a string"))
    elif is_placeholder(host):
        readiness_issues.append(
            ValidationIssue("webui.host", "a bind address is required")
        )


def _validate_frigate(config, errors, readiness_issues):
    frigate = config.get("frigate")
    if frigate is not None and not isinstance(frigate, Mapping):
        return
    frigate = frigate or {}

    frigate_url = frigate.get("frigate_url")
    if frigate_url is not None and not isinstance(frigate_url, str):
        errors.append(ValidationIssue("frigate.frigate_url", "must be a string"))
    elif is_placeholder(frigate_url):
        readiness_issues.append(
            ValidationIssue("frigate.frigate_url", "an HTTP/HTTPS URL is required")
        )
    else:
        try:
            parsed_url = urlsplit(frigate_url)
            valid_url = parsed_url.scheme in ("http", "https") and parsed_url.hostname
        except ValueError:
            valid_url = False
        if not valid_url:
            errors.append(
                ValidationIssue(
                    "frigate.frigate_url",
                    "must be a valid HTTP/HTTPS URL",
                )
            )

    cameras = frigate.get("camera")
    if cameras is None or cameras == []:
        readiness_issues.append(
            ValidationIssue("frigate.camera", "at least one camera is required")
        )
    elif not isinstance(cameras, list):
        errors.append(ValidationIssue("frigate.camera", "must be a YAML list"))
    elif any(not isinstance(camera, str) for camera in cameras):
        errors.append(
            ValidationIssue("frigate.camera", "camera names must be strings")
        )
    elif any(is_placeholder(camera) for camera in cameras):
        readiness_issues.append(
            ValidationIssue("frigate.camera", "camera names must be configured")
        )


def _validate_mqtt(mqtt, errors, readiness_issues):
    for field in ("host", "topic_prefix"):
        value = mqtt.get(field)
        path = f"mqtt.{field}"
        if value is not None and not isinstance(value, str):
            errors.append(ValidationIssue(path, "must be a string"))
        elif is_placeholder(value):
            readiness_issues.append(
                ValidationIssue(path, "a non-placeholder value is required")
            )

    port = mqtt.get("port", 1883)
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        errors.append(
            ValidationIssue("mqtt.port", "must be an integer from 1 to 65535")
        )

    authentication = mqtt["authentication"]
    authentication_enabled = authentication.get("enabled", False)
    if not isinstance(authentication_enabled, bool):
        errors.append(
            ValidationIssue("mqtt.authentication.enabled", "must be a boolean")
        )
    for field in ("username", "password"):
        value = authentication.get(field)
        path = f"mqtt.authentication.{field}"
        if value is not None and not isinstance(value, str):
            errors.append(ValidationIssue(path, "must be a string"))
        elif authentication_enabled is True and is_placeholder(value):
            readiness_issues.append(
                ValidationIssue(path, "is required when authentication is enabled")
            )

    tls = mqtt["tls"]
    for field in ("enabled", "insecure"):
        if not isinstance(tls.get(field, False), bool):
            errors.append(ValidationIssue(f"mqtt.tls.{field}", "must be a boolean"))
    ca_certs = tls.get("ca_certs")
    if ca_certs is not None and not isinstance(ca_certs, str):
        errors.append(ValidationIssue("mqtt.tls.ca_certs", "must be a string"))


def _validate_classification(config, errors, readiness_issues):
    classification = config.get("classification")
    if classification is not None and not isinstance(classification, Mapping):
        return
    classification = classification or {}

    model = classification.get("model")
    if model is not None and not isinstance(model, str):
        errors.append(ValidationIssue("classification.model", "must be a string"))
    elif is_placeholder(model):
        readiness_issues.append(
            ValidationIssue("classification.model", "a model path is required")
        )

    threshold = classification.get("threshold")
    if threshold is None:
        readiness_issues.append(
            ValidationIssue("classification.threshold", "a threshold is required")
        )
    elif (
        isinstance(threshold, bool)
        or not isinstance(threshold, (int, float))
        or not 0 <= threshold <= 1
    ):
        errors.append(
            ValidationIssue(
                "classification.threshold",
                "must be a number from 0 to 1",
            )
        )


def _validate_retention_schedule(config, errors):
    retention = config.get("retention")
    if retention is not None and not isinstance(retention, Mapping):
        return
    try:
        parse_retention_schedule(config)
    except RetentionScheduleError as exc:
        errors.append(ValidationIssue(exc.field, exc.message))


def validate_config(config):
    """Validate loaded configuration without I/O, mutation, or connectivity checks."""

    if not isinstance(config, Mapping):
        return ConfigValidationResult(
            errors=(ValidationIssue("configuration", "must be a YAML mapping"),)
        )

    errors = _mapping_errors(config)
    readiness_issues = []
    _validate_webui(config, errors, readiness_issues)
    _validate_frigate(config, errors, readiness_issues)
    _validate_classification(config, errors, readiness_issues)
    _validate_retention_schedule(config, errors)

    normalization_blocking_fields = {
        "mqtt",
        "mqtt.authentication",
        "mqtt.tls",
        "frigate",
        "perch",
        "bridge",
        "health",
        "camera",
        "live_view",
    }
    normalization_blocked = any(
        issue.field in normalization_blocking_fields for issue in errors
    )
    warnings = ()
    if not normalization_blocked:
        normalized = normalize_config(config)
        warnings = normalized.warnings
        _validate_mqtt(normalized.config["mqtt"], errors, readiness_issues)

    return ConfigValidationResult(
        errors=tuple(errors),
        readiness_issues=tuple(readiness_issues),
        warnings=warnings,
    )
