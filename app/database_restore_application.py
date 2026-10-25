"""Offline restore application and deterministic recovery. Never applies on import."""

import hmac
import os
import secrets
import stat
from contextlib import contextmanager
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path

from app import database_backup as package
from app.database_backup_service import create_backup, resolve_backup_download
from app.database_backup_storage import BackupStorageError, backup_operation_guard
from app.database_lifecycle import (
    exclusive_database_lifecycle,
    require_exclusive,
    require_lifecycle_owner,
)
from app.database_restore import (
    RestoreError,
    RestorePhase,
    _Budget,
    _copy,
    _now,
    _revalidate,
    _target,
)
from app.database_restore_storage import (
    RestoreStorageError,
    _lock,
    operation_store,
    private_file,
    target_store,
)
from app.db import ensure_schema
from app.db_inspection import DatabaseCompatibility, inspect_database
from wamf_paths import get_database_backup_root, get_database_restore_root


class ApplyCode(StrEnum):
    STARTUP_PENDING = 'startup_pending'
    COMPLETE = 'complete'
    PRECONDITION_FAILED = 'precondition_failed'
    PREPARATION_FAILED = 'preparation_failed'
    ROLLBACK_FAILED = 'rollback_preservation_failed'
    REPLACEMENT_FAILED = 'replacement_failed'
    RECOVERY_REQUIRED = 'recovery_required'
    DURABILITY_UNCERTAIN = 'durability_uncertain'
    STARTUP_FAILED = 'startup_failed'
    LIFECYCLE_REQUIRED = 'lifecycle_required'


MESSAGES = {
    ApplyCode.STARTUP_PENDING: 'Database replaced; startup validation is required.',
    ApplyCode.COMPLETE: 'Restored database startup succeeded.',
    ApplyCode.PRECONDITION_FAILED: 'Restore preconditions changed; a new preview and confirmation are required.',
    ApplyCode.PREPARATION_FAILED: 'Restore preparation failed; the original database was preserved.',
    ApplyCode.ROLLBACK_FAILED: 'A verified rollback artifact could not be preserved.',
    ApplyCode.REPLACEMENT_FAILED: 'Replacement failed; the original database was preserved.',
    ApplyCode.RECOVERY_REQUIRED: 'Database state requires explicit offline recovery.',
    ApplyCode.DURABILITY_UNCERTAIN: 'Replacement occurred; directory durability is uncertain.',
    ApplyCode.STARTUP_FAILED: 'Restored database startup failed; explicit offline recovery is required.',
    ApplyCode.LIFECYCLE_REQUIRED: 'Exclusive database lifecycle ownership is required.',
}
PRE_REPLACEMENT = {RestorePhase.PREPARING, RestorePhase.MIGRATING, RestorePhase.ROLLBACK_READY}
POST_REPLACEMENT = {RestorePhase.REPLACED, RestorePhase.STARTUP_PENDING}


@dataclass(frozen=True)
class ApplicationResult:
    operation_id: str
    phase: RestorePhase
    code: ApplyCode
    message: str
    replacement_occurred: bool | None
    rollback_artifact_id: str | None
    rollback_package_sha256: str | None
    restored_db_sha256: str | None
    migration_performed: bool
    durability_warning: bool
    recovery_required: bool
    retention_hold_active: bool
    recovery_artifact_id: str | None = None
    recovery_package_sha256: str | None = None

    @property
    def success(self):
        return self.code in (ApplyCode.STARTUP_PENDING, ApplyCode.COMPLETE)


def _read_head(path):
    with target_store(path) as store:
        try:
            record = store.read()
        except FileNotFoundError as exc:
            raise RestoreStorageError('Incomplete target journal') from exc
        if record['target_path'] != str(Path(path).resolve()):
            raise RestoreStorageError('Target journal identity mismatch')
        return record


def _head(path):
    if not get_database_restore_root().exists():
        return None
    try:
        return _read_head(path)
    except FileNotFoundError:
        return None


@contextmanager
def target_head_transaction(path):
    with (target_store(path, create=True) as store,
          _lock(store.directory, '.state.lock', wait=True)):
        try:
            record = store.read()
        except FileNotFoundError:
            record = {'record_version': 1, 'operation_id': store.identifier,
                      'target_path': str(Path(path).resolve()), 'active_operation': None,
                      'retention_hold': False, 'runtime_allowed': False}
        yield record
        record['updated_at'] = _now()
        store.write(record)


def _write_head(path, **changes):
    with target_head_transaction(path) as record:
        record.update(changes)


def database_access_allowed(path):
    try:
        head = _head(path)
        if head is None:
            return True
        return head['runtime_allowed'] is True and _identity(Path(path)) == head.get('runtime_identity')
    except (OSError, RestoreStorageError, BackupStorageError, KeyError, ValueError):
        return False


def retention_hold_active(path):
    """Fail closed for corrupt journals; never use retention_status as policy."""
    try:
        head = _head(path)
        return bool(head and head['retention_hold'])
    except (OSError, RestoreStorageError, BackupStorageError, KeyError, ValueError):
        return True


def _save(store, record, phase, code, **changes):
    record['application'].update(changes)
    record.update(phase=phase.value, code=code.value, updated_at=_now())
    store.write(record)
    return _result(record)


def _result(record):
    app = record.get('application') or {}
    try:
        code = ApplyCode(record['code'])
    except ValueError:
        code = ApplyCode.PRECONDITION_FAILED
    return ApplicationResult(record['operation_id'], RestorePhase(record['phase']), code, MESSAGES[code],
                             app.get('replacement_occurred'), app.get('rollback_id'), app.get('rollback_sha256'),
                             app.get('final_sha256'), app.get('migration_performed', False),
                             app.get('durability_warning', False),
                             record['phase'] == RestorePhase.RECOVERY_REQUIRED,
                             retention_hold_active(record['target']['path']),
                             app.get('recovery_artifact_id'), app.get('recovery_artifact_sha256'))


def get_application_status(identifier):
    with operation_store(identifier) as store:
        return _result(store.read())


def _clean_database(path, *, allow_upgrade=False):
    # Never unlink SQLite sidecars. Even a persistent cold journal requires an
    # explicit maintenance decision outside this conservative application path.
    for suffix in ('-journal', '-wal', '-shm'):
        try:
            Path(f'{path}{suffix}').lstat()
        except FileNotFoundError:
            continue
        raise RestoreStorageError('Unsupported SQLite recovery or journal state')
    inspection = inspect_database(path, check_integrity=True)
    eligible = inspection.compatibility == DatabaseCompatibility.CURRENT or (
        allow_upgrade and inspection.automatic_upgrade_supported
        and inspection.compatibility in (DatabaseCompatibility.UPGRADEABLE, DatabaseCompatibility.LEGACY))
    if not eligible or not package.check_staged_integrity(path):
        raise RestoreStorageError('Database is not healthy and current')
    return inspection


def _facts(path):
    with path.open('rb') as file:
        return _copy(file, None, package.MAX_DATABASE_BYTES, _Budget(300, None))


def _identity(path):
    return _target(path)


def _same(path, identity, facts):
    return _identity(path) == identity and _facts(path) == tuple(facts)


@contextmanager
def _working_directory(target):
    name = f'.wamf-restore-{secrets.token_hex(16)}'
    parent = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    descriptor = None
    try:
        os.mkdir(name, mode=0o700, dir_fd=parent)
        descriptor = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
        os.fchmod(descriptor, 0o700)
        yield parent, descriptor, target.parent / name / 'working.db'
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(parent)


def _cleanup_working(path):
    """Only disposable fixed files in our generated private working directory."""
    if not path.parent.name.startswith('.wamf-restore-'):
        raise RestoreStorageError('Unsafe working directory')
    with os.scandir(path.parent) as files:
        for file in files:
            if file.name not in {'working.db', 'working.db.lifecycle.lock', 'working.db-journal'}:
                raise RestoreStorageError('Unexpected working artifact')
            info = file.stat(follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise RestoreStorageError('Unsafe working artifact')
            os.unlink(file.path)
    path.parent.rmdir()


def _prepare(store, record, path, directory, *, rollback=False):
    budget = _Budget(300, None)
    if rollback:
        app = record['application']
        with resolve_backup_download(app['rollback_id'], get_database_backup_root()) as download:
            if download.package_sha256 != app['rollback_sha256']:
                raise RestoreStorageError('Rollback package changed')
            # Reuse the isolated validator/retained candidate path, not uploaded
            # SQL or migration logic. The validated archive has fixed members.
            import zipfile

            with (zipfile.ZipFile(download.file) as archive,
                  archive.open(package.DATABASE_MEMBER) as source,
                  private_file(directory, 'working.db', create=True) as destination):
                _copy(source, destination, package.MAX_DATABASE_BYTES, budget)
        inspection = inspect_database(path, check_integrity=True)
        if inspection.compatibility != DatabaseCompatibility.CURRENT and inspection.automatic_upgrade_supported:
            _save(store, record, RestorePhase.MIGRATING, ApplyCode.STARTUP_PENDING)
            ensure_schema(path)
            record['application']['migration_performed'] = True
    else:
        with (private_file(store.directory, 'candidate.db') as source,
              private_file(directory, 'working.db', create=True) as destination):
            _copy(source, destination, package.MAX_DATABASE_BYTES, budget)
        if record['plan']['migration_required']:
            _save(store, record, RestorePhase.MIGRATING, ApplyCode.STARTUP_PENDING)
            ensure_schema(path)
            record['application']['migration_performed'] = True
    _clean_database(path)
    with path.open('rb') as file:
        permissions = record['application']['permissions']
        os.fchown(file.fileno(), permissions['uid'], permissions['gid'])
        os.fchmod(file.fileno(), permissions['mode'])
        os.fsync(file.fileno())
    return _facts(path), _identity(path)


def _preserve_active(lease):
    backup = create_backup(lease.path, get_database_backup_root(), lifecycle_owner=lease)
    if not backup.success or backup.durability_warning:
        raise RestoreStorageError('Rollback preservation failed')
    with resolve_backup_download(backup.backup_id, get_database_backup_root()) as verified:
        if verified.package_sha256 != backup.package_sha256:
            raise RestoreStorageError('Rollback verification failed')
    return backup


def _replace_active(lease, parent, directory, record):
    app = record['application']
    _clean_database(lease.path)
    if not _same(lease.path, app['old_identity'], app['old_facts']):
        raise RestoreStorageError('Active database changed immediately before replacement')
    parent_info = os.fstat(parent)
    if ((parent_info.st_dev, parent_info.st_ino) !=
            (app['old_identity']['parent_device'], app['old_identity']['parent_inode'])):
        raise RestoreStorageError('Target parent changed')
    os.replace('working.db', lease.path.name, src_dir_fd=directory, dst_dir_fd=parent)


def _classify_active(path, app):
    try:
        identity = _identity(path)
        facts = _facts(path)
        _clean_database(path, allow_upgrade=bool(app.get('pre_replacement_authoritative')
                                                or app.get('startup_validation_passed')))
        if identity == app['old_identity'] and (facts == tuple(app['old_facts'])
                                                or app.get('pre_replacement_authoritative') is True):
            return 'old'
        final_identity = app.get('final_identity')
        if (final_identity is not None and identity == final_identity
                and (facts == (app['final_size'], app['final_sha256'])
                     or app.get('startup_validation_passed') is True)):
            return 'new'
    except (OSError, ValueError, RestoreStorageError):
        pass
    return 'uncertain'


def _apply(lease, identifier, token, *, rollback=False):
    require_exclusive(lease)
    target = lease.path
    with backup_operation_guard(), operation_store(identifier) as store:
        record = store.read()
        if record['target']['path'] != str(target):
            raise _PreconditionRejected
        prior = record.get('application')
        if rollback:
            if prior is None or not prior.get('replacement_occurred') or not prior.get('rollback_id'):
                raise _PreconditionRejected
            expected = _identity(target)
            if token != expected:
                raise _PreconditionRejected
        else:
            if record['phase'] != RestorePhase.CONFIRMED or prior is not None:
                raise _PreconditionRejected
            plan = _revalidate(store, record, _Budget(300, None), target)
            if not isinstance(token, str) or not hmac.compare_digest(plan.confirmation_token, token):
                raise _PreconditionRejected
        _clean_database(target)
        old_identity, old_facts = _identity(target), _facts(target)
        permissions = target.stat()
        if permissions.st_uid != os.geteuid():
            raise _PreconditionRejected
        app = {'kind': 'rollback' if rollback else 'restore', 'old_identity': old_identity,
               'old_facts': old_facts, 'replacement_occurred': False, 'migration_performed': False,
               'permissions': {'uid': permissions.st_uid, 'gid': permissions.st_gid,
                               'mode': stat.S_IMODE(permissions.st_mode)},
               'rollback_id': prior['rollback_id'] if rollback else None,
               'rollback_sha256': prior['rollback_sha256'] if rollback else None,
               'previous_application': prior if rollback else None}
        # Head is durable before claiming the operation. Startup also recognizes
        # a confirmed record with this head as a clearly pre-replacement crash.
        _write_head(target, active_operation=identifier, runtime_allowed=False, initial_application=app)
        record['application'] = app
        _save(store, record, RestorePhase.PREPARING, ApplyCode.STARTUP_PENDING)
        working = None
        try:
            with _working_directory(target) as (parent, directory, working):
                app['working_path'] = str(working)
                _save(store, record, RestorePhase.PREPARING, ApplyCode.STARTUP_PENDING)
                facts, identity = _prepare(store, record, working, directory, rollback=rollback)
                # Expected identity after rename has the target name/parent but
                # the working inode. Capture it in the authenticated intent.
                identity.update(path=str(target), parent_device=old_identity['parent_device'],
                                parent_inode=old_identity['parent_inode'])
                app.update(final_size=facts[0], final_sha256=facts[1], final_identity=identity,
                           working_path=str(working))
                try:
                    preserved = _preserve_active(lease)
                except (RestoreStorageError, OSError) as exc:
                    raise _RollbackFailure from exc
                if rollback:
                    app.update(recovery_artifact_id=preserved.backup_id,
                               recovery_artifact_sha256=preserved.package_sha256)
                else:
                    app.update(rollback_id=preserved.backup_id, rollback_sha256=preserved.package_sha256,
                               rollback_db_sha256=preserved.facts.database_sha256)
                if not _same(target, old_identity, old_facts):
                    raise RestoreStorageError('Target changed after rollback preservation')
                _write_head(target, retention_hold=True)
                _save(store, record, RestorePhase.ROLLBACK_READY, ApplyCode.STARTUP_PENDING)
                _save(store, record, RestorePhase.REPLACING, ApplyCode.STARTUP_PENDING)
                _replace_active(lease, parent, directory, record)
                app['replacement_occurred'] = True
                try:
                    os.fsync(parent)
                except OSError:
                    app['durability_warning'] = True
                    return _save(store, record, RestorePhase.RECOVERY_REQUIRED, ApplyCode.DURABILITY_UNCERTAIN)
                _save(store, record, RestorePhase.REPLACED, ApplyCode.STARTUP_PENDING)
                return _save(store, record, RestorePhase.STARTUP_PENDING, ApplyCode.STARTUP_PENDING)
        except (OSError, RestoreStorageError, RestoreError, RuntimeError, _RollbackFailure) as exc:
            observed = _classify_active(target, app)
            if observed == 'new':
                app['replacement_occurred'] = True
                return _save(store, record, RestorePhase.RECOVERY_REQUIRED, ApplyCode.RECOVERY_REQUIRED)
            if observed == 'old':
                code = (ApplyCode.ROLLBACK_FAILED if isinstance(exc, _RollbackFailure) else
                        ApplyCode.REPLACEMENT_FAILED if record['phase'] == RestorePhase.REPLACING else
                        ApplyCode.PREPARATION_FAILED)
                result = _save(store, record, RestorePhase.FAILED, code,
                               pre_replacement_authoritative=True)
                _write_head(target, runtime_allowed=True, runtime_identity=_identity(target))
                return result
            return _save(store, record, RestorePhase.RECOVERY_REQUIRED, ApplyCode.RECOVERY_REQUIRED,
                         replacement_occurred=None)
        finally:
            # A simulated/real crash leaves intent/evidence intact. Never clean
            # application-owned evidence after an uncertain rename.
            if (working is not None and record['phase'] in (RestorePhase.FAILED, RestorePhase.STARTUP_PENDING)
                    and working.parent.exists()):
                _cleanup_working(working)


class _RollbackFailure(Exception):
    pass


class _PreconditionRejected(RestoreStorageError):
    pass


def apply_restore(lease, identifier, confirmation_token):
    """Explicit offline operation; never called by startup reconciliation."""
    require_exclusive(lease)
    try:
        return _apply(lease, identifier, confirmation_token)
    except _PreconditionRejected:
        return _precondition_failure(lease, identifier, preserve=True)
    except (RestoreError, RestoreStorageError, BackupStorageError, OSError, RuntimeError):
        return _precondition_failure(lease, identifier)


def rollback_restore(lease, identifier, *, expected_target_identity):
    """Explicit offline rollback; preserve the DB being replaced as recovery media."""
    require_exclusive(lease)
    try:
        return _apply(lease, identifier, expected_target_identity, rollback=True)
    except _PreconditionRejected:
        return _precondition_failure(lease, identifier, preserve=True)
    except (RestoreError, RestoreStorageError, BackupStorageError, OSError, RuntimeError):
        return _precondition_failure(lease, identifier)


def _precondition_failure(lease, identifier, *, preserve=False):
    try:
        with operation_store(identifier) as store:
            record = store.read()
            if preserve:
                return replace(_result(record), code=ApplyCode.PRECONDITION_FAILED,
                               message=MESSAGES[ApplyCode.PRECONDITION_FAILED])
            if record.get('application') is None:
                head = _head(lease.path)
                record['application'] = (head['initial_application'] if head is not None
                                         and head['active_operation'] == identifier else
                                         {'replacement_occurred': False, 'migration_performed': False})
                return _save(store, record, RestorePhase.FAILED, ApplyCode.PRECONDITION_FAILED)
            app = record['application']
            observed = _classify_active(lease.path, app)
            if observed != 'old':
                app['replacement_occurred'] = True if observed == 'new' else None
                try:
                    return _save(store, record, RestorePhase.RECOVERY_REQUIRED, ApplyCode.RECOVERY_REQUIRED)
                except OSError:
                    record.update(phase=RestorePhase.RECOVERY_REQUIRED, code=ApplyCode.RECOVERY_REQUIRED)
                    return _result(record)
            return replace(_result(record), code=ApplyCode.PRECONDITION_FAILED,
                           message=MESSAGES[ApplyCode.PRECONDITION_FAILED])
    except (RestoreStorageError, OSError, ValueError):
        return ApplicationResult(identifier, RestorePhase.RECOVERY_REQUIRED, ApplyCode.RECOVERY_REQUIRED,
                                 MESSAGES[ApplyCode.RECOVERY_REQUIRED], None, None, None, None,
                                 False, False, True, retention_hold_active(lease.path))


def offline_target_identity(lease):
    require_exclusive(lease)
    return _identity(lease.path)


def reconcile_restore_startup(lease, *, explicit_recovery=False):
    """Reconcile intent, never reapply. Missing/ambiguous databases refuse startup."""
    require_exclusive(lease)
    head = _head(lease.path)
    if head is None or head['active_operation'] is None:
        return None
    with operation_store(head['active_operation']) as store:
        record = store.read()
        if record['target']['path'] != str(lease.path):
            raise RestoreStorageError('Recovery target changed')
        if record['phase'] == RestorePhase.COMPLETE:
            try:
                identity = _identity(lease.path)
                final = record['application']['final_identity']
                if identity['inode'] != final['inode'] or identity['device'] != final['device']:
                    raise RestoreStorageError('Restored database identity changed')
                _clean_database(lease.path, allow_upgrade=True)
                return _result(record)
            except (OSError, ValueError, RestoreStorageError) as exc:
                _save(store, record, RestorePhase.RECOVERY_REQUIRED, ApplyCode.RECOVERY_REQUIRED)
                _write_head(lease.path, runtime_allowed=False)
                raise RestoreStorageError('Completed restore database needs recovery') from exc
        if record.get('application') is None:
            record['application'] = head['initial_application']
        app = record['application']
        if record['phase'] == RestorePhase.RECOVERY_REQUIRED and not explicit_recovery:
            raise RestoreStorageError('Explicit offline recovery is required')
        observed = _classify_active(lease.path, app)
        if observed == 'old':
            code = ApplyCode(record['code']) if record['phase'] == RestorePhase.FAILED else ApplyCode.PREPARATION_FAILED
            result = _save(store, record, RestorePhase.FAILED, code,
                           replacement_occurred=False, pre_replacement_authoritative=True)
            _write_head(lease.path, runtime_allowed=True, runtime_identity=_identity(lease.path))
            return result
        if observed == 'new':
            return _save(store, record, RestorePhase.STARTUP_PENDING, ApplyCode.STARTUP_PENDING,
                         replacement_occurred=True)
        _save(store, record, RestorePhase.RECOVERY_REQUIRED, ApplyCode.RECOVERY_REQUIRED,
              replacement_occurred=None)
        raise RestoreStorageError('Ambiguous database replacement requires offline recovery')


def finish_restore_startup(lease, *, succeeded):
    """Supervisor calls after startup validation; failures never auto-rollback."""
    require_lifecycle_owner(lease)
    head = _head(lease.path)
    if head is None or head['active_operation'] is None:
        return None
    with operation_store(head['active_operation']) as store:
        record = store.read()
        if record['phase'] not in POST_REPLACEMENT:
            return _result(record)
        if succeeded:
            result = _save(store, record, RestorePhase.COMPLETE, ApplyCode.COMPLETE)
            _write_head(lease.path, runtime_allowed=True, runtime_identity=_identity(lease.path))
            return result
        _write_head(lease.path, runtime_allowed=False)
        return _save(store, record, RestorePhase.RECOVERY_REQUIRED, ApplyCode.STARTUP_FAILED,
                     startup_failed=True)


class DatabaseStartup:
    def __init__(self, lease):
        self.lease, self.succeeded = lease, False

    def allow_consumers(self):
        require_exclusive(self.lease)
        head = _head(self.lease.path)
        if head is not None:
            with operation_store(head['active_operation']) as store:
                record = store.read()
                if record['phase'] not in POST_REPLACEMENT | {RestorePhase.COMPLETE, RestorePhase.FAILED}:
                    raise RestoreStorageError('Startup recovery has not been reconciled')
                if _classify_active(self.lease.path, record['application']) not in ('old', 'new'):
                    raise RestoreStorageError('Startup database identity is uncertain')
                if record['phase'] in POST_REPLACEMENT:
                    _clean_database(self.lease.path)
                    record['application']['startup_validation_passed'] = True
                    store.write(record)
            _write_head(self.lease.path, runtime_allowed=True, runtime_identity=_identity(self.lease.path))
        self.lease.downgrade()

    def mark_succeeded(self):
        finish_restore_startup(self.lease, succeeded=True)
        self.succeeded = True


@contextmanager
def database_startup(path):
    """Native/standalone startup gate, held through supervisor/consumer lifetime."""
    with exclusive_database_lifecycle(path) as lease:
        startup = DatabaseStartup(lease)
        reconcile_restore_startup(lease)
        from app.database_restore_coordination import consume_restore_request

        consume_restore_request(lease)
        try:
            yield startup
        finally:
            if not startup.succeeded:
                finish_restore_startup(lease, succeeded=False)
