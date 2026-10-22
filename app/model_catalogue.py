"""Release-owned classifier catalogue. Parsing never inspects or installs models."""

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
import re
from urllib.parse import urlsplit

import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent
CATALOGUE_PATH = REPO_ROOT / "models" / "catalog.yml"
BIRD_CONTRACT = "wamf-bird-v1"


class ModelCatalogueError(ValueError):
    """A deterministic catalogue schema or identifier error."""


def safe_component(value, field="component"):
    """Validate one portable path component, without coercion."""
    if not isinstance(value, str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._-]{0,159}", value
    ):
        raise ModelCatalogueError(f"{field}: must be a safe path component")
    return value


def safe_identity(value, field):
    safe_component(value, field)
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,79}", value):
        raise ModelCatalogueError(f"{field}: must be a lowercase model ID or version")
    return value


def safe_bundled_path(value):
    if not isinstance(value, str) or not value or "\\" in value:
        raise ModelCatalogueError("bundled_path: must be a safe relative path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in ("", ".", "..") for part in value.split("/")):
        raise ModelCatalogueError("bundled_path: must be a safe relative path")
    for part in path.parts:
        safe_component(part, "bundled_path")
    return value


@dataclass(frozen=True)
class BackgroundClass:
    index: int
    display_label: str
    category_label: str


@dataclass(frozen=True)
class ModelArtifact:
    model_id: str
    version: str
    filename: str
    sha256: str
    size_bytes: int
    format: str
    runtime: str
    contract: str
    class_count: int
    background: BackgroundClass
    source: str
    licence: str
    bundled_path: str | None = None
    download_url: str | None = None
    notes: str | None = None


@dataclass(frozen=True)
class SupportedModel:
    id: str
    name: str
    description: str
    artifacts: tuple[ModelArtifact, ...]


@dataclass(frozen=True)
class ModelCatalogue:
    schema_version: int
    models: tuple[SupportedModel, ...]
    default_artifact: ModelArtifact

    def get_artifact(self, model_id: str, version: str) -> ModelArtifact | None:
        return next(
            (artifact for model in self.models if model.id == model_id
             for artifact in model.artifacts if artifact.version == version),
            None,
        )


def _mapping(value, field, required, optional=()):
    if not isinstance(value, dict):
        raise ModelCatalogueError(f"{field}: must be a mapping")
    missing = sorted(set(required) - value.keys())
    if missing:
        raise ModelCatalogueError(f"{field}: missing {missing[0]}")
    unknown = set(value) - set(required) - set(optional)
    if unknown:
        raise ModelCatalogueError(f"{field}: contains unknown fields")
    return value


def _text(value, field):
    if not isinstance(value, str) or not value.strip():
        raise ModelCatalogueError(f"{field}: must be a nonempty string")
    return value


def _positive_int(value, field):
    if type(value) is not int or value <= 0:
        raise ModelCatalogueError(f"{field}: must be a positive integer")
    return value


def _artifact(value, model_id):
    data = _mapping(value, "artifact", (
        "version", "filename", "sha256", "size_bytes", "format", "runtime",
        "contract", "class_count", "background", "source", "licence",
    ), ("bundled_path", "download_url", "notes"))
    safe_identity(data["version"], "artifact.version")
    safe_component(data["filename"], "artifact.filename")
    if not isinstance(data["sha256"], str) or not re.fullmatch(
        r"[0-9a-f]{64}", data["sha256"]
    ):
        raise ModelCatalogueError("artifact.sha256: must be 64 lowercase hexadecimal characters")
    _positive_int(data["size_bytes"], "artifact.size_bytes")
    for key, expected in (
        ("format", "tflite"), ("runtime", "litert"), ("contract", BIRD_CONTRACT)
    ):
        if data[key] != expected:
            raise ModelCatalogueError(f"artifact.{key}: unsupported value")
    count = _positive_int(data["class_count"], "artifact.class_count")
    background = _mapping(
        data["background"], "background", ("index", "display_label", "category_label")
    )
    if type(background["index"]) is not int or not 0 <= background["index"] < count:
        raise ModelCatalogueError("background.index: must be within the class count")
    _text(background["display_label"], "background.display_label")
    _text(background["category_label"], "background.category_label")
    for key in ("source", "licence", "notes"):
        if key in data:
            _text(data[key], f"artifact.{key}")
    if "bundled_path" in data:
        safe_bundled_path(data["bundled_path"])
    if "download_url" in data:
        url = _text(data["download_url"], "artifact.download_url")
        try:
            parsed = urlsplit(url)
            valid = (
                parsed.scheme == "https" and parsed.hostname and not parsed.username
                and not parsed.password and not parsed.fragment
                and not any(c.isspace() for c in url)
            )
            parsed.port
        except ValueError:
            valid = False
        if not valid:
            raise ModelCatalogueError("artifact.download_url: must be an HTTPS artifact URL without credentials")
    return ModelArtifact(model_id=model_id, **{**data, "background": BackgroundClass(**background)})


def parse_catalogue(data):
    data = _mapping(data, "catalogue", ("schema_version", "default", "models"))
    if type(data["schema_version"]) is not int or data["schema_version"] != 1:
        raise ModelCatalogueError("schema_version: unsupported catalogue version")
    default = _mapping(data["default"], "default", ("model_id", "version"))
    safe_identity(default["model_id"], "default.model_id")
    safe_identity(default["version"], "default.version")
    if not isinstance(data["models"], list) or not data["models"]:
        raise ModelCatalogueError("models: must be a nonempty list")
    models = []
    seen = set()
    for value in data["models"]:
        model = _mapping(value, "model", ("id", "name", "description", "artifacts"))
        model_id = safe_identity(model["id"], "model.id")
        if model_id in seen:
            raise ModelCatalogueError("model.id: duplicate model ID")
        seen.add(model_id)
        _text(model["name"], "model.name")
        _text(model["description"], "model.description")
        if not isinstance(model["artifacts"], list) or not model["artifacts"]:
            raise ModelCatalogueError("model.artifacts: must be a nonempty list")
        artifacts = tuple(_artifact(item, model_id) for item in model["artifacts"])
        if len({artifact.version for artifact in artifacts}) != len(artifacts):
            raise ModelCatalogueError("artifact.version: duplicate artifact version")
        models.append(SupportedModel(model_id, model["name"], model["description"], artifacts))
    default_artifact = next(
        (artifact for model in models for artifact in model.artifacts
         if (artifact.model_id, artifact.version) == (default["model_id"], default["version"])),
        None,
    )
    if default_artifact is None:
        raise ModelCatalogueError("default: must reference one supported artifact")
    return ModelCatalogue(1, tuple(models), default_artifact)


class _CatalogueLoader(yaml.SafeLoader):
    """Reject duplicate YAML fields rather than silently replacing metadata."""


def _unique_mapping(loader, node):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node)
        if not isinstance(key, str) or key in result:
            raise ModelCatalogueError("catalogue: duplicate or non-string YAML field")
        result[key] = loader.construct_object(value_node)
    return result


_CatalogueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _unique_mapping)


def load_catalogue(path=None):
    """Read only release metadata; no filesystem creation or network access."""
    try:
        data = yaml.load(
            Path(path or CATALOGUE_PATH).read_text(encoding="utf-8"), Loader=_CatalogueLoader
        )
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ModelCatalogueError("catalogue: could not load YAML") from exc
    return parse_catalogue(data)
