"""#49 round trips of #41 state, including a genuine 006 restore candidate."""

import sqlite3
import zipfile

import pytest

import wamf_paths
from app import database_restore as staging
from app import database_restore_application as application
from app import db
from app import review_repository as repo
from app.database_backup import validate_package
from app.database_backup_service import create_backup, resolve_backup_download
from app.database_backup_storage import BackupIdentity
from app.database_lifecycle import exclusive_database_lifecycle
from app.db_inspection import DatabaseCompatibility as Compatibility
from app.db_inspection import inspect_database
from app.review_schema import REVIEW_TABLE_SCHEMAS
from tests.test_database_backup import manifest_for
from tests.test_database_backup import package as write_package
from tests.test_database_restore_application import finish_startup
from tests.test_review_grouping import (
    application_rows,
    classified,
    event,
    review,
    version006,
)


def review_rows(path):
    with sqlite3.connect(path) as conn:
        return {table: conn.execute(f'SELECT * FROM {table} ORDER BY rowid').fetchall()
                for table in REVIEW_TABLE_SCHEMAS}


@pytest.mark.parametrize('sequence', [6, 7])
def test_backup_validation_restore_upgrade_and_durable_state(tmp_path, monkeypatch, sequence):
    monkeypatch.setattr(wamf_paths, 'REPO_ROOT', tmp_path)
    source, target = tmp_path / 'source.db', tmp_path / 'target.db'
    if sequence == 6:
        version006(source, monkeypatch)
    else:
        db.ensure_schema(source)
        with sqlite3.connect(source) as conn:
            conn.execute('BEGIN')
            review(conn)
            event(conn, review_id='r1')
            classified(conn, phase='provisional')
            event(conn, 'unmatched')
            review(conn, 'closing', end_time=200)
            repo.set_review_state(conn, 'closing', 'closing', reconciliation_status='deferred',
                                  retry_count=3, next_retry_at=300, last_error='api_unavailable')
            review(conn, 'done', end_time=200)
            repo.set_review_state(conn, 'done', 'finalized', aggregation_rule_version='v1')
            repo.suppress_species(conn, 'r1', 'Parus major')
    with sqlite3.connect(source) as conn:
        conn.execute("INSERT INTO detections (id,detection_time,frigate_event,wamf_snapshot_path) "
                     "VALUES (90,'historical','real-event','/archive/keep.jpg')")
    expected_application = application_rows(source)
    expected_reviews = review_rows(source) if sequence == 7 else {key: [] for key in REVIEW_TABLE_SCHEMAS}
    source_bytes = source.read_bytes()
    if sequence == 7:
        result = create_backup(source, wamf_paths.get_database_backup_root())
        assert result.success, result
        backup_id = result.backup_id
    else:
        # Simulate an existing 006 backup. Live backup creation intentionally
        # requires current schema; accepting older restore packages is separate.
        backup_id = write_package(source_bytes, manifest_for(source), staged=False).backup_id
    checked = validate_package(BackupIdentity(backup_id))
    assert checked.valid
    assert checked.inspection.recognized_sequence == sequence
    assert checked.compatibility is (Compatibility.CURRENT if sequence == 7 else Compatibility.UPGRADEABLE)
    db.ensure_schema(target)
    with (resolve_backup_download(backup_id, wamf_paths.get_database_backup_root()) as download,
          zipfile.ZipFile(download.file) as archive):
        packaged_database = archive.read('database.db')
    staged = staging.stage_restore_from_backup(backup_id, target)
    assert staged.success
    confirmed = staging.confirm_restore_plan(staged.operation_id, staged.plan.confirmation_token,
                                             bound_target_path=target)
    assert confirmed.success
    with exclusive_database_lifecycle(target) as lease:
        applied = application.apply_restore(lease, staged.operation_id, staged.plan.confirmation_token)
    assert applied.success, applied
    assert applied.migration_performed is (sequence == 6)
    finish_startup(target)
    assert inspect_database(target).compatibility is Compatibility.CURRENT
    assert inspect_database(target).recognized_sequence == 7
    assert application_rows(target) == expected_application
    assert review_rows(target) == expected_reviews
    assert source.read_bytes() == source_bytes
    candidate = wamf_paths.get_database_restore_root() / staged.operation_id / 'candidate.db'
    assert candidate.read_bytes() == packaged_database
