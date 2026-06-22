"""Central configuration and compatibility helpers for WAMF storage paths."""

from pathlib import Path

import yaml

from app.config_editor import get_config_path


REPO_ROOT = Path(__file__).resolve().parent
DEFAULT_DATABASE_PATH = REPO_ROOT / "data" / "speciesid.db"
DEFAULT_SNAPSHOTS_PATH = REPO_ROOT / "media" / "wamf" / "snapshots"
DEFAULT_CLIPS_PATH = REPO_ROOT / "media" / "wamf" / "clips"


def _load_config():
    config_path = Path(get_config_path())
    if not config_path.is_absolute():
        config_path = REPO_ROOT / config_path

    try:
        with config_path.open("r", encoding="utf-8") as config_file:
            return yaml.safe_load(config_file) or {}
    except (OSError, yaml.YAMLError):
        return {}


def _configured_path(section, key, fallback):
    value = _load_config().get(section, {}).get(key)
    if not value:
        return fallback

    path = Path(value).expanduser()
    return path if path.is_absolute() else REPO_ROOT / path


def get_database_path():
    return _configured_path("storage", "database_path", DEFAULT_DATABASE_PATH)


def get_snapshots_path():
    return _configured_path("media", "snapshots_path", DEFAULT_SNAPSHOTS_PATH)


def get_clips_path():
    return _configured_path("media", "clips_path", DEFAULT_CLIPS_PATH)


def ensure_storage_paths():
    """Create the database parent and configured media directories."""
    paths = (
        get_database_path().parent,
        get_snapshots_path(),
        get_clips_path(),
    )
    for path in paths:
        path.mkdir(parents=True, exist_ok=True)
    return paths


def resolve_media_path(value, media_type):
    """Resolve filename-only, old repo-relative, and absolute database values."""
    if not value:
        return None

    path = Path(value)
    if path.is_absolute():
        return path

    media_root = (
        get_snapshots_path() if media_type == "snapshots" else get_clips_path()
    )
    # Archived rows historically used media/wamf/<type>/<filename>. Mapping
    # relative values by basename keeps those rows portable across deployments.
    return media_root / path.name
