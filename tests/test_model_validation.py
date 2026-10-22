"""Classifier preparation and isolated managed validation, with no live services."""

from dataclasses import replace
import hashlib
import os
from pathlib import Path
import subprocess
from unittest.mock import Mock
import zipfile

import numpy as np
import pytest

from app.classifier_model import (
    CATEGORY_LABELS, DISPLAY_LABELS, MAX_LABEL_BYTES, ModelValidationError,
    prepare_classifier, smoke_check,
)
from app.model_catalogue import BackgroundClass, load_catalogue
from app.model_validation import validate_model_artifact


class ContractInterpreter:
    """Three class fake with the same tensor contract as the supported runtime."""

    def __init__(self, *, model_path, num_threads):
        self.input = self.details(0, [1, 224, 224, 3], 0.0078125, 128)
        self.output = self.details(1, [1, 3], 0.00390625, 0)
        self.inputs = [self.input]
        self.outputs = [self.output]
        self.scores = np.array([[32, 128, 96]], dtype=np.uint8)
        self.invocations = 0

    @staticmethod
    def details(index, shape, scale, zero):
        return {"index": index, "shape": np.array(shape), "dtype": np.uint8,
                "quantization": (scale, zero),
                "quantization_parameters": {"scales": np.array([scale]), "zero_points": np.array([zero])}}

    def allocate_tensors(self):
        pass

    def get_input_details(self):
        return self.inputs

    def get_output_details(self):
        return self.outputs

    def set_tensor(self, index, value):
        assert index == 0
        assert value.shape == (1, 224, 224, 3) and value.dtype == np.uint8
        assert value.flags.c_contiguous
        self.pixels = value.copy()

    def invoke(self):
        self.invocations += 1

    def get_tensor(self, index):
        assert index == 1
        return self.scores


def write_labels(path, display=b"Species a\nSpecies b\nNone\n", categories=b"/a\n/b\n__background__\n"):
    with zipfile.ZipFile(path, "w") as archive:
        if display is not None:
            archive.writestr(DISPLAY_LABELS, display)
        if categories is not None:
            archive.writestr(CATEGORY_LABELS, categories)


@pytest.fixture
def artifact_file(tmp_path):
    path = tmp_path / "model.tflite"
    write_labels(path)
    artifact = replace(load_catalogue().default_artifact, class_count=3,
                       size_bytes=path.stat().st_size, sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                       background=BackgroundClass(2, "None", "__background__"))
    return path, artifact


def prepare_fake(path, artifact, interpreter=None):
    interpreter = interpreter or ContractInterpreter(model_path=str(path), num_threads=4)
    return prepare_classifier(path, artifact=artifact, interpreter_factory=lambda **kwargs: interpreter)


def test_preparation_and_deterministic_smoke_are_independent(artifact_file):
    path, artifact = artifact_file
    prepared = prepare_fake(path, artifact)
    assert prepared.display_labels == ("Species a", "Species b", "None")
    assert prepared.category_labels[-1] == "__background__"
    assert prepared.background_index == 2
    assert prepared.interpreter.invocations == 0
    smoke_check(prepared)
    pixels = prepared.interpreter.pixels.copy()
    smoke_check(prepared)
    assert np.array_equal(pixels, prepared.interpreter.pixels)
    assert prepared.interpreter.invocations == 2


def test_current_bird_model_prepares_and_validates_in_isolation():
    artifact = load_catalogue().default_artifact
    path = Path(__file__).resolve().parent.parent / "model.tflite"
    result = validate_model_artifact(path, artifact)
    assert result.valid and result.code == "valid"
    assert "accuracy was not measured" in result.message


@pytest.mark.parametrize("mutation,code", [
    (lambda i: i.inputs.clear(), "tensor_count"),
    (lambda i: i.inputs.append(i.input), "tensor_count"),
    (lambda i: i.outputs.clear(), "tensor_count"),
    (lambda i: i.outputs.append(i.output), "tensor_count"),
    (lambda i: i.input.update(dtype=np.float32), "input_contract"),
    (lambda i: i.input.update(shape=[1, 224, 224, 1]), "input_contract"),
    (lambda i: i.input.update(shape=[1, 299, 299, 3]), "input_contract"),
    (lambda i: i.output.update(dtype=np.float32), "output_contract"),
    (lambda i: i.output.update(shape=[3]), "output_contract"),
    (lambda i: i.output.update(shape=[1, 4]), "output_contract"),
    (lambda i: i.input.update(shape_signature=[-1, 224, 224, 3]), "dynamic_tensors"),
    (lambda i: i.output.update(quantization=(0, 0)), "invalid_quantization"),
    (lambda i: i.output.update(quantization=(float("nan"), 0)), "invalid_quantization"),
    (lambda i: i.output.update(quantization=(float("inf"), 0)), "invalid_quantization"),
    (lambda i: i.output.update(quantization=(-1, 0)), "invalid_quantization"),
    (lambda i: i.output.update(quantization=(0.00390625, 256)), "invalid_quantization"),
    (lambda i: i.output.update(quantization=(0.00390625, 0.5)), "invalid_quantization"),
    (lambda i: i.output.update(quantization=(0.00390625, True)), "invalid_quantization"),
    (lambda i: i.output.update(quantization_parameters={"scales": [0.1, 0.2], "zero_points": [0, 0]}), "invalid_quantization"),
    (lambda i: i.output.update(quantization_parameters={}), "invalid_quantization"),
    (lambda i: i.output.update(quantization_parameters={"scales": [0.1], "zero_points": [0]}), "invalid_quantization"),
    (lambda i: i.input.update(quantization=(0.00390625, 0), quantization_parameters={"scales": [0.00390625], "zero_points": [0]}), "input_quantization"),
])
def test_incompatible_tensors_and_quantization(artifact_file, mutation, code):
    path, artifact = artifact_file
    interpreter = ContractInterpreter(model_path=str(path), num_threads=4)
    mutation(interpreter)
    with pytest.raises(ModelValidationError) as error:
        prepare_fake(path, artifact, interpreter)
    assert error.value.code == code


@pytest.mark.parametrize("display,categories,code", [
    (None, b"/a\n/b\n__background__", "invalid_labels"),
    (b"Species a\nSpecies b\nNone", None, "invalid_labels"),
    (b"\xff", b"/a\n/b\n__background__", "invalid_labels"),
    (b"Species a", b"/a\n/b\n__background__", "label_count"),
    (b"Species a\n \nNone", b"/a\n/b\n__background__", "invalid_labels"),
    (b"Species a\nSpecies b\nWrong", b"/a\n/b\n__background__", "background_mismatch"),
    (b"Species a\nSpecies b\nNone", b"/a\n/b\nwrong", "background_mismatch"),
    (b"a" * (MAX_LABEL_BYTES + 1), b"/a\n/b\n__background__", "invalid_labels"),
])
def test_invalid_labels(artifact_file, display, categories, code):
    path, artifact = artifact_file
    write_labels(path, display, categories)
    with pytest.raises(ModelValidationError) as error:
        prepare_fake(path, artifact)
    assert error.value.code == code


@pytest.mark.parametrize("phase", ["construct", "allocate"])
def test_interpreter_failure_is_a_model_error(artifact_file, phase):
    path, artifact = artifact_file
    def factory(**kwargs):
        if phase == "construct":
            raise ValueError("private model detail")
        interpreter = ContractInterpreter(**kwargs)
        interpreter.allocate_tensors = Mock(side_effect=RuntimeError("private"))
        return interpreter
    with pytest.raises(ModelValidationError) as error:
        prepare_classifier(path, artifact=artifact, interpreter_factory=factory)
    assert error.value.code == "interpreter_failed"


@pytest.mark.parametrize("failure", ["invoke", "read", "shape", "dtype", "nonfinite"])
def test_smoke_failure(artifact_file, failure):
    path, artifact = artifact_file
    prepared = prepare_fake(path, artifact)
    interpreter = prepared.interpreter
    if failure == "invoke":
        interpreter.invoke = Mock(side_effect=RuntimeError("private"))
    elif failure == "read":
        interpreter.get_tensor = Mock(side_effect=ValueError("private"))
    elif failure == "shape":
        interpreter.scores = np.zeros((1, 4), dtype=np.uint8)
    elif failure == "dtype":
        interpreter.scores = np.zeros((1, 3), dtype=np.float32)
    else:
        prepared.output_details["quantization"] = (float("nan"), 0)
    with pytest.raises(ModelValidationError, match="smoke_failed"):
        smoke_check(prepared)


def test_invalid_litert_with_matching_hash_returns_structured_failure(artifact_file):
    path, artifact = artifact_file
    result = validate_model_artifact(path, artifact, allowed_root=path.parent)
    assert not result.valid and result.code == "interpreter_failed"
    assert "private" not in result.message


@pytest.mark.parametrize("case,code", [("missing", "missing"), ("directory", "not_regular_file"),
                                     ("size", "size_mismatch"), ("hash", "checksum_mismatch"),
                                     ("outside", "unsafe_path"), ("symlink", "unsafe_path"),
                                     ("fifo", "not_regular_file")])
def test_identity_failure_prevents_interpreter_process(artifact_file, monkeypatch, tmp_path, case, code):
    path, artifact = artifact_file
    root = path.parent
    if case == "missing":
        path = root / "missing.tflite"
    elif case == "directory":
        path = root / "directory"
        path.mkdir()
    elif case == "size":
        artifact = replace(artifact, size_bytes=1)
    elif case == "hash":
        artifact = replace(artifact, sha256="0" * 64)
    elif case == "outside":
        root = root / "nested"
    elif case == "symlink":
        link = root / "nested"
        link.mkdir()
        (link / "model.tflite").symlink_to(path)
        root, path = link, link / "model.tflite"
    elif case == "fifo":
        path = root / "fifo"
        os.mkfifo(path)
    run = Mock()
    monkeypatch.setattr("app.model_validation.subprocess.run", run)
    result = validate_model_artifact(path, artifact, allowed_root=root)
    assert not result.valid and result.code == code
    run.assert_not_called()


def test_validation_process_is_bounded_and_sanitized(artifact_file, monkeypatch):
    path, artifact = artifact_file
    def expired(*args, **kwargs):
        assert kwargs["timeout"] == 0.1
        raise subprocess.TimeoutExpired(args[0], 0.1)
    monkeypatch.setattr("app.model_validation.subprocess.run", expired)
    assert validate_model_artifact(path, artifact, allowed_root=path.parent, timeout_seconds=0.1).code == "timeout"
    monkeypatch.setattr("app.model_validation.subprocess.run", Mock(return_value=subprocess.CompletedProcess([], 1, "private path")))
    result = validate_model_artifact(path, artifact, allowed_root=path.parent)
    assert result.code == "worker_failed" and "private" not in result.message


@pytest.mark.parametrize("timeout", [0, -1, True, float("nan"), float("inf")])
def test_programmer_timeout_errors_are_not_swallowed(artifact_file, timeout):
    path, artifact = artifact_file
    with pytest.raises(ValueError, match="timeout"):
        validate_model_artifact(path, artifact, timeout_seconds=timeout)
