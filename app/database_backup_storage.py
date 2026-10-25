"""Private, descriptor-relative storage primitives; no backup/restore execution."""

import fcntl
import os
import re
import secrets
import stat
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import wamf_paths

_backup_owner = ContextVar('wamf_backup_owner', default=None)


class BackupStorageError(ValueError):
    """Safe storage failure, without exposing an underlying filesystem path."""


class BackupBusyError(BackupStorageError):
    """Another creator owns this managed root."""


class BackupCollisionError(BackupStorageError):
    """No fresh artifact name could be reserved within the retry limit."""


@dataclass(frozen=True)
class BackupIdentity:
    backup_id: str

    def __post_init__(self):
        if not isinstance(self.backup_id, str):
            raise BackupStorageError('Invalid backup identifier')
        match = re.fullmatch(r'wamf-db-([0-9]{8}T[0-9]{12}Z)-[0-9a-f]{32}', self.backup_id)
        if match is None:
            raise BackupStorageError('Invalid backup identifier')
        try:
            datetime.strptime(match[1], '%Y%m%dT%H%M%S%fZ')
        except ValueError as exc:
            raise BackupStorageError('Invalid backup identifier') from exc

    @property
    def filename(self):
        return f'{self.backup_id}.zip'

    @classmethod
    def generate(cls):
        stamp = datetime.now(UTC).strftime('%Y%m%dT%H%M%S%fZ')
        return cls(f'wamf-db-{stamp}-{secrets.token_hex(16)}')


def _check_file(descriptor):
    info = os.fstat(descriptor)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise BackupStorageError('Backup must be a regular file with one link')
    return info


@contextmanager
def backup_directory(*, staging=False, create=False):
    """Pin every managed directory with no-follow opens, including during mkdir.

    Only the fixed data mount is a trust anchor. Never chmod the data mount;
    owned backup descendants are private. No filesystem work occurs on import.
    """
    descriptors = []
    try:
        try:
            root = (wamf_paths.REPO_ROOT / 'data').resolve()
            if create:
                root.mkdir(mode=0o700, exist_ok=True)
            descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            descriptors.append(descriptor)
            for component in ('backups', 'database', *(('.staging',) if staging else ())):
                if create:
                    try:
                        os.mkdir(component, mode=0o700, dir_fd=descriptor)
                    except FileExistsError:
                        pass
                descriptor = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                     dir_fd=descriptor)
                descriptors.append(descriptor)
                info = os.fstat(descriptor)
                if info.st_uid != os.geteuid():
                    raise BackupStorageError('Backup directory has an unexpected owner')
                if create:
                    os.fchmod(descriptor, 0o700)
                elif stat.S_IMODE(info.st_mode) != 0o700:
                    raise BackupStorageError('Backup directory must be private')
        except OSError as exc:
            raise BackupStorageError('Backup storage is unavailable or unsafe') from exc
        yield descriptor
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


@contextmanager
def reserve_backup(*, staged=True, attempts=8):
    """Reserve a generated empty artifact exclusively, normally for staging.

    On failure remove only the inode reserved by this operation. A successful
    caller owns the reserved file; this primitive does not publish a backup.
    """
    with backup_directory(staging=staged, create=True) as directory:
        for _ in range(attempts):
            identity = BackupIdentity.generate()
            try:
                descriptor = os.open(identity.filename, os.O_RDWR | os.O_CREAT | os.O_EXCL
                                     | os.O_NOFOLLOW, 0o600, dir_fd=directory)
            except FileExistsError:
                continue
            break
        else:
            raise BackupCollisionError('Backup identifier collision limit reached')
        reserved = os.fstat(descriptor)
        try:
            with os.fdopen(descriptor, 'w+b') as file:
                _check_file(file.fileno())
                os.fchmod(file.fileno(), 0o600)
                yield identity, file
        except BaseException:
            try:
                current = os.stat(identity.filename, dir_fd=directory, follow_symlinks=False)
                if (current.st_dev, current.st_ino) == (reserved.st_dev, reserved.st_ino):
                    os.unlink(identity.filename, dir_fd=directory)
            except FileNotFoundError:
                pass
            raise


@contextmanager
def open_backup(identity, *, staged=False):
    """Read an artifact by validated ID, never by a caller-supplied path."""
    identity = BackupIdentity(identity.backup_id)
    with backup_directory(staging=staged) as directory:
        descriptor = os.open(identity.filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                             dir_fd=directory)
        with os.fdopen(descriptor, 'rb') as file:
            info = _check_file(file.fileno())
            if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600:
                raise BackupStorageError('Backup file must be privately owned')
            yield file


@contextmanager
def backup_operation_guard(*, allow_reentry=False):
    """Nonwaiting, per-root process/thread exclusion; never unlink the lock."""
    owner = _backup_owner.get()
    if allow_reentry and owner is not None and owner[:2] == (os.getpid(), threading.get_ident()):
        _check_file(owner[2])
        yield
        return
    with backup_directory(create=True) as directory:
        descriptor = os.open('.backup.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW
                             | os.O_NONBLOCK, 0o600, dir_fd=directory)
        try:
            _check_file(descriptor)
            if os.fstat(descriptor).st_uid != os.geteuid():
                raise BackupStorageError('Unexpected backup lock owner')
            os.fchmod(descriptor, 0o600)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise BackupBusyError('Another backup is in progress') from exc
            token = _backup_owner.set((os.getpid(), threading.get_ident(), descriptor))
            try:
                yield
            finally:
                _backup_owner.reset(token)
        finally:
            os.close(descriptor)


def check_artifact_entry(identity, file, *, staged=False):
    """Ensure the pinned file is still the sole, private published entry."""
    with backup_directory(staging=staged) as directory:
        current = os.stat(identity.filename, dir_fd=directory, follow_symlinks=False)
        opened = _check_file(file.fileno())
        if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
            raise BackupStorageError('Backup entry changed during access')
        if opened.st_uid != os.geteuid() or stat.S_IMODE(opened.st_mode) != 0o600:
            raise BackupStorageError('Backup file must be privately owned')
        return opened


@contextmanager
def validation_workspace():
    with _database_workspace('validate', sidecars=False) as workspace:
        yield workspace


@contextmanager
def backup_workspace():
    with _database_workspace('create', sidecars=True) as workspace:
        yield workspace


@contextmanager
def _database_workspace(prefix, *, sidecars):
    """Create one private extraction directory; always remove its own payload."""
    with backup_directory(staging=True, create=True) as parent:
        name = f'{prefix}-{secrets.token_hex(16)}'
        os.mkdir(name, mode=0o700, dir_fd=parent)
        descriptor = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
        try:
            os.fchmod(descriptor, 0o700)
            path = wamf_paths.get_database_backup_staging_root() / name / 'database.db'
            yield descriptor, Path(path)
        finally:
            # SQLite is read-only and WAL is refused before opening. Remove only
            # the extraction we created; never recursively traverse directories.
            for filename in ('database.db', *(('database.db-journal', 'database.db-wal',
                                              'database.db-shm') if sidecars else ())):
                try:
                    os.unlink(filename, dir_fd=descriptor)
                except FileNotFoundError:
                    pass
            os.close(descriptor)
            os.rmdir(name, dir_fd=parent)
