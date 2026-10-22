"""Explicit one-shot model install worker, with no application startup/imports."""

from dataclasses import asdict
import json
import os
from pathlib import Path
import signal
import sys

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _owned_by(parent_pid):
    if os.getppid() != parent_pid:
        return False
    if sys.platform == "linux":
        import ctypes

        if ctypes.CDLL(None, use_errno=True).prctl(1, signal.SIGKILL, 0, 0, 0) != 0:
            return False
    return os.getppid() == parent_pid


def main():
    if not _owned_by(int(sys.argv[1])):
        return
    from app.model_download import run_install
    from app.model_install import artifact_from_payload, install_result

    def emit(phase, received):
        print(json.dumps({"phase": phase, "bytes_received": received}), flush=True)

    try:
        payload = json.load(sys.stdin)
        artifact = artifact_from_payload(payload["artifact"])
        result = run_install(artifact, payload["root"], payload["protected_paths"], emit)
    except Exception:
        # Native/library details, paths and signed URLs are never serialized.
        result = install_result("internal_error")
    data = asdict(result)
    print(json.dumps({"result": data["code"], "validation_code": data["validation_code"],
                      "durability_warning": data["durability_warning"]}), flush=True)


if __name__ == "__main__":
    main()
