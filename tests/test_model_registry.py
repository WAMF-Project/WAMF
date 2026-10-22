"""Local storage, installed discovery and canonical selection compatibility."""

from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from app.config_forms import build_settings_candidate
from app.config_migration import migrate_config
from app.config_validation import validate_config
from app.model_catalogue import ModelCatalogueError, load_catalogue
from app.model_registry import (
    ModelSelectionError, artifact_path, discover_local_models, resolve_classifier_model,
)
import wamf_paths


@pytest.fixture
def catalogue():
    return load_catalogue()


def managed_config():
    return {"classification": {"model_id": "bird", "model_version": "bundled-1", "threshold": 0.7}}


def test_native_and_docker_model_storage(monkeypatch, tmp_path):
    assert wamf_paths.get_models_path({}) == wamf_paths.REPO_ROOT / "data/models"
    assert wamf_paths.get_models_path({"storage": {"database_path": "/elsewhere/data.db"}}) == wamf_paths.REPO_ROOT / "data/models"
    monkeypatch.setattr(wamf_paths, "REPO_ROOT", Path("/app"))
    assert wamf_paths.get_models_path({}) == Path("/app/data/models")
    config = {"storage": {"models_path": str(tmp_path / "models")}}
    assert wamf_paths.get_model_id_path("bird", config) == tmp_path / "models/bird"
    assert wamf_paths.get_model_version_path("bird", "2", config) == tmp_path / "models/bird/2"
    assert wamf_paths.get_model_artifact_path("bird", "2", "bird.tflite", config) == tmp_path / "models/bird/2/bird.tflite"
    assert not (tmp_path / "models").exists()


def test_storage_setup_creates_configured_model_root(tmp_path):
    config = {"storage": {"models_path": str(tmp_path / "models"), "database_path": str(tmp_path / "db/wamf.db")},
              "media": {"snapshots_path": str(tmp_path / "snapshots"), "clips_path": str(tmp_path / "clips")}}
    paths = wamf_paths.ensure_storage_paths(config)
    assert tmp_path / "models" in paths
    assert all(path.is_dir() for path in paths)
    assert wamf_paths.get_models_path({"storage": {"models_path": "custom/models"}}) == wamf_paths.REPO_ROOT / "custom/models"


@pytest.mark.parametrize("parts", [("../bird", "1", "model.tflite"), ("bird", "../1", "model.tflite"),
                                   ("bird", "1", "../model.tflite"), ("bird", "1", "/model.tflite"),
                                   ("bird", "1", "a\\b"), ("bird", ".", "model.tflite")])
def test_managed_path_rejects_traversal(parts, tmp_path):
    with pytest.raises(ModelCatalogueError):
        wamf_paths.get_model_artifact_path(*parts, {"storage": {"models_path": str(tmp_path)}})


@pytest.mark.parametrize("level", ["id", "version", "file"])
def test_managed_paths_reject_symlink_escape(tmp_path, level):
    root = tmp_path / "models"
    outside = tmp_path / "outside"
    outside.mkdir()
    root.mkdir()
    if level == "id":
        (root / "bird").symlink_to(outside, target_is_directory=True)
    else:
        (root / "bird").mkdir()
        if level == "version":
            (root / "bird/1").symlink_to(outside, target_is_directory=True)
        else:
            (root / "bird/1").mkdir()
            (root / "bird/1/model.tflite").symlink_to(outside / "model.tflite")
    with pytest.raises(wamf_paths.ModelPathError):
        wamf_paths.get_model_artifact_path("bird", "1", "model.tflite", {"storage": {"models_path": str(root)}})
    with pytest.raises(wamf_paths.ModelPathError):
        wamf_paths.get_bundled_model_path("bird/1/model.tflite", repo_root=root)


def test_bundled_model_is_recognized_by_size_and_hash(catalogue):
    local, = discover_local_models(catalogue, {})
    assert local.installed and local.bundled and local.integrity_verified
    assert local.path == wamf_paths.REPO_ROOT / "model.tflite"
    assert local.issue is None
    # Identity verification does not claim interpreter validation or activation.
    assert not hasattr(local, "active")


@pytest.mark.parametrize("contents", [b"changed", None])
def test_modified_or_missing_bundled_file_is_not_official(catalogue, tmp_path, contents):
    if contents is not None:
        (tmp_path / "model.tflite").write_bytes(contents)
    local, = discover_local_models(catalogue, {}, repo_root=tmp_path)
    assert not local.installed and not local.integrity_verified
    assert local.issue in {"size_mismatch", "missing"}
    if contents is not None:
        assert (tmp_path / "model.tflite").read_bytes() == contents
    selection = resolve_classifier_model({"classification": {"model": "model.tflite"}}, repo_root=tmp_path)
    assert not selection.managed


def test_same_size_wrong_hash_is_not_official(catalogue, tmp_path):
    (tmp_path / "model.tflite").write_bytes(b"x" * catalogue.default_artifact.size_bytes)
    local, = discover_local_models(catalogue, {}, repo_root=tmp_path)
    assert not local.installed
    assert local.issue == "checksum_mismatch"


def test_downloaded_discovery_requires_final_file_and_does_not_validate(catalogue, tmp_path):
    artifact = replace(catalogue.default_artifact, bundled_path=None, version="2")
    model = replace(catalogue.models[0], artifacts=(artifact,))
    catalogue = replace(catalogue, models=(model,), default_artifact=artifact)
    config = {"storage": {"models_path": str(tmp_path)}}
    path = artifact_path(artifact, config)
    path.parent.mkdir(parents=True)
    (path.parent / ".model.part").write_bytes(b"partial")
    (path.parent / "other.tflite").write_bytes(b"other")
    assert not discover_local_models(catalogue, config)[0].installed
    path.write_bytes(b"not a valid model")
    local, = discover_local_models(catalogue, config)
    assert local.installed and not local.integrity_verified and not local.bundled
    assert local.path == path


def test_legacy_resolution_preserves_root_absolute_and_home(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    for value, expected in [("models/manual.tflite", wamf_paths.REPO_ROOT / "models/manual.tflite"),
                            ("/external/custom.tflite", Path("/external/custom.tflite")),
                            ("~/custom.tflite", tmp_path / "home/custom.tflite")]:
        selection = resolve_classifier_model({"classification": {"model": value}})
        assert selection.path == expected
        assert selection.artifact is None


def test_managed_resolution_and_migration_preserve_identity(catalogue):
    config = managed_config()
    migrated = migrate_config(config).config
    assert migrated["classification"] == config["classification"]
    selection = resolve_classifier_model(migrated, catalogue)
    assert selection.managed and selection.artifact == catalogue.default_artifact
    assert selection.path == wamf_paths.REPO_ROOT / "model.tflite"
    validation = validate_config(migrated, catalogue=catalogue)
    assert not any(issue.field.startswith("classification") for issue in validation.errors + validation.readiness_issues)


@pytest.mark.parametrize("selection,field", [
    ({"model_id": "missing", "model_version": "1"}, "model_id"),
    ({"model_id": "bird", "model_version": "missing"}, "model_version"),
    ({"model_id": "bird"}, "model_version"),
    ({"model_version": "bundled-1"}, "model_id"),
    ({"model_id": None, "model_version": "bundled-1"}, "model_id"),
    ({"model_id": "bird", "model_version": ""}, "model_version"),
    ({"model_id": "bird", "model_version": "bundled-1", "model": "model.tflite"}, "model"),
    ({"model_id": "bird", "model_version": "bundled-1", "model": None}, "model"),
])
def test_invalid_selection_has_no_fallback(catalogue, selection, field):
    config = {"classification": {**selection, "threshold": 0.7}}
    with pytest.raises(ModelSelectionError) as error:
        resolve_classifier_model(config, catalogue)
    assert error.value.field == f"classification.{field}"
    result = validate_config(config, catalogue=catalogue)
    assert any(issue.field == error.value.field for issue in result.errors)


def test_partial_settings_preserve_managed_selection():
    config = managed_config()
    for form in ({"classification_threshold": "0.8"},
                 {"classification_threshold": "0.8", "classification_model": "model.tflite"}):
        candidate = build_settings_candidate(config, form)
        assert candidate["classification"] == {"model_id": "bird", "model_version": "bundled-1", "threshold": 0.8}
    custom = build_settings_candidate(config, {"classification_model": "custom.tflite"})
    assert custom["classification"] == {"model": "custom.tflite", "threshold": 0.7}
    legacy = {"classification": {"model": "custom.tflite", "threshold": 0.7}}
    assert build_settings_candidate(legacy, {"webui_port": "7767"})["classification"] == legacy["classification"]


@pytest.mark.parametrize("value", [None, True, [], ""])
def test_model_root_config_validation(value):
    result = validate_config({"storage": {"models_path": value}})
    assert any(issue.field == "storage.models_path" for issue in result.errors)


def test_managed_config_uses_existing_persistence_boundary(tmp_path, monkeypatch):
    from app.config_editor import write_config_preserving_admin
    from app.config_loader import load_runtime_config

    path = tmp_path / "config.yml"
    legacy = {"config_version": 2, "classification": {"model": "model.tflite", "threshold": 0.7}}
    path.write_text(yaml.safe_dump(legacy))
    monkeypatch.setenv("WHOSATMYFEEDER_CONFIG", str(path))
    # Merely loading/migrating a legacy selection never rewrites it.
    assert load_runtime_config()["classification"] == legacy["classification"]
    assert yaml.safe_load(path.read_text()) == legacy
    managed = {"config_version": 2, **managed_config()}
    write_config_preserving_admin(yaml.safe_dump(managed))
    loaded = load_runtime_config()
    assert loaded["classification"] == managed["classification"]
    assert resolve_classifier_model(loaded).managed


def test_missing_catalogue_cannot_fall_back_to_legacy(monkeypatch):
    def fail():
        raise ModelCatalogueError("private detail")
    monkeypatch.setattr("app.model_registry.load_catalogue", fail)
    result = validate_config(managed_config())
    assert any(issue.field == "classification.model_id" for issue in result.errors)
    assert "private detail" not in str(result.errors)


def test_symlink_escape_is_reported_in_discovery(catalogue, tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / "model.tflite").symlink_to(wamf_paths.REPO_ROOT / "model.tflite")
    local, = discover_local_models(catalogue, {}, repo_root=root)
    assert not local.installed and local.path is None and local.issue == "unsafe_path"
