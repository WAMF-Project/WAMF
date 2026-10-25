"""Explicit request-owned Admin jobs and safe DTOs; no work starts on import."""

import os
import secrets
import threading
from contextlib import contextmanager
from dataclasses import asdict

from app import database_backup as package
from app.database_backup_service import create_backup
from app.database_backup_storage import backup_directory
from app.database_restore import (
    _Budget,
    _copy,
    _validate,
    stage_restore_from_backup,
    stage_restore_from_stream,
)
from app.database_restore_storage import OperationStore, private_file
from wamf_paths import get_database_backup_root

MAX_UPLOAD_REQUEST_BYTES = package.MAX_PACKAGE_BYTES + 64 * 1024


def isolated_package_validation(identity, file):
    """Bound untrusted archive/SQLite work, retaining the caller's pinned file."""
    budget = _Budget(30, None)
    before = os.fstat(file.fileno())
    with backup_directory(staging=True, create=True) as parent:
        name = f'admin-validate-{secrets.token_hex(16)}'
        os.mkdir(name, mode=0o700, dir_fd=parent)
        directory = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
        try:
            os.fchmod(directory, 0o700)
            file.seek(0)
            with private_file(directory, 'package.zip', create=True) as destination:
                _, digest = _copy(file, destination, package.MAX_PACKAGE_BYTES, budget)
            validation = _validate(OperationStore(directory, b'', identity.backup_id), budget)
            file.seek(0)
            if _copy(file, None, package.MAX_PACKAGE_BYTES, budget)[1] != digest:
                raise package.PackageValidationError(package.ValidationCode.CHECKSUM_MISMATCH)
            after = os.fstat(file.fileno())
            if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                    after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise package.PackageValidationError(package.ValidationCode.INVALID_PACKAGE)
            file.seek(0)
            return validation
        finally:
            for filename in ('package.zip', 'candidate.db', 'database.db'):
                try:
                    os.unlink(filename, dir_fd=directory)
                except FileNotFoundError:
                    pass
            os.close(directory)
            os.rmdir(name, dir_fd=parent)


def plan_dto(plan):
    return asdict(plan) if plan is not None else None


def restore_dto(result):
    return {'operation_id': result.operation_id, 'phase': result.phase.value,
            'code': result.code.value, 'success': result.success,
            'validation_code': result.validation_code.value if result.validation_code else None,
            'plan': plan_dto(result.plan)}


class DatabaseJobs:
    """One owned daemon thread per explicit job, terminated with its Flask owner.

    Restore plans/status are durable; job progress is intentionally ephemeral.
    Browser refresh reads snapshots and never repeats a mutating request.
    """
    def __init__(self):
        self.lock = threading.Lock()
        self.jobs = {}
        self.active = None
        self.reserved = False

    def snapshot(self, identifier=None):
        with self.lock:
            if identifier is None:
                identifier = next(reversed(self.jobs), None)
            if identifier not in self.jobs:
                return None
            return {key: value for key, value in self.jobs[identifier].items() if key != 'cancel'}

    def _publish(self, identifier, **updates):
        with self.lock:
            self.jobs[identifier].update(updates)

    @contextmanager
    def reserve_restart(self):
        with self.lock:
            if self.active is not None or self.reserved:
                raise ValueError('A database operation is still active')
            self.reserved = True
        try:
            yield
        finally:
            with self.lock:
                self.reserved = False

    def start(self, kind, bound_path, *, backup_id=None, stream=None, cleanup=None):
        with self.lock:
            if self.active is not None or self.reserved:
                raise ValueError('A database operation is still active')
            identifier = secrets.token_hex(16)
            cancelled = threading.Event()
            self.jobs[identifier] = {'job_id': identifier, 'kind': kind, 'phase': 'preflight',
                                     'done': False, 'cancel': cancelled, 'operation_id': None}
            self.active = identifier

        def run():
            try:
                if kind == 'backup':
                    def progress(snapshot):
                        self._publish(identifier, phase=snapshot.phase.value, percent=snapshot.percent)

                    result = create_backup(bound_path, get_database_backup_root(), progress=progress,
                                           cancelled=cancelled.is_set)
                    self._publish(identifier, phase=result.phase.value, result={
                        'success': result.success, 'code': result.code.value, 'message': result.summary,
                        'backup_id': result.backup_id, 'size': result.final_size})
                else:
                    def progress(operation_id, phase):
                        self._publish(identifier, operation_id=operation_id, phase=phase.value)

                    kwargs = {'progress': progress, 'cancelled': cancelled.is_set}
                    result = (stage_restore_from_backup(backup_id, bound_path, **kwargs) if stream is None else
                              stage_restore_from_stream(stream, bound_path, **kwargs))
                    self._publish(identifier, operation_id=result.operation_id, phase=result.phase.value,
                                  result=restore_dto(result))
            except (OSError, ValueError, RuntimeError):
                self._publish(identifier, phase='failed', result={'success': False, 'code': 'storage_error',
                                                                 'message': 'Database operation could not complete.'})
            finally:
                try:
                    if cleanup is not None:
                        cleanup()
                finally:
                    with self.lock:
                        self.jobs[identifier]['done'] = True
                        self.active = None
                        while len(self.jobs) > 16:
                            self.jobs.pop(next(iter(self.jobs)))

        try:
            threading.Thread(target=run, name=f'wamf-database-{kind}', daemon=True).start()
        except RuntimeError:
            with self.lock:
                self.jobs.pop(identifier)
                self.active = None
            raise
        return identifier

    def cancel(self, identifier):
        with self.lock:
            if identifier not in self.jobs or self.jobs[identifier]['done']:
                return False
            self.jobs[identifier]['cancel'].set()
            return True
