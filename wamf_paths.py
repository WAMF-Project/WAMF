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


def _configured_path(section, key, fallback, config=None):
    source = _load_config() if config is None else config
    value = source.get(section, {}).get(key)
    if not value:
        return fallback

    path = Path(value).expanduser()
    return path if path.is_absolute() else REPO_ROOT / path


def get_database_path(config=None):
    return _configured_path(
        "storage", "database_path", DEFAULT_DATABASE_PATH, config
    )


def get_snapshots_path(config=None):
    return _configured_path(
        "media", "snapshots_path", DEFAULT_SNAPSHOTS_PATH, config
    )


def get_clips_path(config=None):
    return _configured_path("media", "clips_path", DEFAULT_CLIPS_PATH, config)


class ModelPathError(ValueError):
    """A managed artifact would escape its WAMF-owned storage root."""


def get_models_path(config=None):
    """Use the existing data mount, independently of database location."""
    return _configured_path("storage", "models_path", REPO_ROOT / "data" / "models", config)


def _contained_model_path(root, *parts):
    try:
        root = Path(root).resolve()
        candidate = root.joinpath(*parts).resolve()
        if candidate == root or root not in candidate.parents:
            raise ModelPathError("Model path must remain beneath its storage root")
        return candidate
    except (OSError, RuntimeError) as exc:
        raise ModelPathError("Model path could not be resolved safely") from exc


def get_model_id_path(model_id, config=None):
    from app.model_catalogue import safe_identity

    safe_identity(model_id, "model_id")
    return _contained_model_path(get_models_path(config), model_id)


def get_model_version_path(model_id, version, config=None):
    from app.model_catalogue import safe_identity

    safe_identity(model_id, "model_id")
    safe_identity(version, "model_version")
    return _contained_model_path(get_models_path(config), model_id, version)


def get_model_artifact_path(model_id, version, filename, config=None):
    from app.model_catalogue import safe_component, safe_identity

    safe_identity(model_id, "model_id")
    safe_identity(version, "model_version")
    safe_component(filename, "filename")
    return _contained_model_path(get_models_path(config), model_id, version, filename)


def get_bundled_model_path(value, repo_root=None):
    from app.model_catalogue import safe_bundled_path

    safe_bundled_path(value)
    return _contained_model_path(repo_root or REPO_ROOT, value)


def ensure_storage_paths(config=None):
    """Create the database parent and configured media/model directories."""
    paths = (
        get_database_path(config).parent,
        get_snapshots_path(config),
        get_clips_path(config),
        get_models_path(config),
    )
    for path in paths:
        path.mkdir(parents=True, exist_ok=True)
    return paths


def resolve_media_path(value, media_type, config=None):
    """Resolve filename-only, old repo-relative, and absolute database values."""
    if not value:
        return None

    path = Path(value)
    if path.is_absolute():
        return path

    media_root = (
        get_snapshots_path(config)
        if media_type == "snapshots"
        else get_clips_path(config)
    )
    # Archived rows historically used media/wamf/<type>/<filename>. Mapping
    # relative values by basename keeps those rows portable across deployments.
    return media_root / path.name


def is_wamf_media_path(path, media_type=None, config=None):
    """Return whether a path resolves within the configured WAMF media roots."""

    if not path:
        return False

    try:
        if media_type:
            media_path = resolve_media_path(path, media_type, config).resolve()
        else:
            media_path = Path(path).resolve()
        allowed_dirs = (
            get_snapshots_path(config).resolve(),
            get_clips_path(config).resolve(),
        )
        return any(
            media_path == allowed_dir or allowed_dir in media_path.parents
            for allowed_dir in allowed_dirs
        )
    except (OSError, RuntimeError):
        return False


def contained_media_path(path, media_type, config=None):
    """Resolve an archived path, rejecting targets outside its specific root."""

    resolved = resolve_media_path(path, media_type, config)
    if resolved is None:
        return None

    try:
        candidate = resolved.resolve()
        allowed_root = (
            get_snapshots_path(config)
            if media_type == "snapshots"
            else get_clips_path(config)
        ).resolve()
        if candidate != allowed_root and allowed_root not in candidate.parents:
            return None
    except (OSError, RuntimeError):
        return None
    return resolved


def delete_wamf_media_files(
    snapshot_path=None,
    clip_path=None,
    *,
    config=None,
):
    """Delete existing regular WAMF media files after archive containment checks."""

    deleted = []
    for media_path, media_type in (
        (snapshot_path, "snapshots"),
        (clip_path, "clips"),
    ):
        path = contained_media_path(media_path, media_type, config)
        if path is None or not path.exists() or not path.is_file():
            continue
        path.unlink()
        deleted.append(str(path))
    return deleted
