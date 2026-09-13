"""Process lifecycle helpers used by the admin interface."""

import logging
import os
import secrets
import sys
import signal
import threading
import time


logger = logging.getLogger(__name__)
INSTANCE_ID = secrets.token_hex(16)
_supervisor_pid = None
_restart_scheduled = False
_restart_lock = threading.Lock()


def schedule_restart(delay=1.0):
    """Request one parent re-exec after the HTTP response can complete."""
    global _restart_scheduled
    if _supervisor_pid is None or os.getppid() != _supervisor_pid:
        raise RuntimeError('Restart requires the native WAMF parent process.')
    with _restart_lock:
        if _restart_scheduled:
            return
        _restart_scheduled = True

    def request_restart():
        time.sleep(delay)
        if os.getppid() == _supervisor_pid:
            try:
                os.kill(_supervisor_pid, signal.SIGUSR1)
            except ProcessLookupError:
                pass  # An ordinary shutdown may have completed first.

    threading.Thread(
        target=request_restart,
        name="wamf-restart",
        daemon=True,
    ).start()


def reexec_application():
    """Replace the parent, retaining its interpreter, invocation, PID and env."""
    os.execv(sys.executable, [sys.executable, *sys.orig_argv[1:]])


class WorkerSupervisor:
    """Own worker lifetime, including startup failures and parent-only signals."""

    def __init__(self, process_factory):
        self.process_factory = process_factory
        self.children = []
        self.stopping = False
        self.received_signal = None
        self.restart_requested = False
        self.children_joined = False
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
        if signum == signal.SIGUSR1:
            if self.stopping:
                return
            self.restart_requested = True
        else:
            # An operator stop always wins over a queued admin restart.
            self.restart_requested = False
        self.stopping = True
        self.received_signal = signum
        if signum != signal.SIGUSR1 and self.children_joined:
            # Cancel even a stop arriving between the final restart check and
            # execv. At this point exiting cannot leave any owned child behind.
            raise SystemExit(0)

    def __enter__(self):
        global _supervisor_pid
        self.previous_supervisor_pid = _supervisor_pid
        _supervisor_pid = self.owner_pid
        for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGUSR1):
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
        global _supervisor_pid
        self.stopping = True
        try:
            # Signal every live child before waiting for any one of them.
            for process in self.children:
                if process.pid is not None and process.is_alive():
                    process.terminate()
            for process in self.children:
                if process.pid is not None:
                    process.join()
            self.children_joined = True
            if exc_type is None and self.restart_requested:
                logger.info("All child processes joined; re-executing WAMF")
                reexec_application()
        finally:
            _supervisor_pid = self.previous_supervisor_pid
            for signum, handler in self.previous_handlers.items():
                signal.signal(signum, handler)


def configure_worker_signals():
    """Let the parent coordinate Ctrl+C; accept its SIGTERM without delay."""
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    signal.signal(signal.SIGUSR1, signal.SIG_IGN)
