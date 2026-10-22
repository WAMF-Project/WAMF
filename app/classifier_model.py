"""Classifier preparation without live globals, plus the Kingfisher contract.

wamf-bird-v1 means RGB, direct 224x224 resize, raw uint8 input and scalar
quantized class probabilities. It does not imply accuracy or wildlife support.
Legacy preparation intentionally retains the historical initialization checks.
"""

from dataclasses import dataclass
import math
from pathlib import Path
import zipfile

import numpy as np
from ai_edge_litert.interpreter import Interpreter

from app.model_catalogue import BIRD_CONTRACT


LEGACY_BACKGROUND_INDEX = 964
MAX_LABEL_BYTES = 1024 * 1024
DISPLAY_LABELS = "probability-labels-en.txt"
CATEGORY_LABELS = "probability-labels.txt"


class ModelValidationError(ValueError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class PreparedClassifier:
    interpreter: object
    input_details: dict
    output_details: dict
    display_labels: tuple[str, ...]
    category_labels: tuple[str, ...]
    background_index: int


def _quantization(details):
    scale, zero = details["quantization"]
    parameters = details.get("quantization_parameters", {})
    scales = np.asarray(parameters.get("scales", []))
    zeros = np.asarray(parameters.get("zero_points", []))
    limits = np.iinfo(details["dtype"])
    if (
        not math.isfinite(scale) or scale <= 0
        or isinstance(zero, (bool, np.bool_)) or not isinstance(zero, (int, np.integer))
        or not limits.min <= zero <= limits.max
        or scales.shape != (1,) or zeros.shape != (1,)
        or not np.isfinite(scales[0]) or scales[0] != scale or zeros[0] != zero
    ):
        raise ModelValidationError("invalid_quantization")
    return scale, zero


def _managed_tensors(inputs, outputs, artifact):
    if len(inputs) != 1 or len(outputs) != 1:
        raise ModelValidationError("tensor_count")
    input_details, output_details = inputs[0], outputs[0]
    if input_details["dtype"] != np.uint8 or tuple(input_details["shape"]) != (1, 224, 224, 3):
        raise ModelValidationError("input_contract")
    if output_details["dtype"] != np.uint8 or tuple(output_details["shape"]) != (1, artifact.class_count):
        raise ModelValidationError("output_contract")
    for details in (input_details, output_details):
        if tuple(details.get("shape_signature", details["shape"])) != tuple(details["shape"]):
            raise ModelValidationError("dynamic_tensors")
        _quantization(details)
    # This contract preserves the bundled model's raw-byte preprocessing.
    if tuple(input_details["quantization"]) != (0.0078125, 128):
        raise ModelValidationError("input_quantization")


def _read_labels(model_path, managed):
    with zipfile.ZipFile(model_path) as model:
        labels = []
        for name in (DISPLAY_LABELS, CATEGORY_LABELS):
            if managed:
                members = [info for info in model.infolist() if info.filename == name]
                if len(members) != 1 or members[0].file_size > MAX_LABEL_BYTES:
                    raise ModelValidationError("invalid_labels")
                with model.open(members[0]) as member:
                    content = member.read(MAX_LABEL_BYTES + 1)
                if len(content) > MAX_LABEL_BYTES:
                    raise ModelValidationError("invalid_labels")
            else:
                content = model.read(name)
            labels.append(tuple(content.decode("utf-8").splitlines()))
        return labels


def prepare_classifier(model_path, *, artifact=None, interpreter_factory=None):
    """Prepare privately; callers publish state only after complete success.

    Passing a catalogue artifact enables stricter managed-model checks. This
    helper does not verify artifact identity or invoke inference; the timed
    validation boundary does both before a managed artifact is used.
    """
    if artifact is not None and artifact.contract != BIRD_CONTRACT:
        raise ModelValidationError("unsupported_contract")
    factory = interpreter_factory if interpreter_factory is not None else Interpreter
    try:
        interpreter = factory(model_path=str(Path(model_path)), num_threads=4)
        interpreter.allocate_tensors()
    except (ValueError, RuntimeError, OSError) as exc:
        if artifact is not None:
            raise ModelValidationError("interpreter_failed") from exc
        raise
    inputs = interpreter.get_input_details()
    outputs = interpreter.get_output_details()
    if artifact is not None:
        _managed_tensors(inputs, outputs, artifact)
    input_details, output_details = inputs[0], outputs[0]
    if input_details["dtype"] != np.uint8:
        raise ValueError("Classifier input tensor must use uint8 data")
    if tuple(input_details["shape"]) != (1, 224, 224, 3):
        raise ValueError("Classifier input tensor must have shape (1, 224, 224, 3)")
    try:
        display, categories = _read_labels(model_path, artifact is not None)
    except (KeyError, UnicodeError, zipfile.BadZipFile, OSError, RuntimeError) as exc:
        if artifact is not None:
            raise ModelValidationError("invalid_labels") from exc
        raise
    output_size = int(np.prod(output_details["shape"]))
    if len(display) != output_size or len(categories) != output_size:
        if artifact is not None:
            raise ModelValidationError("label_count")
        raise ValueError("Classifier labels do not match the output tensor size")
    background_index = LEGACY_BACKGROUND_INDEX
    if artifact is not None:
        if any(not label.strip() for labels in (display, categories) for label in labels):
            raise ModelValidationError("invalid_labels")
        background = artifact.background
        if (not 0 <= background.index < output_size
                or display[background.index] != background.display_label
                or categories[background.index] != background.category_label):
            raise ModelValidationError("background_mismatch")
        background_index = background.index
    return PreparedClassifier(interpreter, input_details, output_details, display, categories, background_index)


def smoke_check(prepared):
    """One deterministic synthetic invocation, not an accuracy measurement.

    Run only through the timed validation worker when inspecting artifacts.
    """
    interpreter = prepared.interpreter
    try:
        pixels = np.arange(224 * 224 * 3, dtype=np.uint32).astype(np.uint8).reshape(1, 224, 224, 3)
        interpreter.set_tensor(prepared.input_details["index"], pixels)
        interpreter.invoke()
        raw = interpreter.get_tensor(prepared.output_details["index"])
        if raw.dtype != np.uint8 or tuple(raw.shape) != tuple(prepared.output_details["shape"]):
            raise ModelValidationError("smoke_failed")
        scale, zero = prepared.output_details["quantization"]
        scores = (raw.astype(np.float32) - zero) * scale
        if not np.isfinite(scores).all():
            raise ModelValidationError("smoke_failed")
    except (ValueError, RuntimeError, OSError) as exc:
        raise ModelValidationError("smoke_failed") from exc
