"""Directory locking and durable, atomic configuration replacement."""

import errno
import fcntl
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def config_lock(path):
    descriptor = os.open(Path(path).parent, os.O_RDONLY)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


def atomic_write_config(path, content, before_replace=None):
    """Caller holds config_lock. Never fall back to an in-place write."""
    path = Path(path)
    if path.exists() and not path.stat().st_mode & 0o222:
        raise PermissionError(errno.EACCES, 'Configuration is read-only')
    temporary = None
    try:
        descriptor, temporary = tempfile.mkstemp(prefix='.wamf-config-', dir=path.parent)
        with os.fdopen(descriptor, 'w', encoding='utf-8') as output:
            os.fchmod(output.fileno(), 0o600)
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        if before_replace is not None:
            before_replace()
        # A single-file Docker mount fails here with EBUSY. Its original stays
        # intact; directory mounts support replacement normally.
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            try:
                os.fsync(directory)
            except OSError as error:
                if error.errno not in (errno.EINVAL, errno.ENOTSUP):
                    raise
        finally:
            os.close(directory)
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)
