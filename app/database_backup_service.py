"""Online, database-only backups. No configuration loading, restore, or UI."""

import errno
import hashlib
import json
import logging
import math
import os
import secrets
import sqlite3
import stat
import subprocess
import sys
import threading
import time
import zipfile
from contextlib import closing, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import BinaryIO

from app import database_backup as package
from app.database_backup_storage import (
    BackupBusyError,
    BackupCollisionError,
    BackupIdentity,
    BackupStorageError,
    backup_directory,
    backup_operation_guard,
    backup_workspace,
    check_artifact_entry,
    open_backup,
    reserve_backup,
)
from app.database_lifecycle import (
    DatabaseLifecycleError,
    database_consumer,
    require_exclusive,
)
from app.db_inspection import (
    DatabaseCompatibility,
    DatabaseInspection,
    InspectionError,
    MigrationHistoryEntry,
    inspect_database,
)
from wamf_paths import DatabaseBackupPathError, get_database_backup_root

logger = logging.getLogger(__name__)
DEFAULT_TIMEOUT_SECONDS = 300.0
DEFAULT_PAGE_BATCH = 64
DISK_MARGIN_BYTES = 64 * 1024 * 1024
_creating_backup = ContextVar('wamf_creating_backup', default=None)


class BackupPhase(StrEnum):
    PREFLIGHT = 'preflight'
    BACKING_UP = 'backing_up'
    VALIDATING = 'validating'
    PACKAGING = 'packaging'
    PUBLISHING = 'publishing'
    COMPLETE = 'complete'
    FAILED = 'failed'
    CANCELLED = 'cancelled'


class BackupCode(StrEnum):
    COMPLETE = 'complete'
    DURABILITY_UNCERTAIN = 'durability_uncertain'
    BUSY = 'backup_in_progress'
    INVALID_REQUEST = 'invalid_request'
    SOURCE_MISSING = 'source_missing'
    SOURCE_UNSAFE = 'source_unsafe'
    SOURCE_UNAVAILABLE = 'source_unavailable'
    SOURCE_NOT_CURRENT = 'source_not_current'
    SOURCE_UNHEALTHY = 'source_unhealthy'
    WAL_UNSUPPORTED = 'wal_unsupported'
    TOO_LARGE = 'database_too_large'
    INSUFFICIENT_SPACE = 'insufficient_space'
    CANCELLED = 'cancelled'
    DEADLINE = 'deadline_exceeded'
    DATABASE_ERROR = 'database_error'
    DESTINATION_INVALID = 'destination_invalid'
    PACKAGE_INVALID = 'package_invalid'
    UNSAFE_STORAGE = 'unsafe_storage'
    FILESYSTEM_ERROR = 'filesystem_error'
    FILE_SYNC_FAILED = 'file_sync_failed'
    PUBLICATION_FAILED = 'publication_failed'
    COLLISION = 'publication_collision'
    INTERNAL_ERROR = 'internal_error'


MESSAGES = {
    BackupCode.COMPLETE: 'Backup created and verified.',
    BackupCode.DURABILITY_UNCERTAIN: 'Backup published; directory durability could not be confirmed.',
    BackupCode.BUSY: 'Another database backup is in progress.',
    BackupCode.INVALID_REQUEST: 'Backup parameters are invalid.',
    BackupCode.SOURCE_MISSING: 'The bound database does not exist.',
    BackupCode.SOURCE_UNSAFE: 'The bound database identity is unsafe or changed.',
    BackupCode.SOURCE_UNAVAILABLE: 'The bound database could not be read.',
    BackupCode.SOURCE_NOT_CURRENT: 'The bound database schema is not current.',
    BackupCode.SOURCE_UNHEALTHY: 'The bound database did not pass health checks.',
    BackupCode.WAL_UNSUPPORTED: 'WAL-format database backup is not supported in this version.',
    BackupCode.TOO_LARGE: 'The database exceeds the backup size limit.',
    BackupCode.INSUFFICIENT_SPACE: 'Backup storage has insufficient free space.',
    BackupCode.CANCELLED: 'Backup cancelled before publication.',
    BackupCode.DEADLINE: 'Backup exceeded its operation deadline.',
    BackupCode.DATABASE_ERROR: 'SQLite could not complete the backup.',
    BackupCode.DESTINATION_INVALID: 'The copied database did not pass verification.',
    BackupCode.PACKAGE_INVALID: 'The completed package did not pass verification.',
    BackupCode.UNSAFE_STORAGE: 'Backup storage is unavailable or unsafe.',
    BackupCode.FILESYSTEM_ERROR: 'Backup staging could not be completed.',
    BackupCode.FILE_SYNC_FAILED: 'The staged package could not be synced to storage.',
    BackupCode.PUBLICATION_FAILED: 'The backup could not be published.',
    BackupCode.COLLISION: 'A fresh backup filename could not be reserved.',
    BackupCode.INTERNAL_ERROR: 'The backup operation could not complete.',
}


class _Abort(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code.value)


@dataclass(frozen=True)
class BackupProgress:
    operation_id: str
    phase: BackupPhase
    started_at: str
    completed_at: str | None = None
    pages_copied: int = 0
    pages_total: int | None = None
    bytes_copied: int = 0
    backup_id: str | None = None
    final_size: int | None = None
    package_sha256: str | None = None
    database_sha256: str | None = None
    code: BackupCode | None = None
    summary: str | None = None

    @property
    def success(self):
        if self.code is None:
            return None
        return self.code in (BackupCode.COMPLETE, BackupCode.DURABILITY_UNCERTAIN)

    @property
    def percent(self):
        if not self.pages_total:
            return None
        return round(100 * self.pages_copied / self.pages_total, 1)


@dataclass(frozen=True)
class BackupResult:
    operation_id: str
    phase: BackupPhase
    started_at: str
    completed_at: str
    code: BackupCode
    summary: str
    backup_id: str | None = None
    final_size: int | None = None
    package_sha256: str | None = None
    facts: package.ArtifactFacts | None = None
    manifest: package.BackupManifest | None = None

    @property
    def success(self):
        return self.code in (BackupCode.COMPLETE, BackupCode.DURABILITY_UNCERTAIN)

    @property
    def durability_warning(self):
        return self.code == BackupCode.DURABILITY_UNCERTAIN


def _utc_now():
    return datetime.now(UTC).isoformat(timespec='microseconds').replace('+00:00', 'Z')


class _Operation:
    def __init__(self, timeout, cancelled, observer):
        self.snapshot = BackupProgress(secrets.token_hex(16), BackupPhase.PREFLIGHT, _utc_now())
        self.deadline = time.monotonic() + timeout
        self.cancelled = cancelled
        self.observer = observer
        self.published = None

    def check(self):
        if self.cancelled is not None and self.cancelled():
            raise _Abort(BackupCode.CANCELLED)
        if time.monotonic() >= self.deadline:
            raise _Abort(BackupCode.DEADLINE)

    def emit(self, phase, **changes):
        self.snapshot = replace(self.snapshot, phase=phase, **changes)
        if self.observer is not None:
            try:
                self.observer(self.snapshot)
            except (RuntimeError, ValueError, TypeError, OSError) as exc:
                # Observers do not own operation cancellation or publication.
                logger.warning('Backup progress observer failed (%s)', type(exc).__name__)


def _managed_root(root):
    expected = get_database_backup_root()
    if Path(root) != expected:
        raise BackupStorageError('Only the managed database backup root is supported')
    return expected


@contextmanager
def _source_identity(bound_path):
    """Record a regular source identity without opening a raw database FD.

    Closing a non-SQLite DB descriptor can release *other threads'* SQLite
    POSIX locks. Directory descriptors and stat calls avoid that hazard.
    """
    path = Path(bound_path)
    if not path.is_absolute() or '..' in path.parts:
        raise _Abort(BackupCode.SOURCE_UNSAFE)
    descriptors = []
    try:
        try:
            directory = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
            descriptors.append(directory)
            for component in path.parts[1:-1]:
                directory = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                    dir_fd=directory)
                descriptors.append(directory)
            original = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
            if not stat.S_ISREG(original.st_mode) or original.st_nlink != 1:
                raise _Abort(BackupCode.SOURCE_UNSAFE)
        except FileNotFoundError as exc:
            raise _Abort(BackupCode.SOURCE_MISSING) from exc
        except OSError as exc:
            code = (BackupCode.SOURCE_UNSAFE if exc.errno in (errno.ELOOP, errno.ENOTDIR)
                    else BackupCode.SOURCE_UNAVAILABLE)
            raise _Abort(code) from exc

        def unchanged():
            try:
                current = os.stat(path, follow_symlinks=False)
            except OSError as exc:
                raise _Abort(BackupCode.SOURCE_UNSAFE) from exc
            if ((current.st_dev, current.st_ino) != (original.st_dev, original.st_ino)
                    or current.st_nlink != 1 or not stat.S_ISREG(current.st_mode)):
                raise _Abort(BackupCode.SOURCE_UNSAFE)
            return current

        yield path, original, unchanged
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _inspect_source(path, operation):
    """Own/reap a fresh interpreter, isolating #52's raw header read/close."""
    operation.check()
    process = subprocess.Popen(
        [sys.executable, '-B', '-m', 'app.database_backup_probe', str(os.getpid()), str(path)],
        cwd=Path(__file__).resolve().parent.parent,
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
    )
    try:
        while True:
            operation.check()
            try:
                output, _ = process.communicate(timeout=0.05)
                break
            except subprocess.TimeoutExpired:
                continue
        operation.check()
        if process.returncode != 0 or len(output.encode('utf-8')) > 64 * 1024:
            raise _Abort(BackupCode.SOURCE_UNAVAILABLE)
        data = json.loads(output)
        data['compatibility'] = DatabaseCompatibility(data['compatibility'])
        data['error_category'] = InspectionError(data['error_category']) if data['error_category'] else None
        data['history'] = tuple(MigrationHistoryEntry(**entry) for entry in data['history'])
        return DatabaseInspection(**data)
    except (ValueError, TypeError, KeyError) as exc:
        raise _Abort(BackupCode.SOURCE_UNAVAILABLE) from exc
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate()  # Reap and close pipes on success/cancellation/failure.


def _full_integrity(path, operation):
    return package.check_staged_integrity(path, checkpoint=operation.check)


def _digest(file, operation=None, *, checkpoint=None):
    digest, size = hashlib.sha256(), 0
    file.seek(0)
    while True:
        if operation is not None:
            operation.check()
        if checkpoint is not None:
            checkpoint()
        chunk = file.read(package.CHUNK_BYTES)
        if not chunk:
            break
        size += len(chunk)
        digest.update(chunk)
    file.seek(0)
    return size, digest.hexdigest()


def _copy_database(source, destination, operation, page_size, pages):
    def progress(status, remaining, total):
        operation.check()
        if total * page_size > package.MAX_DATABASE_BYTES:
            raise _Abort(BackupCode.TOO_LARGE)
        copied = max(0, total - remaining)
        operation.emit(BackupPhase.BACKING_UP, pages_copied=copied, pages_total=total,
                       bytes_copied=copied * page_size)
        operation.check()

    source.backup(destination, pages=pages, progress=progress, name='main', sleep=0.01)


def _write_package(file, database_path, manifest, operation):
    with zipfile.ZipFile(file, 'w', compression=zipfile.ZIP_STORED, allowZip64=False) as archive:
        info = zipfile.ZipInfo(package.DATABASE_MEMBER)
        info.file_size = manifest.database_size
        info.external_attr = (stat.S_IFREG | 0o600) << 16
        with (database_path.open('rb') as source, archive.open(info, 'w') as destination):
            while chunk := source.read(package.CHUNK_BYTES):
                operation.check()
                destination.write(chunk)
        manifest = replace(manifest, completed_at=_utc_now())
        archive.writestr(package.MANIFEST_MEMBER, manifest.to_bytes())
    file.flush()
    operation.check()
    try:
        os.fsync(file.fileno())
    except OSError as exc:
        raise _Abort(BackupCode.FILE_SYNC_FAILED) from exc
    return manifest


def _publish(staged_identity, file, operation):
    """Replace only our O_EXCL reservation under the cooperating writer guard."""
    with (reserve_backup(staged=False) as (identity, reservation),
          backup_directory() as root, backup_directory(staging=True) as staging):
        operation.check()
        check_artifact_entry(identity, reservation)
        check_artifact_entry(staged_identity, file, staged=True)
        if os.fstat(root).st_dev != os.fstat(staging).st_dev:
            raise _Abort(BackupCode.PUBLICATION_FAILED)
        try:
            os.replace(staged_identity.filename, identity.filename,
                       src_dir_fd=staging, dst_dir_fd=root)
        except OSError as exc:
            raise _Abort(BackupCode.PUBLICATION_FAILED) from exc
        operation.published = identity
        # No cancellation after commit: the artifact is now visible. A failure
        # here must never be reported as an unpublished operation or delete it.
        try:
            os.fsync(root)
            os.fsync(staging)
        except OSError:
            return identity, BackupCode.DURABILITY_UNCERTAIN
        return identity, BackupCode.COMPLETE


def create_backup(bound_database_path, backup_root, *, lifecycle_owner=None, **kwargs):
    owner = (os.getpid(), threading.get_ident())
    if _creating_backup.get() == owner:
        now = _utc_now()
        return BackupResult(secrets.token_hex(16), BackupPhase.FAILED, now, now,
                            BackupCode.BUSY, MESSAGES[BackupCode.BUSY])
    token = _creating_backup.set(owner)
    try:
        if lifecycle_owner is not None:
            require_exclusive(lifecycle_owner, bound_database_path)
        with database_consumer(bound_database_path, lifecycle_owner=lifecycle_owner):
            return _create_backup(bound_database_path, backup_root,
                                  _offline_guard=lifecycle_owner is not None, **kwargs)
    except DatabaseLifecycleError:
        now = _utc_now()
        return BackupResult(secrets.token_hex(16), BackupPhase.FAILED, now, now,
                            BackupCode.BUSY, MESSAGES[BackupCode.BUSY])
    finally:
        _creating_backup.reset(token)


def _create_backup(bound_database_path, backup_root, *, _offline_guard=False, timeout_seconds=DEFAULT_TIMEOUT_SECONDS,
                  page_batch=DEFAULT_PAGE_BATCH, cancelled=None, progress=None):
    """Back up the caller's absolute, bound path without reading configuration.

    Deadlines/cancellation are cooperative across SQLite steps and streamed I/O.
    Filesystem syscalls and short SQLite lock waits are not hard-preemptible.
    """
    valid_parameters = (type(timeout_seconds) in (int, float) and math.isfinite(timeout_seconds)
                        and timeout_seconds >= 0 and type(page_batch) is int
                        and 1 <= page_batch <= 4096)
    operation = _Operation(timeout_seconds if valid_parameters else 0, cancelled, progress)
    facts = manifest = package_size = package_sha = None
    code = BackupCode.INTERNAL_ERROR
    try:
        if not valid_parameters:
            raise _Abort(BackupCode.INVALID_REQUEST)
        operation.emit(BackupPhase.PREFLIGHT)
        operation.check()
        _managed_root(backup_root)
        with backup_operation_guard(allow_reentry=_offline_guard), _source_identity(bound_database_path) as (source_path, info, unchanged):
            if info.st_size > package.MAX_DATABASE_BYTES:
                raise _Abort(BackupCode.TOO_LARGE)
            source_inspection = _inspect_source(source_path, operation)
            operation.check()
            if source_inspection.error_category == InspectionError.UNSUPPORTED_JOURNAL_MODE:
                raise _Abort(BackupCode.WAL_UNSUPPORTED)
            if source_inspection.error_category in (InspectionError.CORRUPT_DATABASE,
                                                    InspectionError.INTEGRITY_CHECK_FAILED):
                raise _Abort(BackupCode.SOURCE_UNHEALTHY)
            if source_inspection.error_category in (InspectionError.LOCKED_DATABASE,
                                                    InspectionError.UNREADABLE_DATABASE):
                raise _Abort(BackupCode.SOURCE_UNAVAILABLE)
            if source_inspection.compatibility != DatabaseCompatibility.CURRENT:
                raise _Abort(BackupCode.SOURCE_NOT_CURRENT)
            if not _full_integrity(source_path, operation):
                raise _Abort(BackupCode.SOURCE_UNHEALTHY)
            current_info = unchanged()
            with backup_directory() as root:
                disk = os.fstatvfs(root)
                required = max(info.st_size, current_info.st_size) * 3 + package.MAX_MANIFEST_BYTES + DISK_MARGIN_BYTES
                if disk.f_bavail * disk.f_frsize < required:
                    raise _Abort(BackupCode.INSUFFICIENT_SPACE)
            with backup_workspace() as (directory, destination_path):
                descriptor = os.open(package.DATABASE_MEMBER, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                                     | os.O_NOFOLLOW, 0o600, dir_fd=directory)
                try:
                    os.fchmod(descriptor, 0o600)
                finally:
                    os.close(descriptor)
                operation.emit(BackupPhase.BACKING_UP)
                operation.check()
                with (closing(sqlite3.connect(source_path.as_uri() + '?mode=ro', uri=True,
                                              timeout=0.1)) as source,
                      closing(sqlite3.connect(destination_path, timeout=0.1)) as destination):
                    source.execute('PRAGMA query_only = ON')
                    page_size = source.execute('PRAGMA page_size').fetchone()[0]
                    unchanged()
                    _copy_database(source, destination, operation, page_size, page_batch)
                unchanged()
                operation.check()
                operation.emit(BackupPhase.VALIDATING)
                if any(Path(f'{destination_path}{suffix}').exists()
                       for suffix in ('-journal', '-wal', '-shm')):
                    raise _Abort(BackupCode.DESTINATION_INVALID)
                inspection = inspect_database(destination_path, check_integrity=True,
                                              checkpoint=operation.check)
                if (inspection.compatibility != DatabaseCompatibility.CURRENT
                        or inspection.history != source_inspection.history
                        or not _full_integrity(destination_path, operation)):
                    raise _Abort(BackupCode.DESTINATION_INVALID)
                with destination_path.open('rb') as file:
                    database_size, database_sha = _digest(file, operation)
                if database_size > package.MAX_DATABASE_BYTES:
                    raise _Abort(BackupCode.TOO_LARGE)
                facts = package.ArtifactFacts(database_size, database_sha)
                manifest = package.BackupManifest.from_inspection(
                    inspection, facts, started_at=operation.snapshot.started_at,
                    completed_at=_utc_now(), source_database=source_path.name)
                operation.emit(BackupPhase.PACKAGING)
                with reserve_backup() as (staged_identity, file):
                    manifest = _write_package(file, destination_path, manifest, operation)
                    validation = package.validate_package(staged_identity, staged=True,
                                                          checkpoint=operation.check)
                    if not validation.valid or validation.facts != facts or validation.manifest != manifest:
                        raise _Abort(BackupCode.PACKAGE_INVALID)
                    package_size, package_sha = _digest(file, operation)
                    operation.emit(BackupPhase.PUBLISHING)
                    _identity, code = _publish(staged_identity, file, operation)
    except _Abort as exc:
        code = exc.code
    except BackupBusyError:
        code = BackupCode.BUSY
    except BackupCollisionError:
        code = BackupCode.COLLISION
    except (BackupStorageError, DatabaseBackupPathError):
        code = BackupCode.UNSAFE_STORAGE
    except sqlite3.Error:
        code = BackupCode.DATABASE_ERROR
    except OSError as exc:
        code = (BackupCode.INSUFFICIENT_SPACE if exc.errno == errno.ENOSPC
                else BackupCode.FILESYSTEM_ERROR)
    except (RuntimeError, ValueError, TypeError) as exc:
        logger.error('Database backup failed (%s)', type(exc).__name__)
        code = BackupCode.INTERNAL_ERROR
    if operation.published is not None and code not in (BackupCode.COMPLETE, BackupCode.DURABILITY_UNCERTAIN):
        code = BackupCode.DURABILITY_UNCERTAIN
    success = code in (BackupCode.COMPLETE, BackupCode.DURABILITY_UNCERTAIN)
    phase = (BackupPhase.COMPLETE if success else BackupPhase.CANCELLED
             if code == BackupCode.CANCELLED else BackupPhase.FAILED)
    completed = _utc_now()
    operation.emit(phase, backup_id=operation.published.backup_id if operation.published else None,
                   completed_at=completed, final_size=package_size if success else None,
                   package_sha256=package_sha if success else None,
                   database_sha256=facts.database_sha256 if facts else None, code=code, summary=MESSAGES[code])
    return BackupResult(operation.snapshot.operation_id, phase, operation.snapshot.started_at,
                        completed, code, MESSAGES[code], operation.snapshot.backup_id,
                        package_size if success else None, package_sha if success else None, facts, manifest)


@dataclass(frozen=True)
class BackupEntry:
    backup_id: str
    size: int | None
    validation: package.PackageValidationResult
    package_sha256: str | None = None

    @property
    def timestamp(self):
        return self.validation.manifest.completed_at if self.validation.manifest else None

    @property
    def manifest(self):
        return self.validation.manifest

    @property
    def compatibility(self):
        return self.validation.compatibility

    @property
    def checksum(self):
        return self.validation.facts.database_sha256 if self.validation.facts else None


@dataclass(frozen=True)
class BackupDownload:
    identity: BackupIdentity
    file: BinaryIO
    size: int
    package_sha256: str
    validation: package.PackageValidationResult


@contextmanager
def resolve_backup_download(backup_id, backup_root, *, validation_runner=None, checkpoint=None):
    """Yield the validated pinned handle; caller must stream within this context."""
    _managed_root(backup_root)
    identity = BackupIdentity(backup_id)
    with open_backup(identity) as file:
        validation = (validation_runner(identity, file) if validation_runner is not None else
                      package.validate_open_package(identity, file, checkpoint=checkpoint))
        if not validation.valid:
            raise package.PackageValidationError(validation.code)
        size, digest = _digest(file, checkpoint=checkpoint)
        check_artifact_entry(identity, file)
        yield BackupDownload(identity, file, size, digest, validation)


def list_backups(backup_root, *, validation_runner=None):
    """Validated entries plus safe per-artifact failures; never prune or repair."""
    root = _managed_root(backup_root)
    if not root.exists():
        return ()
    entries = []
    with backup_directory() as directory:
        for filename in sorted(os.listdir(directory), reverse=True):
            if not filename.endswith('.zip'):
                continue
            try:
                identity = BackupIdentity(filename[:-4])
            except BackupStorageError:
                continue
            size = None
            try:
                info = os.stat(filename, dir_fd=directory, follow_symlinks=False)
                if stat.S_ISREG(info.st_mode) and info.st_size == 0:
                    continue  # Exclusive unpublished reservation.
                size = info.st_size if stat.S_ISREG(info.st_mode) else None
                if size is not None and size > package.MAX_PACKAGE_BYTES:
                    validation = package.PackageValidationResult(package.ValidationCode.PACKAGE_TOO_LARGE, identity)
                    entries.append(BackupEntry(identity.backup_id, size, validation))
                    continue
                with open_backup(identity) as file:
                    validation = (validation_runner(identity, file) if validation_runner is not None else
                                  package.validate_open_package(identity, file))
                    check_artifact_entry(identity, file)
                    digest = _digest(file)[1] if validation.valid else None
                    entries.append(BackupEntry(identity.backup_id, size, validation, digest))
            except package.PackageValidationError as exc:
                entries.append(BackupEntry(identity.backup_id, size,
                                            package.PackageValidationResult(exc.code, identity)))
            except (OSError, ValueError):
                validation = package.PackageValidationResult(package.ValidationCode.UNSAFE_STORAGE, identity)
                entries.append(BackupEntry(identity.backup_id, size, validation))
    return tuple(entries)
