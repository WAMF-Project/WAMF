"""Shared persisted and runtime configuration loading."""

from pathlib import Path

import yaml

from app.config_editor import get_config_path
from app.config_migration import migrate_config
from app.config_secrets import apply_secrets, get_secrets_path, load_secrets


def load_persisted_config(config_path=None):
    path = Path(config_path or get_config_path())
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ValueError("Configuration could not be loaded.") from exc
    if not isinstance(loaded, dict):
        raise ValueError("Configuration must be a YAML mapping")
    return loaded


def load_runtime_config(config_path=None):
    """Load canonical persisted config and strictly apply adjacent secrets."""

    path = Path(config_path or get_config_path())
    config = migrate_config(load_persisted_config(path)).config
    secrets = load_secrets(get_secrets_path(path))
    return apply_secrets(config, secrets)
