"""Durable restart requests and explicit media-hold acknowledgement; no UI."""

import hmac
from pathlib import Path

from app.database_lifecycle import database_consumer, require_exclusive
from app.database_restore import RestorePhase, _now, _target
from app.database_restore_application import apply_restore, target_head_transaction
from app.database_restore_storage import (
    RestoreStorageError,
    operation_store,
    restore_request_store,
)
from wamf_paths import get_database_restore_root

ACTIVE_REQUESTS = {'queued', 'claimed'}


def _read_request(store):
    try:
        return store.read()
    except FileNotFoundError:
        return None


def read_restore_request():
    if not (get_database_restore_root() / 'requests').exists():
        return None
    try:
        with restore_request_store() as store:
            return _read_request(store)
    except FileNotFoundError:
        return None


def queue_restore_request(identifier, token, bound_target_path):
    """Only queue a revalidated, confirmed operation. Never replaces a DB."""
    if not isinstance(token, str) or len(token) != 64 or any(char not in '0123456789abcdef' for char in token):
        raise RestoreStorageError('Invalid confirmation token')
    with database_consumer(bound_target_path), restore_request_store(create=True) as request_store:
        previous = _read_request(request_store)
        if previous is not None and previous['status'] in ACTIVE_REQUESTS:
            if (previous['restore_operation'] == identifier
                    and hmac.compare_digest(previous['confirmation_token'], token)):
                return False  # Idempotent refresh/retry: do not send another signal.
            raise RestoreStorageError('Another restore request is pending')
        with operation_store(identifier) as store:
            operation = store.read()
            if (operation['phase'] != RestorePhase.CONFIRMED or operation.get('application') is not None
                    or operation['target'] != _target(bound_target_path)
                    or not isinstance(token, str)
                    or not hmac.compare_digest(operation['plan']['confirmation_token'], token)):
                raise RestoreStorageError('Confirmed restore binding changed')
            request_store.write({
                'record_version': 1, 'operation_id': request_store.identifier,
                'restore_operation': identifier, 'confirmation_token': token,
                'target_path': str(Path(bound_target_path).resolve()), 'target': operation['target'],
                'requested_at': _now(), 'updated_at': _now(), 'status': 'queued', 'code': 'pending_restart',
            })
        return True


def consume_restore_request(lease):
    """Startup exclusive boundary only. Claim before apply; never replay claims."""
    require_exclusive(lease)
    if not (get_database_restore_root() / 'requests').exists():
        return None
    try:
        context = restore_request_store()
        request_store = context.__enter__()
    except FileNotFoundError:
        return None
    try:
        request = _read_request(request_store)
        if request is None or request['status'] not in ACTIVE_REQUESTS:
            return None
        identifier = request['restore_operation']
        if request['target_path'] != str(lease.path):
            request.update(status='failed', code='target_changed', updated_at=_now())
            request_store.write(request)
            raise RestoreStorageError('Pending restore targets a different bound database')
        if request['status'] == 'claimed':
            with operation_store(identifier) as store:
                operation = store.read()
                applied = operation.get('application') is not None
                if not applied:
                    operation.update(phase=RestorePhase.FAILED.value, code='interrupted_request',
                                     plan=None, confirmed_at=None, updated_at=_now())
                    store.write(operation)
            request.update(status='consumed' if applied else 'failed',
                           code='reconciled' if applied else 'interrupted_request', updated_at=_now())
            request_store.write(request)
            return None
        # Reconciliation already ran before this call. Exactly one caller may
        # cross from queued to claimed; crashes after this point never reapply.
        request.update(status='claimed', code='applying', updated_at=_now())
        request_store.write(request)
        result = apply_restore(lease, identifier, request['confirmation_token'])
        request.update(status='consumed' if result.success else 'failed',
                       code=result.code.value, updated_at=_now())
        request_store.write(request)
        if not result.success:
            raise RestoreStorageError('Restore application failed; inspect durable operation status')
        return result
    finally:
        context.__exit__(None, None, None)


def acknowledge_retention_hold(bound_path, expected_operation_id):
    """Clear only the completed restore's hold; preserve retention history."""
    with database_consumer(bound_path), restore_request_store(create=True) as request_store:
        pending = _read_request(request_store)
        if pending is not None and pending['status'] in ACTIVE_REQUESTS:
            raise RestoreStorageError('A restore restart is pending')
        with target_head_transaction(bound_path) as head:
            if head['active_operation'] != expected_operation_id:
                raise RestoreStorageError('Restore hold identity changed')
            with operation_store(expected_operation_id) as store:
                operation = store.read()
                if operation['phase'] != RestorePhase.COMPLETE:
                    raise RestoreStorageError('Restore startup has not completed')
                if head['retention_hold'] is False:
                    return False
                timestamp = _now()
                operation['application']['hold_acknowledged_at'] = timestamp
                operation['updated_at'] = timestamp
                store.write(operation)
                head.update(retention_hold=False, hold_acknowledged_at=timestamp,
                            hold_acknowledged_operation=expected_operation_id)
                return True
