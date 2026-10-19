"""Private interpreter entry point for the bounded Frigate probe."""

import json
import os
import signal
import sys
from pathlib import Path

# Run by absolute filename, retaining the caller's cwd so relative paths
# have exactly the same meaning as for the production client.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config_validation import validate_frigate_connection_config
from app.frigate_client import FrigateClient


def main():
    # WAMF's Flask worker can be stopped with SIGTERM. On Linux, ensure its
    # temporary probe dies too, even when Python finally blocks cannot run.
    parent_pid = int(sys.argv[1])
    if os.getppid() != parent_pid:
        return "internal_error"
    if sys.platform == "linux":
        import ctypes

        if ctypes.CDLL(None, use_errno=True).prctl(1, signal.SIGKILL, 0, 0, 0) != 0:
            return "internal_error"
        if os.getppid() != parent_pid:
            return "internal_error"
    try:
        payload = json.load(sys.stdin)
        base_url = payload["base_url"]
        if not validate_frigate_connection_config({"frigate": {"frigate_url": base_url}}).is_ready:
            return "configuration_error"
        return FrigateClient(base_url).check_api_connection()
    except Exception:
        return "internal_error"


if __name__ == "__main__":
    print(main())
