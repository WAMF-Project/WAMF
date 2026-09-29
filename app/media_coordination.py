"""Cross-thread/process coordination for archive writes and retention scans."""

from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import threading

from wamf_paths import get_database_path


LOCK_MODE = 0o600
_locks_guard = threading.Lock()
_media_thread_locks = {}
_retention_thread_locks = {}


def _target_database_path(database_path=None):
    return Path(database_path or get_database_path()).expanduser().resolve()


def _thread_lock(lock_map, lock_path):
    key = str(lock_path)
    with _locks_guard:
        return lock_map.setdefault(key, threading.Lock())


def _open_lock(lock_path):
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, LOCK_MODE)
    os.fchmod(descriptor, LOCK_MODE)
    return descriptor


@contextmanager
def media_activity_guard(database_path=None):
    """Serialize archive-file creation/DB commit with retention media scans."""

    database_path = _target_database_path(database_path)
    lock_path = Path(f"{database_path}.media.lock")
    thread_lock = _thread_lock(_media_thread_locks, lock_path)
    with thread_lock:
        descriptor = _open_lock(lock_path)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)


@contextmanager
def retention_execution_guard(database_path=None):
    """Yield whether this caller acquired the non-overlapping retention guard."""

    database_path = _target_database_path(database_path)
    lock_path = Path(f"{database_path}.retention.lock")
    thread_lock = _thread_lock(_retention_thread_locks, lock_path)
    if not thread_lock.acquire(blocking=False):
        yield False
        return

    descriptor = None
    acquired_file_lock = False
    try:
        descriptor = _open_lock(lock_path)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired_file_lock = True
        except BlockingIOError:
            yield False
            return
        yield True
    finally:
        if descriptor is not None:
            try:
                if acquired_file_lock:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)
        thread_lock.release()
