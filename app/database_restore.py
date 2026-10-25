"""Durable restore staging and confirmation; offline application is separate."""

import hashlib
import hmac
import json
import math
import os
import stat
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from app import database_backup as package
from app.database_backup_service import resolve_backup_download
from app.database_backup_storage import BackupIdentity, BackupStorageError
from app.database_restore_storage import (
    RestoreBusyError,
    RestoreStorageError,
    operation_store,
    private_file,
)
from app.db_inspection import (
    DatabaseCompatibility,
    DatabaseInspection,
    InspectionError,
    MigrationHistoryEntry,
)
from wamf_paths import DatabaseBackupPathError, get_database_backup_root

MEDIA_WARNING = (
    'Database-only restore: snapshots and clips are not restored. Older database '
    'references may have missing media; newer media may become orphaned. '
    'Destructive retention must be held until acknowledgement after restore.'
)
DEFAULT_TIMEOUT_SECONDS = 120.0


class RestorePhase(StrEnum):
    STAGING = 'staging'
    VALIDATING = 'validating'
    READY = 'ready'
    REJECTED = 'rejected'
    CONFIRMED = 'confirmed'
    CANCELLED = 'cancelled'
    FAILED = 'failed'
    PREPARING = 'preparing'
    MIGRATING = 'migrating'
    ROLLBACK_READY = 'rollback_ready'
    REPLACING = 'replacing'
    REPLACED = 'replaced'
    STARTUP_PENDING = 'startup_pending'
    COMPLETE = 'complete'
    RECOVERY_REQUIRED = 'recovery_required'


# Deliberately explicit: adding future application phases to the enum must not
# automatically grant cancellation permission over application-owned artifacts.
CANCELLABLE_PHASES = frozenset({
    RestorePhase.STAGING, RestorePhase.VALIDATING, RestorePhase.READY,
    RestorePhase.REJECTED, RestorePhase.CONFIRMED, RestorePhase.CANCELLED, RestorePhase.FAILED,
})


class RestoreCode(StrEnum):
    IN_PROGRESS = 'in_progress'
    READY = 'ready'
    CONFIRMED = 'confirmed'
    CANCELLED = 'cancelled'
    INVALID_INPUT = 'invalid_input'
    UNSAFE_STORAGE = 'unsafe_storage'
    INVALID_RECORD = 'invalid_record'
    BUSY = 'operation_busy'
    PACKAGE_REJECTED = 'package_rejected'
    INPUT_TOO_LARGE = 'input_too_large'
    DEADLINE = 'deadline_exceeded'
    WORKER_FAILED = 'validation_worker_failed'
    ARTIFACT_CHANGED = 'artifact_changed'
    TARGET_CHANGED = 'target_changed'
    CONFIRMATION_INVALID = 'confirmation_invalid'
    INVALID_STATE = 'invalid_state'
    IO_ERROR = 'storage_error'


class RestoreError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code.value)


@dataclass(frozen=True)
class TargetIdentity:
    device: int
    inode: int
    binding_digest: str


@dataclass(frozen=True)
class RestorePlan:
    operation_id: str
    source_backup_id: str | None
    source_package_sha256: str
    candidate_sha256: str
    candidate_size: int
    target: TargetIdentity
    compatibility: DatabaseCompatibility
    detected_sequence: int | None
    detected_migration_id: str | None
    latest_sequence: int
    latest_migration_id: str
    migration_required: bool
    automatic_upgrade_supported: bool
    wamf_version: str
    sqlite_version: str
    created_at: str
    media_warning: str
    confirmation_token: str


@dataclass(frozen=True)
class RestoreResult:
    operation_id: str | None
    phase: RestorePhase
    code: RestoreCode
    plan: RestorePlan | None = None
    validation_code: package.ValidationCode | None = None

    @property
    def success(self):
        return self.code in (RestoreCode.READY, RestoreCode.CONFIRMED, RestoreCode.CANCELLED)


def _now():
    return datetime.now(UTC).isoformat(timespec='microseconds').replace('+00:00', 'Z')


class _Budget:
    def __init__(self, timeout, cancelled):
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 <= timeout <= 300:
            raise RestoreError(RestoreCode.INVALID_INPUT)
        self.deadline = time.monotonic() + timeout
        self.cancelled = cancelled

    def check(self):
        if self.cancelled is not None and self.cancelled():
            raise RestoreError(RestoreCode.CANCELLED)
        if time.monotonic() >= self.deadline:
            raise RestoreError(RestoreCode.DEADLINE)


def _target(path):
    """Stat only: never open/close a raw active DB FD or a SQLite connection."""
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts:
        raise RestoreError(RestoreCode.INVALID_INPUT)
    descriptors = []
    try:
        directory = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
        descriptors.append(directory)
        for component in path.parts[1:-1]:
            directory = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                dir_fd=directory)
            descriptors.append(directory)
        info = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
        parent = os.fstat(directory)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise RestoreError(RestoreCode.TARGET_CHANGED)
        return {'path': str(path), 'device': info.st_dev, 'inode': info.st_ino,
                'parent_device': parent.st_dev, 'parent_inode': parent.st_ino}
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _copy(source, destination, limit, budget):
    digest, size = hashlib.sha256(), 0
    while True:
        budget.check()
        chunk = source.read(min(package.CHUNK_BYTES, limit - size + 1))
        budget.check()
        if not isinstance(chunk, bytes):
            raise RestoreError(RestoreCode.INVALID_INPUT)
        if not chunk:
            break
        size += len(chunk)
        if size > limit:
            raise RestoreError(RestoreCode.INPUT_TOO_LARGE)
        digest.update(chunk)
        if destination is not None:
            destination.write(chunk)
    if destination is not None:
        destination.flush()
        os.fsync(destination.fileno())
    return size, digest.hexdigest()


def _decode_validation(data):
    manifest = (package.BackupManifest.from_bytes(json.dumps(data['manifest']).encode())
                if data['manifest'] is not None else None)
    facts = package.ArtifactFacts(**data['facts']) if data['facts'] is not None else None
    inspection = data['inspection']
    if inspection is not None:
        inspection['compatibility'] = DatabaseCompatibility(inspection['compatibility'])
        inspection['error_category'] = (InspectionError(inspection['error_category'])
                                        if inspection['error_category'] else None)
        inspection['history'] = tuple(MigrationHistoryEntry(**row) for row in inspection['history'])
        inspection = DatabaseInspection(**inspection)
    return package.PackageValidationResult(package.ValidationCode(data['code']), None, manifest,
                                           facts, inspection, data['full_integrity_checked'])


def _validate(store, budget):
    """The only untrusted ZIP/SQLite parsing boundary, in an owned child."""
    budget.check()
    with (private_file(store.directory, 'package.zip') as source,
          private_file(store.directory, 'candidate.db', create=True) as candidate):
        cpu = math.ceil(max(1, budget.deadline - time.monotonic()))
        process = subprocess.Popen(
            [sys.executable, '-B', '-m', 'app.database_restore_worker', str(os.getpid()),
             str(source.fileno()), str(candidate.fileno()), str(store.directory), str(cpu)],
            cwd=Path(__file__).resolve().parent.parent,
            pass_fds=(source.fileno(), candidate.fileno(), store.directory),
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        try:
            while True:
                budget.check()
                try:
                    output, _ = process.communicate(timeout=0.05)
                    break
                except subprocess.TimeoutExpired:
                    continue
            budget.check()
            if process.returncode != 0 or len(output) > 256 * 1024:
                raise RestoreError(RestoreCode.WORKER_FAILED)
            return _decode_validation(json.loads(output))
        except (ValueError, TypeError, KeyError, RecursionError) as exc:
            if isinstance(exc, RestoreError):
                raise
            raise RestoreError(RestoreCode.WORKER_FAILED) from exc
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate()


def _plan_from_dict(data):
    data = dict(data)
    data['target'] = TargetIdentity(**data['target'])
    data['compatibility'] = DatabaseCompatibility(data['compatibility'])
    return RestorePlan(**data)


def _artifact_identities(store):
    identities = {}
    for name in ('package.zip', 'candidate.db'):
        with private_file(store.directory, name) as file:
            info = os.fstat(file.fileno())
            identities[name] = {'device': info.st_dev, 'inode': info.st_ino}
    return identities


def _make_plan(store, record, validation):
    inspection, manifest, facts = validation.inspection, validation.manifest, validation.facts
    if (inspection.compatibility not in (DatabaseCompatibility.CURRENT, DatabaseCompatibility.UPGRADEABLE,
                                         DatabaseCompatibility.LEGACY)
            or (inspection.compatibility != DatabaseCompatibility.CURRENT
                and not inspection.automatic_upgrade_supported)
            or (inspection.compatibility == DatabaseCompatibility.LEGACY
                and not inspection.legacy_recognized)):
        raise RestoreError(RestoreCode.PACKAGE_REJECTED)
    target = record['target']
    public_target = TargetIdentity(target['device'], target['inode'],
                                   store.seal(target, purpose=b'target'))
    data = {'operation_id': store.identifier, 'source_backup_id': record['source_backup_id'],
                'source_package_sha256': record['source_package_sha256'], 'candidate_sha256': facts.database_sha256,
                'candidate_size': facts.database_size, 'target': asdict(public_target),
                'compatibility': inspection.compatibility, 'detected_sequence': inspection.recognized_sequence,
                'detected_migration_id': inspection.recognized_migration_id,
                'latest_sequence': inspection.latest_sequence, 'latest_migration_id': inspection.latest_migration_id,
                'migration_required': inspection.migration_required,
                'automatic_upgrade_supported': inspection.automatic_upgrade_supported,
                'wamf_version': manifest.wamf_version, 'sqlite_version': manifest.sqlite_version,
                'created_at': record['created_at'], 'media_warning': MEDIA_WARNING}
    data['confirmation_token'] = store.seal({'plan': data, 'target': target,
                                             'artifacts': record['artifacts']}, purpose=b'confirmation')
    return _plan_from_dict(data)


def _transition(store, record, phase, code, *, plan=None, validation_code=None):
    record.update(phase=phase.value, code=code.value, updated_at=_now(),
                  plan=asdict(plan) if plan else None,
                  confirmed_at=_now() if phase == RestorePhase.CONFIRMED else None,
                  validation_code=validation_code.value if validation_code else None)
    store.write(record)
    return RestoreResult(store.identifier, phase, code, plan, validation_code)


def _stage(bound_target_path, source_backup_id, receive, timeout_seconds, cancelled, progress=None):
    identifier = None
    try:
        budget = _Budget(timeout_seconds, cancelled)
        budget.progress = progress
        budget.check()
        target = _target(bound_target_path)
        with operation_store(create=True) as store:
            identifier = store.identifier
            record = {'record_version': 1, 'operation_id': identifier, 'phase': RestorePhase.STAGING.value,
                          'code': RestoreCode.IN_PROGRESS.value, 'created_at': _now(), 'updated_at': _now(),
                          'target': target, 'source_backup_id': source_backup_id, 'source_package_sha256': None,
                          'plan': None, 'confirmed_at': None, 'validation_code': None, 'artifacts': None}
            store.write(record)
            if progress is not None:
                progress(identifier, RestorePhase.STAGING)
            try:
                validation = receive(store, record, budget)
                if not validation.valid:
                    store.cleanup()
                    return _transition(store, record, RestorePhase.REJECTED, RestoreCode.PACKAGE_REJECTED,
                                       validation_code=validation.code)
                if _target(bound_target_path) != target:
                    raise RestoreError(RestoreCode.TARGET_CHANGED)
                record['artifacts'] = _artifact_identities(store)
                plan = _make_plan(store, record, validation)
                with private_file(store.directory, 'candidate.db') as file:
                    facts = _copy(file, None, package.MAX_DATABASE_BYTES, budget)
                if facts != (plan.candidate_size, plan.candidate_sha256):
                    raise RestoreError(RestoreCode.ARTIFACT_CHANGED)
                os.fsync(store.directory)
                return _transition(store, record, RestorePhase.READY, RestoreCode.READY, plan=plan)
            except (RestoreError, package.PackageValidationError, RestoreStorageError,
                    BackupStorageError, OSError) as exc:
                # Revoke before cleanup if staging/worker I/O failed. A cleanup
                # error must not leave a ready or confirmed operation behind.
                if isinstance(exc, package.PackageValidationError):
                    result = _transition(store, record, RestorePhase.REJECTED, RestoreCode.PACKAGE_REJECTED,
                                         validation_code=exc.code)
                else:
                    code = exc.code if isinstance(exc, RestoreError) else RestoreCode.IO_ERROR
                    phase = (RestorePhase.CANCELLED if code == RestoreCode.CANCELLED else
                             RestorePhase.REJECTED if code == RestoreCode.PACKAGE_REJECTED else RestorePhase.FAILED)
                    result = _transition(store, record, phase, code)
                store.cleanup()
                return result
    except RestoreError as exc:
        phase = RestorePhase.CANCELLED if exc.code == RestoreCode.CANCELLED else RestorePhase.FAILED
        return RestoreResult(identifier, phase, exc.code)
    except RestoreBusyError:
        return RestoreResult(identifier, RestorePhase.FAILED, RestoreCode.BUSY)
    except (RestoreStorageError, BackupStorageError, DatabaseBackupPathError):
        return RestoreResult(identifier, RestorePhase.FAILED, RestoreCode.UNSAFE_STORAGE)
    except OSError:
        return RestoreResult(identifier, RestorePhase.FAILED, RestoreCode.IO_ERROR)
    except (TypeError, ValueError):
        return RestoreResult(identifier, RestorePhase.FAILED, RestoreCode.INVALID_INPUT)


def _receive(store, record, source, budget):
    with private_file(store.directory, 'package.zip', create=True) as destination:
        _, digest = _copy(source, destination, package.MAX_PACKAGE_BYTES, budget)
    record['source_package_sha256'] = digest
    _transition(store, record, RestorePhase.VALIDATING, RestoreCode.IN_PROGRESS)
    if getattr(budget, 'progress', None) is not None:
        budget.progress(store.identifier, RestorePhase.VALIDATING)
    return _validate(store, budget)


def stage_restore_from_backup(backup_id, bound_target_path, *, timeout_seconds=DEFAULT_TIMEOUT_SECONDS,
                              cancelled=None, progress=None):
    try:
        BackupIdentity(backup_id)
    except BackupStorageError:
        return RestoreResult(None, RestorePhase.FAILED, RestoreCode.INVALID_INPUT)

    def receive(store, record, budget):
        def runner(identity, file):
            return _receive(store, record, file, budget)

        with resolve_backup_download(backup_id, get_database_backup_root(), validation_runner=runner,
                                     checkpoint=budget.check) as download:
            if download.package_sha256 != record['source_package_sha256']:
                raise RestoreError(RestoreCode.ARTIFACT_CHANGED)
            return download.validation

    return _stage(bound_target_path, backup_id, receive, timeout_seconds, cancelled, progress)


def stage_restore_from_stream(stream, bound_target_path, *, timeout_seconds=DEFAULT_TIMEOUT_SECONDS,
                              cancelled=None, progress=None):
    """Caller-owned bounded byte stream; server paths/filenames are not inputs."""
    if not callable(getattr(stream, 'read', None)):
        return RestoreResult(None, RestorePhase.FAILED, RestoreCode.INVALID_INPUT)
    return _stage(bound_target_path, None, lambda store, record, budget: _receive(store, record, stream, budget),
                  timeout_seconds, cancelled, progress)


def get_restore_plan(identifier):
    """Authenticated cached preview; confirmation always performs full revalidation."""
    try:
        with operation_store(identifier) as store:
            record = store.read()
            if RestorePhase(record['phase']) not in (RestorePhase.READY, RestorePhase.CONFIRMED):
                raise RestoreError(RestoreCode.INVALID_STATE)
            plan = _plan_from_dict(record['plan'])
            if _artifact_identities(store) != record['artifacts']:
                raise RestoreError(RestoreCode.ARTIFACT_CHANGED)
            return plan
    except (RestoreStorageError, BackupStorageError, OSError, KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, RestoreError):
            raise
        raise RestoreError(RestoreCode.INVALID_RECORD) from exc


def _revalidate(store, record, budget, bound_target_path):
    if RestorePhase(record['phase']) not in (RestorePhase.READY, RestorePhase.CONFIRMED):
        raise RestoreError(RestoreCode.INVALID_STATE)
    old = _plan_from_dict(record['plan'])
    if _artifact_identities(store) != record['artifacts']:
        raise RestoreError(RestoreCode.ARTIFACT_CHANGED)
    if bound_target_path is not None and str(Path(bound_target_path)) != record['target']['path']:
        raise RestoreError(RestoreCode.TARGET_CHANGED)
    if _target(record['target']['path']) != record['target']:
        raise RestoreError(RestoreCode.TARGET_CHANGED)
    with private_file(store.directory, 'package.zip') as file:
        _, digest = _copy(file, None, package.MAX_PACKAGE_BYTES, budget)
    with private_file(store.directory, 'candidate.db') as file:
        size, candidate = _copy(file, None, package.MAX_DATABASE_BYTES, budget)
    if (digest != old.source_package_sha256 or candidate != old.candidate_sha256 or size != old.candidate_size):
        raise RestoreError(RestoreCode.ARTIFACT_CHANGED)
    # Validate into a fresh candidate name through an exclusive operation-owned
    # scratch directory, never overwrite the preview's candidate.
    os.mkdir('.recheck', mode=0o700, dir_fd=store.directory)
    scratch = os.open('.recheck', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=store.directory)
    try:
        # The validator needs the same package in its workspace. A fresh streamed
        # copy avoids hard-link aliases and keeps every artifact single-link.
        with (private_file(store.directory, 'package.zip') as source,
              private_file(scratch, 'package.zip', create=True) as destination):
            _copy(source, destination, package.MAX_PACKAGE_BYTES, budget)
        from app.database_restore_storage import OperationStore

        validation = _validate(OperationStore(scratch, store.key, store.identifier), budget)
        if not validation.valid or _make_plan(store, record, validation) != old:
            raise RestoreError(RestoreCode.ARTIFACT_CHANGED)
        with private_file(scratch, 'candidate.db') as file:
            if _copy(file, None, package.MAX_DATABASE_BYTES, budget) != (size, candidate):
                raise RestoreError(RestoreCode.ARTIFACT_CHANGED)
        # Close the validation window: preview artifacts may not change while
        # the worker validates its independent scratch copy.
        with private_file(store.directory, 'candidate.db') as file:
            if _copy(file, None, package.MAX_DATABASE_BYTES, budget) != (size, candidate):
                raise RestoreError(RestoreCode.ARTIFACT_CHANGED)
        with private_file(store.directory, 'package.zip') as file:
            if _copy(file, None, package.MAX_PACKAGE_BYTES, budget)[1] != digest:
                raise RestoreError(RestoreCode.ARTIFACT_CHANGED)
        if _target(record['target']['path']) != record['target']:
            raise RestoreError(RestoreCode.TARGET_CHANGED)
        if _artifact_identities(store) != record['artifacts']:
            raise RestoreError(RestoreCode.ARTIFACT_CHANGED)
        return old
    finally:
        for name in ('package.zip', 'candidate.db', 'database.db'):
            try:
                os.unlink(name, dir_fd=scratch)
            except FileNotFoundError:
                pass
        os.close(scratch)
        os.rmdir('.recheck', dir_fd=store.directory)


def revalidate_restore_plan(identifier, *, bound_target_path=None, timeout_seconds=DEFAULT_TIMEOUT_SECONDS,
                             cancelled=None, confirmation_token=None):
    try:
        budget = _Budget(timeout_seconds, cancelled)
        with operation_store(identifier) as store:
            record = store.read()
            if RestorePhase(record['phase']) not in (RestorePhase.READY, RestorePhase.CONFIRMED):
                return RestoreResult(identifier, RestorePhase(record['phase']), RestoreCode.INVALID_STATE)
            try:
                plan = _revalidate(store, record, budget, bound_target_path)
                if confirmation_token is not None:
                    if not isinstance(confirmation_token, str) or not hmac.compare_digest(
                            plan.confirmation_token, confirmation_token):
                        raise RestoreError(RestoreCode.CONFIRMATION_INVALID)
                    return _transition(store, record, RestorePhase.CONFIRMED, RestoreCode.CONFIRMED, plan=plan)
                phase = RestorePhase(record['phase'])
                return RestoreResult(identifier, phase, RestoreCode(record['code']), plan)
            except (RestoreError, RestoreStorageError, BackupStorageError, OSError) as exc:
                code = exc.code if isinstance(exc, RestoreError) else RestoreCode.ARTIFACT_CHANGED
                phase = RestorePhase.CANCELLED if code == RestoreCode.CANCELLED else RestorePhase.REJECTED
                result = _transition(store, record, phase, code)
                if phase == RestorePhase.CANCELLED:
                    store.cleanup()
                return result
    except RestoreBusyError:
        return RestoreResult(identifier, RestorePhase.FAILED, RestoreCode.BUSY)
    except (RestoreStorageError, BackupStorageError, OSError, KeyError, TypeError, ValueError):
        return RestoreResult(identifier, RestorePhase.FAILED, RestoreCode.INVALID_RECORD)


def confirm_restore_plan(identifier, token, *, bound_target_path, **kwargs):
    return revalidate_restore_plan(identifier, bound_target_path=bound_target_path,
                                   confirmation_token=token if isinstance(token, str) else '', **kwargs)


def cancel_restore(identifier):
    try:
        with operation_store(identifier) as store:
            record = store.read()
            if record['phase'] not in CANCELLABLE_PHASES or record.get('application') is not None:
                raise RestoreError(RestoreCode.INVALID_STATE)
            # Persist revocation BEFORE deleting artifacts: crashes cannot leave
            # a confirmed/ready record pointing to intentionally removed files.
            result = _transition(store, record, RestorePhase.CANCELLED, RestoreCode.CANCELLED)
            store.cleanup()
            return result
    except RestoreBusyError:
        return RestoreResult(identifier, RestorePhase.FAILED, RestoreCode.BUSY)
    except RestoreError as exc:
        return RestoreResult(identifier, RestorePhase.FAILED, exc.code)
    except (RestoreStorageError, BackupStorageError, OSError, KeyError, TypeError, ValueError):
        return RestoreResult(identifier, RestorePhase.FAILED, RestoreCode.INVALID_RECORD)
