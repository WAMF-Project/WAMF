"""Release catalogue schema and bundled artifact identity."""

from copy import deepcopy
from dataclasses import FrozenInstanceError

import pytest
import yaml

from app.model_catalogue import CATALOGUE_PATH, ModelCatalogueError, load_catalogue, parse_catalogue


@pytest.fixture
def catalogue_data():
    return yaml.safe_load(CATALOGUE_PATH.read_text())


def test_release_catalogue_is_frozen_and_has_one_explicit_default(catalogue_data):
    before = deepcopy(catalogue_data)
    catalogue = parse_catalogue(catalogue_data)
    artifact = catalogue.default_artifact
    assert catalogue.schema_version == 1
    assert (artifact.model_id, artifact.version) == ("bird", "bundled-1")
    assert artifact == catalogue.get_artifact("bird", "bundled-1")
    assert artifact.background.index == 964
    assert artifact.background.display_label == "None"
    assert artifact.size_bytes == 3561598
    assert artifact.sha256 == "95f59df4cff9053355abcd0b6e5e7e740f6eae19ed8cded32e2c3e8068195093"
    assert "Unverified" in artifact.licence
    assert catalogue.get_artifact("unknown", "1") is None
    with pytest.raises(FrozenInstanceError):
        artifact.version = "new"
    assert catalogue_data == before


@pytest.mark.parametrize("version", [0, 2, "1", True, None])
def test_unsupported_schema(catalogue_data, version):
    catalogue_data["schema_version"] = version
    with pytest.raises(ModelCatalogueError, match="schema_version"):
        parse_catalogue(catalogue_data)


def test_duplicate_ids_and_versions(catalogue_data):
    catalogue_data["models"].append(deepcopy(catalogue_data["models"][0]))
    with pytest.raises(ModelCatalogueError, match="duplicate model ID"):
        parse_catalogue(catalogue_data)
    catalogue_data["models"].pop()
    artifacts = catalogue_data["models"][0]["artifacts"]
    artifacts.append(deepcopy(artifacts[0]))
    with pytest.raises(ModelCatalogueError, match="duplicate artifact version"):
        parse_catalogue(catalogue_data)


@pytest.mark.parametrize("default", [None, [], [{"model_id": "bird", "version": "bundled-1"}] * 2,
                                        {}, {"model_id": "absent", "version": "1"}])
def test_missing_multiple_or_unknown_default(catalogue_data, default):
    catalogue_data["default"] = default
    with pytest.raises(ModelCatalogueError, match="default"):
        parse_catalogue(catalogue_data)


@pytest.mark.parametrize("field,value", [
    ("version", "../1"), ("version", "1/2"), ("version", True),
    ("filename", "/model.tflite"), ("filename", "../model.tflite"),
    ("filename", "sub/model.tflite"), ("filename", "C:\\model.tflite"),
    ("filename", "."), ("filename", ".."), ("filename", "x\n.tflite"),
    ("sha256", "x" * 64), ("sha256", "A" * 64), ("sha256", 123),
    ("size_bytes", 0), ("size_bytes", -1), ("size_bytes", True), ("size_bytes", "123"),
    ("format", "onnx"), ("runtime", "unknown"), ("contract", "unknown"),
    ("class_count", 0), ("class_count", True),
    ("bundled_path", "/model.tflite"), ("bundled_path", "../model.tflite"),
    ("bundled_path", "sub/../../model.tflite"), ("bundled_path", "a/./model.tflite"),
    ("bundled_path", "a\\model.tflite"), ("bundled_path", "a//model.tflite"),
    ("download_url", "http://example.com/model.tflite"),
    ("download_url", "https://user:secret@example.com/model.tflite"),
    ("download_url", "https://example.com:bad/model.tflite"),
    ("source", None), ("licence", ""), ("notes", 3),
])
def test_invalid_artifact_fields(catalogue_data, field, value):
    catalogue_data["models"][0]["artifacts"][0][field] = value
    with pytest.raises(ModelCatalogueError):
        parse_catalogue(catalogue_data)


@pytest.mark.parametrize("model_id", ["../bird", "/bird", ".", "..", "Bird", "a\\b", "a/b", 1, ""])
def test_invalid_model_id(catalogue_data, model_id):
    catalogue_data["models"][0]["id"] = model_id
    with pytest.raises(ModelCatalogueError):
        parse_catalogue(catalogue_data)


@pytest.mark.parametrize("field,value", [("index", -1), ("index", 965), ("index", True),
                                        ("display_label", None), ("category_label", "")])
def test_invalid_background(catalogue_data, field, value):
    catalogue_data["models"][0]["artifacts"][0]["background"][field] = value
    with pytest.raises(ModelCatalogueError, match="background"):
        parse_catalogue(catalogue_data)


def test_downloadable_version_is_metadata_only(catalogue_data):
    artifacts = catalogue_data["models"][0]["artifacts"]
    additional = deepcopy(artifacts[0])
    additional.pop("bundled_path")
    additional.update(version="2", download_url="https://example.com/releases/model.tflite")
    artifacts.append(additional)
    catalogue = parse_catalogue(catalogue_data)
    assert catalogue.get_artifact("bird", "2").bundled_path is None
    assert catalogue.default_artifact.version == "bundled-1"


@pytest.mark.parametrize("content", ["[", "null", "models: []", "schema_version: 1\nschema_version: 1\n",
                                      "? [x, y]\n: value\n"])
def test_malformed_yaml_has_catalogue_error(tmp_path, content):
    path = tmp_path / "catalog.yml"
    path.write_text(content)
    with pytest.raises(ModelCatalogueError):
        load_catalogue(path)


def test_missing_field_and_unknown_field_are_rejected(catalogue_data):
    artifact = catalogue_data["models"][0]["artifacts"][0]
    artifact.pop("sha256")
    with pytest.raises(ModelCatalogueError, match="missing sha256"):
        parse_catalogue(catalogue_data)
    artifact["sha256"] = "0" * 64
    artifact["typo"] = True
    with pytest.raises(ModelCatalogueError, match="unknown fields"):
        parse_catalogue(catalogue_data)
