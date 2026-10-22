"""One-shot model inspection worker, owned by its invoking WAMF process."""

import json
import os
from pathlib import Path
import signal
import sys
from types import SimpleNamespace

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main():
    parent_pid = int(sys.argv[1])
    if os.getppid() != parent_pid:
        return "worker_failed"
    if sys.platform == "linux":
        import ctypes

        if ctypes.CDLL(None, use_errno=True).prctl(1, signal.SIGKILL, 0, 0, 0) != 0:
            return "worker_failed"
        if os.getppid() != parent_pid:
            return "worker_failed"

    # Import LiteRT only after arranging cleanup if the owning process exits.
    from app.classifier_model import ModelValidationError, prepare_classifier, smoke_check

    payload = json.load(sys.stdin)
    path = payload.pop("path")
    payload["background"] = SimpleNamespace(**payload["background"])
    try:
        prepared = prepare_classifier(path, artifact=SimpleNamespace(**payload))
        smoke_check(prepared)
    except ModelValidationError as exc:
        return exc.code
    return "valid"


if __name__ == "__main__":
    print(main())
