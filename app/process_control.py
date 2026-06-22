"""Process lifecycle helpers used by the admin interface."""

import os
import signal
import threading
import time


def schedule_restart(delay=1.0):
    """Stop WAMF after the current HTTP response has had time to complete.

    The web UI is normally a child of the main WAMF process. Stopping that
    parent ends the container, allowing Docker's restart policy to start the
    complete application again rather than only replacing the web process.
    """
    supervisor_pid = os.getppid()

    def stop_supervisor():
        time.sleep(delay)
        os.kill(supervisor_pid, signal.SIGTERM)

    threading.Thread(
        target=stop_supervisor,
        name="wamf-restart",
        daemon=True,
    ).start()

