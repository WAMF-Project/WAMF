"""Local classifier identity, selection and discovery; never installs artifacts."""

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import stat

from app.model_catalogue import ModelArtifact, REPO_ROOT, load_catalogue


class ModelSelectionError(ValueError):
    def __init__(self, field, message):
        super().__init__(f"{field}: {message}")
        self.field = field
        self.message = message


class ArtifactIntegrityError(ValueError):
    """A normal missing, unreadable or modified local artifact."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ClassifierModel:
    path: Path
    artifact: ModelArtifact | None = None

    @property
    def managed(self):
        return self.artifact is not None


@dataclass(frozen=True)
class LocalModelArtifact:
    artifact: ModelArtifact
    path: Path | None
    bundled: bool
    installed: bool
    integrity_verified: bool = False
    issue: str | None = None


def selected_catalogue_artifact(classification, catalogue=None):
    """Validate selection syntax/identity only, without inspecting model files.

    Presence of either managed key is explicit, including null/blank values.
    Legacy and managed keys are mutually exclusive. No migration guesses IDs.
    """
    if not isinstance(classification, Mapping):
        raise ModelSelectionError("classification", "must be a YAML mapping")
    if not any(key in classification for key in ("model_id", "model_version")):
        return None
    if "model" in classification:
        raise ModelSelectionError("classification.model", "cannot be combined with managed model selection")
    for key in ("model_id", "model_version"):
        value = classification.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ModelSelectionError(f"classification.{key}", "is required for managed model selection")
    catalogue = catalogue if catalogue is not None else load_catalogue()
    model_id, version = classification["model_id"], classification["model_version"]
    if not any(model.id == model_id for model in catalogue.models):
        raise ModelSelectionError("classification.model_id", "unknown supported model")
    artifact = catalogue.get_artifact(model_id, version)
    if artifact is None:
        raise ModelSelectionError("classification.model_version", "unknown supported artifact version")
    return artifact


def artifact_path(artifact, config=None, *, repo_root=None):
    # Lazy import avoids the config editor/validation/path dependency cycle.
    from wamf_paths import get_bundled_model_path, get_model_artifact_path

    if artifact.bundled_path is not None:
        return get_bundled_model_path(artifact.bundled_path, repo_root=repo_root)
    return get_model_artifact_path(artifact.model_id, artifact.version, artifact.filename, config)


def resolve_classifier_model(config, catalogue=None, *, repo_root=None):
    if not isinstance(config, Mapping):
        raise ModelSelectionError("configuration", "must be a YAML mapping")
    classification = config.get("classification", {})
    artifact = selected_catalogue_artifact(classification, catalogue)
    if artifact is not None:
        return ClassifierModel(artifact_path(artifact, config, repo_root=repo_root), artifact)
    value = classification.get("model")
    if not isinstance(value, str) or not value.strip():
        raise ModelSelectionError("classification.model", "a model path is required")
    path = Path(value).expanduser()
    return ClassifierModel((repo_root or REPO_ROOT) / path)


def check_artifact_integrity(path, artifact):
    """Bounded-memory size/hash verification; not LiteRT validation."""
    try:
        # A FIFO/device must not block an integrity check before fstat.
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode):
                raise ArtifactIntegrityError("not_regular_file")
            if info.st_size != artifact.size_bytes:
                raise ArtifactIntegrityError("size_mismatch")
            digest = hashlib.sha256()
            count = 0
            with os.fdopen(descriptor, "rb", closefd=False) as handle:
                while chunk := handle.read(min(1024 * 1024, artifact.size_bytes - count + 1)):
                    count += len(chunk)
                    if count > artifact.size_bytes:
                        raise ArtifactIntegrityError("size_mismatch")
                    digest.update(chunk)
            if count != artifact.size_bytes:
                raise ArtifactIntegrityError("size_mismatch")
            if digest.hexdigest() != artifact.sha256:
                raise ArtifactIntegrityError("checksum_mismatch")
        finally:
            os.close(descriptor)
    except FileNotFoundError as exc:
        raise ArtifactIntegrityError("missing") from exc
    except OSError as exc:
        raise ArtifactIntegrityError("unreadable") from exc


def discover_local_models(catalogue=None, config=None, *, repo_root=None):
    """Presence is separate from validation, selection and running state.

    Bundled bytes must match before they identify an official installed model.
    A mismatched legacy file is left untouched and remains usable as custom.
    Downloaded presence alone never asserts integrity or runtime validity.
    """
    from wamf_paths import ModelPathError

    catalogue = catalogue if catalogue is not None else load_catalogue()
    found = []
    for model in catalogue.models:
        for artifact in model.artifacts:
            bundled = artifact.bundled_path is not None
            try:
                path = artifact_path(artifact, config, repo_root=repo_root)
                if bundled:
                    check_artifact_integrity(path, artifact)
                elif not path.is_file():
                    raise ArtifactIntegrityError("missing")
            except ModelPathError:
                found.append(LocalModelArtifact(artifact, None, bundled, False, issue="unsafe_path"))
            except (ArtifactIntegrityError, OSError) as exc:
                code = exc.code if isinstance(exc, ArtifactIntegrityError) else "unreadable"
                found.append(LocalModelArtifact(artifact, path, bundled, False, issue=code))
            else:
                found.append(LocalModelArtifact(artifact, path, bundled, True, bundled))
    return tuple(found)
