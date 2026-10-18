"""Private interpreter entry point for the bounded MQTT probe."""

import json
import os
import signal
import sys
from pathlib import Path

# Run by absolute filename, retaining the caller's cwd so relative CA paths
# have exactly the same meaning as for the production client.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.mqtt_connection import _probe_in_worker
from app.mqtt_settings import MqttSettings


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
        return _probe_in_worker(MqttSettings(**payload["settings"]), payload["timeout"])
    except Exception:
        return "internal_error"


if __name__ == "__main__":
    print(main())
