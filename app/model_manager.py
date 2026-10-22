"""Settings presentation and explicit selection, reusing M1/M2 boundaries."""

from copy import deepcopy
from pathlib import Path
from threading import Lock

from app.model_catalogue import load_catalogue
from app.model_install import InstallError, model_install_guard
from app.model_registry import (
    artifact_path,
    discover_local_models,
    resolve_classifier_model,
)
from app.model_validation import validate_model_artifact
from wamf_paths import get_models_path


class ModelSelectionError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


_validation_cache = {}
_cache_lock = Lock()


def _inspect(path, artifact, root):
    """Cache observations of unchanged local files, never selection authority.

    Selection always performs fresh isolated validation under the M2 lock.
    No download, native interpreter or manager starts at module import.
    """
    try:
        stat = path.stat()
        key = (str(path), str(root), artifact, stat.st_dev, stat.st_ino,
               stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
    except OSError:
        return validate_model_artifact(path, artifact, allowed_root=root)
    with _cache_lock:
        cached = _validation_cache.get(key)
    if cached is not None:
        return cached
    result = validate_model_artifact(path, artifact, allowed_root=root)
    with _cache_lock:
        if len(_validation_cache) >= 128:
            _validation_cache.clear()
        _validation_cache[key] = result
    return result


def _identity(artifact):
    return (artifact.model_id, artifact.version)


def build_model_manager(config, runtime=None, *, catalogue=None):
    """Return safe view data; running identity comes only from the worker."""
    from app.model_registry import ModelSelectionError as ResolverError

    catalogue = catalogue or load_catalogue()
    local = discover_local_models(catalogue, config)
    selected_error = None
    try:
        selection = resolve_classifier_model(config, catalogue)
        selected_identity = _identity(selection.artifact) if selection.managed else ("custom", str(selection.path))
    except (ResolverError, ValueError, OSError):
        selection = None
        selected_identity = None
        selected_error = "The saved model selection is invalid or unavailable. Review Classification or Advanced YAML."

    observed = runtime.classifier if runtime is not None and runtime.is_current else None
    running_identity = None
    running_label = "Unknown — current classifier status is unavailable."
    if observed is not None and observed.state == "ready":
        running_identity = ((observed.model_id, observed.model_version) if observed.model_id
                            else ("custom", observed.path))
        running_label = "Custom model · " + Path(observed.path or "unknown").name
    elif observed is not None and observed.state == "failed":
        running_label = "Unavailable — classifier initialization failed."
    elif observed is not None and observed.state == "starting":
        running_label = "Starting classifier…"

    cards = []
    selected_label = "Invalid model selection" if selected_error else "Custom model · " + selection.path.name
    for model in catalogue.models:
        for artifact in model.artifacts:
            item = next(entry for entry in local if entry.artifact == artifact)
            validation = None
            if item.installed and item.path is not None:
                root = Path(__file__).resolve().parent.parent if item.bundled else get_models_path(config)
                validation = _inspect(item.path, artifact, root)
            valid = bool(validation and validation.valid)
            identity = _identity(artifact)
            label = f"{model.name} · {artifact.version}"
            # Recognize the official bundled legacy file for display only;
            # never rewrite legacy configuration or infer loaded identity from it.
            if (selection is not None and not selection.managed and valid
                    and selection.path == item.path):
                selected_identity = identity
            if (observed is not None and observed.state == "ready"
                    and not observed.model_id and item.path is not None
                    and observed.path == str(item.path) and observed.sha256 == artifact.sha256):
                running_identity = identity
            if running_identity == identity:
                running_label = label
            if selected_identity == identity:
                selected_label = label
                if not valid:
                    selected_error = "The selected model is missing or invalid. It cannot be used at the next start."
            issue = (validation.message if validation is not None and not valid else
                     "Artifact is missing or does not match the supported version."
                     if item.issue and (item.bundled or item.issue != "missing") else None)
            cards.append({
                "model_id": model.id, "version": artifact.version, "name": model.name,
                "description": model.description, "bundled": item.bundled,
                "installed": item.installed, "valid": valid,
                "selected": selected_identity == identity, "active": running_identity == identity,
                "selected_for_next_start": selected_identity == identity and selected_identity != running_identity,
                "issue": issue, "size_bytes": artifact.size_bytes,
                "source": artifact.source, "licence": artifact.licence, "notes": artifact.notes,
                "can_install": not item.bundled and not valid,
                "can_select": valid and selected_identity != identity,
            })
    restart_required = running_identity is not None and selected_identity is not None and running_identity != selected_identity
    return {
        "models": cards, "running_label": running_label, "selected_label": selected_label,
        "restart_required": restart_required, "running_known": running_identity is not None,
        "selected_error": selected_error, "custom": bool(selection and not selection.managed),
    }


def select_model(model_id, version, *, reload_callback=None):
    """Freshly validate then patch only selection under install + config locks."""
    from app.config_editor import write_config_preserving_admin
    from app.config_loader import load_runtime_config

    catalogue = load_catalogue()
    artifact = catalogue.get_artifact(model_id, version)
    if artifact is None:
        raise ModelSelectionError("unknown_model", "That model version is not supported.")
    config = load_runtime_config()
    root = get_models_path(config)
    try:
        with model_install_guard(root):
            def update(current):
                # Read/validate against the current locked storage configuration.
                # An intervening root change cannot select a different file.
                if get_models_path(current) != root:
                    raise ModelSelectionError("configuration_changed", "Model storage changed. Refresh and try again.")
                path = artifact_path(artifact, current)
                allowed = Path(__file__).resolve().parent.parent if artifact.bundled_path else root
                result = validate_model_artifact(path, artifact, allowed_root=allowed)
                if not result.valid:
                    raise ModelSelectionError("invalid_model", "Only an installed, validated model can be selected.")
                candidate = deepcopy(current)
                classification = candidate.setdefault("classification", {})
                if not isinstance(classification, dict):
                    raise ModelSelectionError("configuration_error", "Classification settings could not be updated.")
                classification.pop("model", None)
                classification.update(model_id=model_id, model_version=version)
                return candidate
            write_config_preserving_admin(None, update=update, reload_callback=reload_callback)
    except InstallError as exc:
        raise ModelSelectionError("install_in_progress", "Another model installation is running. Try again when it finishes.") from exc
