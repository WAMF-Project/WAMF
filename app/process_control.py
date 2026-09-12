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



class WorkerSupervisor:
    """Own worker lifetime, including startup failures and parent-only signals."""

    def __init__(self, process_factory):
        self.process_factory = process_factory
        self.children = []
        self.stopping = False
        self.received_signal = None
        self.owner_pid = os.getpid()
        self.previous_handlers = {}

    def _request_shutdown(self, signum, frame):
        # fork() initially inherits these handlers. A TERM arriving before the
        # worker resets them must still stop that child, not just set its copy
        # of the parent's flag. Terminal INT is handled by the parent alone.
        if os.getpid() != self.owner_pid:
            if signum == signal.SIGTERM:
                # Before multiprocessing's child bootstrap, unwinding could
                # run the parent's inherited cleanup against sibling PIDs.
                os._exit(0)
            return
        self.stopping = True
        self.received_signal = signum

    def __enter__(self):
        for signum in (signal.SIGINT, signal.SIGTERM):
            self.previous_handlers[signum] = signal.signal(
                signum, self._request_shutdown
            )
        return self

    def start(self, target):
        if self.stopping:
            return None
        process = self.process_factory(target=target)
        # Register before start: a signal during fork must not leave an
        # untracked child. The signal handler only sets a flag in the parent.
        self.children.append(process)
        process.start()
        return process

    def reap(self, process):
        process.join()
        self.children.remove(process)

    def __exit__(self, exc_type, exc_value, traceback):
        self.stopping = True
        try:
            # Signal every live child before waiting for any one of them.
            for process in self.children:
                if process.pid is not None and process.is_alive():
                    process.terminate()
            for process in self.children:
                if process.pid is not None:
                    process.join()
        finally:
            for signum, handler in self.previous_handlers.items():
                signal.signal(signum, handler)


def configure_worker_signals():
    """Let the parent coordinate Ctrl+C; accept its SIGTERM without delay."""
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
