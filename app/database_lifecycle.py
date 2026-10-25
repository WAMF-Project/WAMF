"""Cooperative process/connection lifecycle exclusion, separate from SQLite locks."""

import fcntl
import os
import stat
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

_exclusive = ContextVar('wamf_database_exclusive', default=None)
_maintenance = ContextVar('wamf_database_maintenance', default=None)


class DatabaseLifecycleError(RuntimeError):
    pass


def canonical_database_path(path):
    return Path(path).expanduser().resolve()


class LifecycleLease:
    def __init__(self, path, descriptor, mode):
        self.path, self.descriptor, self.mode = path, descriptor, mode
        self.pid, self.thread = os.getpid(), threading.get_ident()
        self.active = True
        self.maintenance_consumers = 0

    def downgrade(self):
        require_exclusive(self)
        fcntl.flock(self.descriptor, fcntl.LOCK_SH)
        self.mode = 'shared'


def require_lifecycle_owner(lease):
    if (not isinstance(lease, LifecycleLease) or not lease.active
            or lease.pid != os.getpid() or lease.thread != threading.get_ident()
            or _exclusive.get() is not lease):
        raise DatabaseLifecycleError('Database lifecycle ownership is required')


def require_exclusive(lease, path=None, *, allow_connections=False):
    require_lifecycle_owner(lease)
    if (lease.mode != 'exclusive' or (lease.maintenance_consumers and not allow_connections)
            or (path is not None and canonical_database_path(path) != lease.path)):
        raise DatabaseLifecycleError('Exclusive database lifecycle ownership is required')
    info = os.fstat(lease.descriptor)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise DatabaseLifecycleError('Unsafe database lifecycle lock')


@contextmanager
def _lease(path, exclusive):
    path = canonical_database_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(f'{path}.lifecycle.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW
                         | os.O_NONBLOCK, 0o600)
    lease = None
    token = None
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid():
            raise DatabaseLifecycleError('Unsafe database lifecycle lock')
        os.fchmod(descriptor, 0o600)
        try:
            fcntl.flock(descriptor, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise DatabaseLifecycleError('Database lifecycle is in use') from exc
        lease = LifecycleLease(path, descriptor, 'exclusive' if exclusive else 'shared')
        if exclusive:
            token = _exclusive.set(lease)
        yield lease
    finally:
        if token is not None:
            _exclusive.reset(token)
        if lease is not None:
            lease.active = False
        # An escaped maintenance connection still owns the kernel exclusion.
        # Its final close releases this descriptor; never unlock beneath it.
        if lease is None or lease.maintenance_consumers == 0:
            os.close(descriptor)
            if lease is not None:
                lease.descriptor = -1


@contextmanager
def exclusive_database_lifecycle(path):
    """Offline caller-owned boundary. All supported consumers must be stopped."""
    with _lease(path, True) as lease:
        yield lease


@contextmanager
def maintenance_access(lease):
    require_exclusive(lease)
    token = _maintenance.set(lease)
    try:
        yield
    finally:
        _maintenance.reset(token)


@contextmanager
def database_consumer(path, *, lifecycle_owner=None):
    """Shared lifetime lease; connections and background consumers use this API."""
    path = canonical_database_path(path)
    owner = lifecycle_owner or _maintenance.get()
    if owner is not None and owner.path == path:
        require_exclusive(owner, path, allow_connections=True)
        owner.maintenance_consumers += 1
        try:
            yield owner
        finally:
            owner.maintenance_consumers -= 1
            if not owner.active and owner.maintenance_consumers == 0 and owner.descriptor >= 0:
                os.close(owner.descriptor)
                owner.descriptor = -1
        return
    with _lease(path, False) as lease:
        from app.database_restore_application import database_access_allowed

        if not database_access_allowed(path):
            raise DatabaseLifecycleError('Database requires offline restore/startup recovery')
        yield lease
