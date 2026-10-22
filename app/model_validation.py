"""Size/hash/containment and bounded, isolated LiteRT model validation."""

from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import subprocess
import sys

from app.model_catalogue import BIRD_CONTRACT, REPO_ROOT
from app.model_registry import ArtifactIntegrityError, check_artifact_integrity


VALIDATION_TIMEOUT_SECONDS = 15.0
MESSAGES = {
    "valid": "Artifact identity and classifier contract validated; accuracy was not measured.",
    "missing": "Model file is missing.",
    "not_regular_file": "Model is not a regular file.",
    "unreadable": "Model file could not be read.",
    "unsafe_path": "Model path is outside its allowed storage root.",
    "size_mismatch": "Model size does not match the catalogue.",
    "checksum_mismatch": "Model checksum does not match the catalogue.",
    "unsupported_contract": "Model compatibility contract is unsupported.",
    "interpreter_failed": "LiteRT could not initialize the model.",
    "tensor_count": "Model must have exactly one input and one output.",
    "input_contract": "Model input must be uint8 with shape [1,224,224,3].",
    "output_contract": "Model output must be one uint8 class vector.",
    "dynamic_tensors": "Dynamic tensor shapes are unsupported.",
    "invalid_quantization": "Model scalar quantization is invalid or unsupported.",
    "input_quantization": "Model input quantization does not match raw-byte preprocessing.",
    "invalid_labels": "Embedded label files are missing, malformed or too large.",
    "label_count": "Embedded label counts do not match the output classes.",
    "background_mismatch": "Embedded background labels do not match the catalogue.",
    "smoke_failed": "Synthetic inference could not produce usable finite scores.",
    "timeout": "Model validation exceeded its time limit.",
    "worker_failed": "Isolated model validation could not complete.",
}


@dataclass(frozen=True)
class ModelValidationResult:
    valid: bool
    code: str
    message: str


def _result(code):
    return ModelValidationResult(code == "valid", code, MESSAGES[code])


def validate_model_artifact(path, artifact, *, allowed_root=None, timeout_seconds=VALIDATION_TIMEOUT_SECONDS):
    """Validate locally, without changing files, runtime globals or selection.

    A caller validating staged/override storage must supply its owned root.
    No interpreter is created in the calling (possibly Flask) process.
    """
    from wamf_paths import get_models_path

    if (
        not isinstance(timeout_seconds, (int, float)) or isinstance(timeout_seconds, bool)
        or not math.isfinite(timeout_seconds) or timeout_seconds <= 0
    ):
        raise ValueError("Validation timeout must be finite and positive")
    if artifact.contract != BIRD_CONTRACT:
        return _result("unsupported_contract")
    root = allowed_root
    if root is None:
        root = REPO_ROOT if artifact.bundled_path else get_models_path({})
    try:
        candidate = Path(path).resolve()
        root = Path(root).resolve()
        if candidate == root or root not in candidate.parents:
            return _result("unsafe_path")
    except (OSError, RuntimeError):
        return _result("unsafe_path")
    try:
        check_artifact_integrity(candidate, artifact)
    except ArtifactIntegrityError as exc:
        return _result(exc.code)
    payload = {
        "path": str(candidate), "model_id": artifact.model_id, "version": artifact.version,
        "class_count": artifact.class_count, "contract": artifact.contract,
        "background": {
            "index": artifact.background.index,
            "display_label": artifact.background.display_label,
            "category_label": artifact.background.category_label,
        },
    }
    try:
        worker = Path(__file__).with_name("model_validation_worker.py")
        completed = subprocess.run(
            [sys.executable, str(worker), str(os.getpid())], input=json.dumps(payload),
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
            timeout=timeout_seconds, check=False,
        )
    except subprocess.TimeoutExpired:
        return _result("timeout")
    except OSError:
        return _result("worker_failed")
    code = completed.stdout.strip()
    if completed.returncode != 0 or code not in MESSAGES:
        return _result("worker_failed")
    return _result(code)
