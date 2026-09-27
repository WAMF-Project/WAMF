"""Strict loading, validation, application, and removal of WAMF secrets."""

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path

import yaml


CURRENT_SECRETS_VERSION = 1

# (runtime/persisted config path, secrets.yml path).  This is the complete
# supported inventory; secrets.yml is deliberately not a general override.
SECRET_PATHS = (
    (("mqtt", "authentication", "username"), ("mqtt", "username")),
    (("mqtt", "authentication", "password"), ("mqtt", "password")),
    (("admin", "password_hash"), ("admin", "password_hash")),
    (("admin", "session_secret"), ("admin", "session_secret")),
    (("api", "token_hash"), ("api", "token_hash")),
)


class SecretsValidationError(ValueError):
    """The secrets document is unsupported or malformed."""


@dataclass(frozen=True)
class SecretExtraction:
    config: dict
    secrets: dict
    embedded_paths: tuple[str, ...]


def get_secrets_path(config_path):
    """Return the secrets file beside the selected config file."""

    return Path(config_path).with_name("secrets.yml")


def empty_secrets():
    return {"secrets_version": CURRENT_SECRETS_VERSION}


def _lookup(mapping, path):
    current = mapping
    for key in path:
        if not isinstance(current, Mapping) or key not in current:
            return False, None
        current = current[key]
    return True, current


def _set_path(mapping, path, value):
    current = mapping
    for key in path[:-1]:
        child = current.get(key)
        if not isinstance(child, dict):
            child = {}
            current[key] = child
        current = child
    current[path[-1]] = deepcopy(value)


def _remove_path(mapping, path):
    parents = []
    current = mapping
    for key in path[:-1]:
        if not isinstance(current, dict) or key not in current:
            return False
        parents.append((current, key))
        current = current[key]
    if not isinstance(current, dict) or path[-1] not in current:
        return False
    del current[path[-1]]
    # Remove only containers made empty by removal.  mqtt.authentication is
    # retained when it still carries the ordinary ``enabled`` setting.
    for parent, key in reversed(parents):
        child = parent.get(key)
        if isinstance(child, dict) and not child:
            del parent[key]
        else:
            break
    return True


def validate_secrets(secrets):
    """Return an independent, strictly validated secrets mapping."""

    if not isinstance(secrets, Mapping):
        raise SecretsValidationError("Secrets must be a YAML mapping.")
    candidate = deepcopy(dict(secrets))
    allowed_top = {"secrets_version", "mqtt", "admin", "api"}
    unknown_top = set(candidate) - allowed_top
    if unknown_top:
        raise SecretsValidationError(
            "Unsupported secrets field: " + sorted(unknown_top)[0]
        )
    version = candidate.get("secrets_version")
    if isinstance(version, bool) or not isinstance(version, int):
        raise SecretsValidationError("secrets_version must be the integer 1.")
    if version != CURRENT_SECRETS_VERSION:
        raise SecretsValidationError(
            f"Secrets version {version} is not supported by this WAMF version."
        )

    supported = {
        "mqtt": {"username", "password"},
        "admin": {"password_hash", "session_secret"},
        "api": {"token_hash"},
    }
    for section, allowed_fields in supported.items():
        if section not in candidate:
            continue
        values = candidate[section]
        if not isinstance(values, Mapping):
            raise SecretsValidationError(f"{section} must be a YAML mapping.")
        unknown = set(values) - allowed_fields
        if unknown:
            raise SecretsValidationError(
                f"Unsupported secrets field: {section}.{sorted(unknown)[0]}"
            )
        for field, value in values.items():
            if not isinstance(value, str):
                raise SecretsValidationError(
                    f"{section}.{field} must be a string."
                )
    return candidate


def load_secrets(path):
    """Load and validate a secrets file, or return an empty v1 document."""

    path = Path(path)
    if not path.exists():
        return empty_secrets()
    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise SecretsValidationError("Secrets could not be loaded.") from exc
    return parse_secrets_content(content)


def parse_secrets_content(content):
    """Parse secrets YAML without allowing parser context to expose values."""

    try:
        loaded = yaml.safe_load(content)
    except (UnicodeError, yaml.YAMLError) as exc:
        raise SecretsValidationError("Secrets could not be loaded.") from exc
    return validate_secrets(loaded)


def apply_secrets(config, secrets):
    """Apply only supported paths to a new independent runtime mapping."""

    if not isinstance(config, Mapping):
        raise ValueError("Configuration must be a YAML mapping")
    validated = validate_secrets(secrets)
    applied = deepcopy(dict(config))
    for config_path, secret_path in SECRET_PATHS:
        present, value = _lookup(validated, secret_path)
        if present:
            _set_path(applied, config_path, value)
    return applied


def sanitize_config(config):
    """Return an independent persisted-config mapping without secret leaves."""

    if not isinstance(config, Mapping):
        raise ValueError("Configuration must be a YAML mapping")
    sanitized = deepcopy(dict(config))
    for config_path, _ in SECRET_PATHS:
        _remove_path(sanitized, config_path)
    return sanitized


def merge_embedded_secrets(config, existing_secrets):
    """Extract embedded values with existing secrets taking precedence."""

    merged = validate_secrets(existing_secrets)
    sanitized = deepcopy(dict(config))
    embedded = []
    for config_path, secret_path in SECRET_PATHS:
        present, value = _lookup(config, config_path)
        if not present:
            continue
        embedded.append(".".join(config_path))
        existing_present, _ = _lookup(merged, secret_path)
        if not existing_present:
            _set_path(merged, secret_path, value)
        _remove_path(sanitized, config_path)
    return SecretExtraction(
        config=sanitized,
        # Bootstrap may replace legacy null/placeholder admin credentials
        # before the resulting candidate receives strict validation.
        secrets=merged,
        embedded_paths=tuple(embedded),
    )


def secret_updates(candidate, existing_secrets):
    """Merge present non-blank supported inputs, preserving omitted/blank values."""

    merged = validate_secrets(existing_secrets)
    for config_path, secret_path in SECRET_PATHS:
        present, value = _lookup(candidate, config_path)
        if present and value not in (None, ""):
            _set_path(merged, secret_path, value)
    return validate_secrets(merged)
