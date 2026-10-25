"""Session-authenticated System Health database tools. No route applies a DB."""

import hmac
import os
from dataclasses import asdict
from pathlib import Path

from flask import Blueprint, current_app, jsonify, request, send_file
from werkzeug.exceptions import RequestEntityTooLarge

from app import database_backup as package
from app.database_admin import DatabaseJobs, isolated_package_validation, restore_dto
from app.database_backup_service import list_backups, resolve_backup_download
from app.database_backup_storage import (
    backup_directory,
    check_artifact_entry,
    reserve_backup,
)
from app.database_restore import (
    RestoreCode,
    RestoreError,
    RestorePhase,
    _Budget,
    _copy,
    cancel_restore,
    confirm_restore_plan,
)
from app.database_restore_application import (
    _head,
    get_application_status,
    retention_hold_active,
)
from app.database_restore_coordination import (
    ACTIVE_REQUESTS,
    acknowledge_retention_hold,
    queue_restore_request,
    read_restore_request,
)
from app.database_restore_storage import operation_store
from app.process_control import INSTANCE_ID, native_restart_available, schedule_restart
from app.system_events import log_system_event
from wamf_paths import get_database_backup_root, get_database_path

database_admin_bp = Blueprint('database_admin', __name__, url_prefix='/admin/database')


def _bound():
    import webui

    if webui.DBPATH is None:
        raise ValueError('Runtime database is not bound')
    return Path(webui.DBPATH)


def _jobs():
    return current_app.extensions.setdefault('database_jobs', DatabaseJobs())


def _pending():
    pending = read_restore_request()
    return pending is not None and pending['status'] in ACTIVE_REQUESTS


def _operation(identifier):
    with operation_store(identifier) as store:
        record = store.read()
        if record['target']['path'] != str(_bound()):
            raise ValueError('Operation target differs from bound database')
        phase = record['phase']
        result = {'operation_id': identifier, 'phase': phase, 'code': record['code'], 'plan': None}
        if record.get('application') is not None:
            status = get_application_status  # Load after releasing operation mutex below.
        else:
            status = None
        if phase in (RestorePhase.READY, RestorePhase.CONFIRMED):
            result['plan'] = record['plan']
        result['validation_code'] = record.get('validation_code')
    if status is not None:
        result['application'] = asdict(status(identifier))
    pending = read_restore_request()
    if pending and pending['restore_operation'] == identifier and pending['status'] in ACTIVE_REQUESTS:
        result['phase'] = 'pending_restart' if pending['status'] == 'queued' else 'applying'
    return result


@database_admin_bp.after_request
def no_store(response):
    response.headers['Cache-Control'] = 'no-store'
    return response


@database_admin_bp.errorhandler(ValueError)
@database_admin_bp.errorhandler(OSError)
@database_admin_bp.errorhandler(RuntimeError)
def safe_failure(error):
    return jsonify(success=False, code='operation_unavailable',
                   message='Database operation is unavailable. Refresh status or consult offline recovery guidance.'), 409


@database_admin_bp.errorhandler(RequestEntityTooLarge)
def too_large(error):
    return jsonify(success=False, code='upload_too_large', message='Package exceeds the upload limit.'), 413


@database_admin_bp.get('/status')
def database_status():
    target = _bound()
    head = _head(target)
    pending = read_restore_request()
    operation_id = pending['restore_operation'] if pending and pending['target_path'] == str(target) else None
    return {'instance_id': INSTANCE_ID, 'native_restart': native_restart_available(),
            'job': _jobs().snapshot(), 'operation': _operation(operation_id) if operation_id else None,
            'retention_hold': retention_hold_active(target),
            'hold_operation_id': head['active_operation'] if head else None}


@database_admin_bp.get('/backups')
def database_backups():
    entries = list_backups(get_database_backup_root(), validation_runner=isolated_package_validation)
    return {'backups': [{
        'backup_id': entry.backup_id, 'size': entry.size, 'timestamp': entry.timestamp,
        'valid': entry.validation.valid, 'code': entry.validation.code.value,
        'compatibility': entry.compatibility.value if entry.compatibility else None,
        'schema': entry.validation.inspection.recognized_migration_id if entry.validation.inspection else None,
        'wamf_version': entry.manifest.wamf_version if entry.manifest else None,
        'checksum': entry.package_sha256,
    } for entry in entries]}


@database_admin_bp.post('/backups')
def database_create_backup():
    if _pending():
        raise ValueError('Restore pending')
    identifier = _jobs().start('backup', _bound())
    return {'job_id': identifier}, 202


@database_admin_bp.get('/backups/<backup_id>/download')
def database_download(backup_id):
    context = resolve_backup_download(backup_id, get_database_backup_root(),
                                      validation_runner=isolated_package_validation)
    try:
        download = context.__enter__()
    except (ValueError, OSError, RuntimeError):
        return {'success': False, 'code': 'invalid_artifact', 'message': 'Backup is unavailable or invalid.'}, 404
    try:
        response = send_file(download.file, mimetype='application/zip', as_attachment=True,
                             download_name=download.identity.filename, conditional=False)
        response.content_length = download.size
        response.direct_passthrough = False
        response.call_on_close(lambda: context.__exit__(None, None, None))
        return response
    except BaseException:
        context.__exit__(None, None, None)
        raise


@database_admin_bp.post('/restore/stage')
def database_stage_backup():
    if _pending():
        raise ValueError('Restore pending')
    data = request.get_json() or {}
    identifier = _jobs().start('staging', _bound(), backup_id=data.get('backup_id'))
    return {'job_id': identifier}, 202


@database_admin_bp.post('/restore/upload')
def database_upload():
    if _pending():
        raise ValueError('Restore pending')
    # Native Werkzeug provides a socket; production WSGI/proxy deployments must
    # also enforce transport read deadlines. The streamed ZIP budget is total.
    socket = request.environ.get('werkzeug.socket')
    previous_timeout = socket.gettimeout() if socket is not None else None
    if socket is not None:
        socket.settimeout(30)
    try:
        return _receive_upload()
    finally:
        if socket is not None:
            socket.settimeout(previous_timeout)


def _receive_upload():
    budget = _Budget(120, None)
    if request.mimetype in ('application/zip', 'application/octet-stream'):
        source = request.stream
    else:
        uploaded = request.files.get('package')
        if uploaded is None:
            return {'success': False, 'code': 'missing_package'}, 400
        source = uploaded.stream
    reservation = reserve_backup()
    identity, file = reservation.__enter__()
    try:
        _copy(source, file, package.MAX_PACKAGE_BYTES, budget)
        file.seek(0)

        def cleanup():
            try:
                check_artifact_entry(identity, file, staged=True)
                with backup_directory(staging=True) as directory:
                    os.unlink(identity.filename, dir_fd=directory)
            finally:
                reservation.__exit__(None, None, None)

        identifier = _jobs().start('staging', _bound(), stream=file, cleanup=cleanup)
        return {'job_id': identifier}, 202
    except BaseException as exc:
        # Reuse reservation's inode-aware failure cleanup; no supplied filename.
        reservation.__exit__(RuntimeError, RuntimeError('upload interrupted'), None)
        if isinstance(exc, RestoreError) and exc.code == RestoreCode.INPUT_TOO_LARGE:
            raise RequestEntityTooLarge from exc
        raise


@database_admin_bp.get('/jobs/<identifier>')
def database_job(identifier):
    snapshot = _jobs().snapshot(identifier)
    return snapshot if snapshot is not None else ({'code': 'job_interrupted'}, 404)


@database_admin_bp.post('/jobs/<identifier>/cancel')
def database_cancel_job(identifier):
    return {'success': _jobs().cancel(identifier)}


@database_admin_bp.get('/operations/<identifier>')
def database_operation(identifier):
    return _operation(identifier)


@database_admin_bp.post('/operations/<identifier>/cancel')
def database_cancel_restore(identifier):
    _operation(identifier)
    pending = read_restore_request()
    if pending and pending['restore_operation'] == identifier and pending['status'] in ACTIVE_REQUESTS:
        raise ValueError('Restart pending')
    return restore_dto(cancel_restore(identifier))


@database_admin_bp.post('/operations/<identifier>/restart')
def database_restore_restart(identifier):
    import webui
    from app.config_loader import load_runtime_config
    from app.config_validation import validate_config

    if not native_restart_available():
        return {'success': False, 'code': 'native_restart_required',
                'message': 'Restore requires the native WAMF supervisor.'}, 409
    data = request.get_json() or {}
    token = data.get('confirmation_token')
    if (data.get('destructive_confirmation') is not True or not isinstance(token, str) or len(token) != 64
            or any(char not in '0123456789abcdef' for char in token)):
        return {'success': False, 'code': 'confirmation_required'}, 400
    saved = load_runtime_config()
    if not validate_config(saved).is_valid or get_database_path(saved).resolve() != _bound():
        return {'success': False, 'code': 'configuration_changed',
                'message': 'Saved configuration and bound database must agree before restoring.'}, 409
    _operation(identifier)
    with _jobs().reserve_restart():
        pending = read_restore_request()
        if pending and pending['restore_operation'] == identifier and pending['status'] in ACTIVE_REQUESTS:
            if pending['target_path'] != str(_bound()) or not hmac.compare_digest(pending['confirmation_token'], token):
                return {'success': False, 'code': 'confirmation_invalid'}, 409
            return {'success': True, 'phase': 'pending_restart', 'instance_id': INSTANCE_ID}
        confirmed = confirm_restore_plan(identifier, token, bound_target_path=webui.DBPATH)
        if not confirmed.success:
            return restore_dto(confirmed), 409
        queued = queue_restore_request(identifier, token, _bound())
        if queued:
            log_system_event('INFO', 'SYSTEM', 'Confirmed database restore queued for controlled restart', db_path=_bound())
            schedule_restart()
    return {'success': True, 'phase': 'pending_restart', 'instance_id': INSTANCE_ID}


@database_admin_bp.post('/retention-hold/acknowledge')
def database_acknowledge_hold():
    data = request.get_json() or {}
    if data.get('acknowledge_media_mismatch') is not True:
        return {'success': False, 'code': 'acknowledgement_required'}, 400
    changed = acknowledge_retention_hold(_bound(), data.get('operation_id'))
    if changed:
        log_system_event('INFO', 'SYSTEM', 'Admin acknowledged database-only restore media mismatch; retention hold released',
                         db_path=_bound())
    return {'success': True, 'retention_hold': False}
