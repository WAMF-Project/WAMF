"""007 and repository tests use disposable SQLite files, never the live database."""

import sqlite3
from dataclasses import replace

import pytest

from app import db
from app import review_repository as repo
from app.db_inspection import DatabaseCompatibility as Compatibility
from app.db_inspection import inspect_database
from app.review_schema import DETECTION_GROUPING_COLUMNS, REVIEW_TABLE_SCHEMAS
from tests.test_db_migrations import current_prebaseline, snapshot


def version006(path, monkeypatch):
    with monkeypatch.context() as patch:
        patch.setattr(db, 'MIGRATIONS', db.MIGRATIONS[:6])
        db.ensure_schema(path)


@pytest.fixture
def connection(tmp_path):
    path = tmp_path / 'reviews.db'
    db.ensure_schema(path)
    with sqlite3.connect(path) as conn:
        conn.execute('BEGIN')
        yield conn


def review(conn, identity='r1', **changes):
    return repo.upsert_review(conn, identity, **{
        'camera_name': 'feeder', 'start_time': 100, **changes})


def event(conn, identity='e1', **changes):
    values = {'camera_name': 'feeder', 'label': 'bird', 'start_time': 101}
    values.update(changes)
    return repo.upsert_event(conn, identity, **values)


def classified(conn, identity='e1', **changes):
    values = {'state': 'classified', 'phase': 'terminal', 'scientific_name': 'Parus major',
              'common_name': 'Great Tit', 'category_name': 'bird', 'detection_index': 12,
              'score': .9, 'snapshot_revision': 'frame-110', 'snapshot_time': 110,
              'classifier_model': 'aiy-birds-v1'}
    values.update(changes)
    return repo.persist_classification(conn, identity, **values)


def observation(conn, event_id='e1', **changes):
    values = {'review_id': 'r1', 'representative_event_id': event_id,
              'detection_time': '2026-10-25 10:00:00',
              'observation_end_time': '2026-10-25 10:04:00', 'support_count': 1,
              'aggregation_rule_version': 'species-per-review-v1', 'score': .85}
    values.update(changes)
    return repo.create_grouped_observation(conn, **values)


def application_rows(path):
    with sqlite3.connect(path) as conn:
        return {table: conn.execute(f'SELECT {columns} FROM {table} ORDER BY rowid').fetchall()
                for table, columns in (
                    ('detections', ','.join(db.DETECTION_COLUMNS)),
                    ('system_events', '*'), ('species_info', '*'),
                    ('retention_status', '*'), ('sqlite_sequence', '*'))}


def test_006_upgrade_preserves_all_data_sequence_and_history(tmp_path, monkeypatch):
    path = tmp_path / '006.db'
    current_prebaseline(path)
    version006(path, monkeypatch)
    with sqlite3.connect(path) as conn:
        # Historical duplicate event identities must remain legal and unchanged.
        conn.execute("INSERT INTO detections (id, detection_time, frigate_event) VALUES (42, 'old', 'event')")
        ledger = conn.execute('SELECT * FROM schema_migrations').fetchall()
    before = application_rows(path)
    assert inspect_database(path).recognized_sequence == 6
    assert inspect_database(path).compatibility is Compatibility.UPGRADEABLE
    db.ensure_schema(path)
    assert application_rows(path) == before
    result = inspect_database(path, check_integrity=True)
    assert result.compatibility is Compatibility.CURRENT
    assert result.recognized_migration_id == '007_review_grouping_model'
    with sqlite3.connect(path) as conn:
        assert conn.execute('SELECT * FROM schema_migrations LIMIT 6').fetchall() == ledger
        columns = ','.join(DETECTION_GROUPING_COLUMNS)
        assert conn.execute(f'SELECT {columns} FROM detections').fetchall() == [(None,) * 4] * 2
        for table in REVIEW_TABLE_SCHEMAS:
            assert conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone() == (0,)
        conn.execute("INSERT INTO detections (detection_time) VALUES ('next')")
        assert conn.execute('SELECT MAX(id) FROM detections').fetchone() == (901,)
    before = snapshot(path)
    db.ensure_schema(path)
    assert snapshot(path) == before


@pytest.mark.parametrize('fresh', [False, True])
def test_007_failure_rolls_back_schema_data_and_ledger(tmp_path, monkeypatch, fresh):
    path = tmp_path / 'rollback.db'
    if fresh:
        sqlite3.connect(path).close()
    else:
        current_prebaseline(path)
        version006(path, monkeypatch)
    before = snapshot(path)
    original = db.MIGRATIONS[-1]

    def fail(conn):
        assert conn.in_transaction
        original.upgrade(conn)
        conn.execute("UPDATE system_events SET message='must roll back'")
        raise RuntimeError('injected 007 failure')

    monkeypatch.setattr(db, 'MIGRATIONS', db.MIGRATIONS[:-1] + (replace(original, upgrade=fail),))
    with pytest.raises(RuntimeError, match='injected 007'):
        db.ensure_schema(path)
    assert snapshot(path) == before


@pytest.mark.parametrize('damage', [
    'DROP TABLE frigate_review_events',
    'DROP TABLE frigate_review_suppressions',
    'DROP INDEX idx_detections_review_species',
    'ALTER TABLE frigate_reviews ADD COLUMN speculative TEXT',
    'ALTER TABLE detections DROP COLUMN observation_end_time',
    "DELETE FROM schema_migrations WHERE version='007_review_grouping_model'",
])
def test_partial_007_is_invalid_not_repaired(tmp_path, damage):
    path = tmp_path / 'partial.db'
    db.ensure_schema(path)
    with sqlite3.connect(path) as conn:
        conn.execute(damage)
    before = snapshot(path)
    assert inspect_database(path).compatibility is Compatibility.INVALID
    with pytest.raises(db.SchemaMigrationError):
        db.ensure_schema(path)
    assert snapshot(path) == before


def test_review_lifecycle_idempotence_and_stale_messages(connection):
    conn = connection
    first = review(conn, severity='detection')
    assert review(conn, severity='detection') == first
    assert review(conn, end_time=200, severity='alert')['state'] == 'closing'
    assert review(conn, severity='detection')['end_time'] == 200
    assert repo.get_review(conn, 'r1')['severity'] == 'alert'
    repo.set_review_state(conn, 'r1', 'finalized', aggregation_rule_version='v1')
    assert review(conn)['state'] == 'finalized'
    assert repo.list_open_reviews(conn) == []
    with pytest.raises(ValueError, match='Finalized'):
        repo.set_review_state(conn, 'r1', 'active')
    with pytest.raises(ValueError, match='camera mismatch'):
        repo.upsert_review(conn, 'r1', camera_name='other', start_time=100)


@pytest.mark.parametrize('state', ['active', 'closing', 'finalized', 'deferred', 'incomplete'])
def test_review_states_survive_reopen(tmp_path, state):
    path = tmp_path / 'state.db'
    db.ensure_schema(path)
    with sqlite3.connect(path) as conn:
        conn.execute('BEGIN')
        review(conn, end_time=200 if state in ('closing', 'finalized') else None)
        repo.set_review_state(conn, 'r1', state, reconciliation_status='deferred',
                              retry_count=2, next_retry_at=300, last_error='api_unavailable',
                              aggregation_rule_version='v1')
    with sqlite3.connect(path) as conn:
        row = repo.get_review(conn, 'r1')
        assert (row['state'], row['reconciliation_status'], row['retry_count'],
                row['next_retry_at'], row['last_error']) == (state, 'deferred', 2, 300, 'api_unavailable')


def test_event_before_review_and_review_before_event(connection):
    conn = connection
    before = event(conn)
    assert before['review_id'] is None
    assert event(conn) == before
    review(conn)
    repo.link_event_to_review(conn, 'e1', 'r1')
    repo.link_event_to_review(conn, 'e1', 'r1')
    stub = repo.upsert_event(conn, 'e2', camera_name='feeder', review_id='r1')
    assert stub['label'] is stub['start_time'] is None
    assert len(repo.list_review_members(conn, 'r1')) == 2
    event(conn, 'e2', review_id='r1', end_time=150)
    assert event(conn, 'e2')['end_time'] == 150
    assert [row['event_id'] for row in repo.list_review_members(conn, 'r1')] == ['e1', 'e2']
    assert conn.execute('SELECT COUNT(*) FROM frigate_review_events').fetchone() == (2,)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO frigate_review_events (event_id,camera_name,label,start_time) "
                     "VALUES ('e1','feeder','bird',100)")


def test_membership_integrity_without_foreign_keys(connection):
    conn = connection
    assert conn.execute('PRAGMA foreign_keys').fetchone() == (0,)
    event(conn)
    with pytest.raises(ValueError, match='does not exist'):
        repo.link_event_to_review(conn, 'e1', 'absent')
    repo.upsert_review(conn, 'other', camera_name='other-camera', start_time=100)
    with pytest.raises(ValueError, match='camera mismatch'):
        repo.link_event_to_review(conn, 'e1', 'other')
    review(conn)
    repo.link_event_to_review(conn, 'e1', 'r1')
    review(conn, 'r2')
    with pytest.raises(ValueError, match='another review'):
        repo.link_event_to_review(conn, 'e1', 'r2')
    with pytest.raises(ValueError, match='camera/label mismatch'):
        event(conn, label='cat')
    review(conn, end_time=200)
    repo.set_review_state(conn, 'r1', 'finalized', aggregation_rule_version='v1')
    with pytest.raises(ValueError, match='Finalized membership'):
        event(conn, 'late', review_id='r1')
    assert repo.get_event(conn, 'late') is None


def test_provisional_terminal_and_duplicate_evidence(connection):
    conn = connection
    event(conn)
    assert classified(conn, phase='provisional')
    assert not classified(conn, phase='provisional')
    assert classified(conn, scientific_name='Cyanistes caeruleus', snapshot_revision='final', snapshot_time=120)
    assert not classified(conn, phase='provisional')
    assert not classified(conn, snapshot_revision='older', snapshot_time=115)
    row = repo.get_event(conn, 'e1')
    assert row['scientific_name'] == 'Cyanistes caeruleus'
    assert row['classification_phase'] == 'terminal'
    assert row['classifier_model'] == 'aiy-birds-v1'
    with pytest.raises(ValueError, match='already has a result'):
        classified(conn, snapshot_revision='final', snapshot_time=120, score=.99)


@pytest.mark.parametrize('state,reason', [('rejected', 'below_threshold'), ('rejected', 'background'),
                                          ('failed', 'snapshot_unavailable')])
def test_nonusable_evidence_is_durable(connection, state, reason):
    conn = connection
    event(conn)
    repo.persist_classification(conn, 'e1', state=state, phase='terminal', reason=reason,
                                retry_count=1, next_retry_at=300, last_error='safe_code')
    conn.commit()
    row = repo.get_event(conn, 'e1')
    assert (row['classification_state'], row['reason'], row['retry_count'], row['next_retry_at']) == (
        state, reason, 1, 300)
    assert row['scientific_name'] is None


def test_grouped_species_uniqueness_real_representatives_and_links(connection):
    conn = connection
    review(conn, end_time=200)
    for identity, species in [('e1', 'Parus major'), ('e2', 'Cyanistes caeruleus'), ('e3', 'Parus major')]:
        event(conn, identity, review_id='r1')
        classified(conn, identity, scientific_name=species)
    one = observation(conn)
    assert observation(conn) == one
    two = observation(conn, 'e2')
    assert two != one
    assert observation(conn, 'e3') == one
    for identity in ('e1', 'e3'):
        repo.link_event_to_observation(conn, identity, one)
        repo.link_event_to_observation(conn, identity, one)
    with pytest.raises(ValueError, match='must match'):
        repo.link_event_to_observation(conn, 'e2', one)
    with pytest.raises(ValueError, match='does not exist'):
        observation(conn, 'review-or-synthetic-id')
    assert conn.execute('SELECT frigate_event FROM detections ORDER BY id').fetchall() == [('e1',), ('e2',)]
    assert conn.execute('SELECT score FROM detections WHERE id=?', (one,)).fetchone() == (.85,)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO detections (detection_time,frigate_review_id,display_name) "
                     "VALUES ('t','r1','Parus major')")
    for _ in range(2):
        conn.execute("INSERT INTO detections (detection_time,frigate_event,display_name) "
                     "VALUES ('historical','same-id','Parus major')")
    assert conn.execute('SELECT frigate_review_id, observation_end_time, support_count, '
                        'aggregation_rule_version FROM detections WHERE frigate_review_id IS NULL'
                        ).fetchall() == [(None,) * 4] * 2


def test_suppression_survives_observation_deletion(connection):
    conn = connection
    review(conn, end_time=200)
    event(conn, review_id='r1')
    classified(conn)
    identity = observation(conn)
    repo.suppress_species(conn, 'r1', 'Parus major')
    repo.suppress_species(conn, 'r1', 'Parus major')
    conn.execute('DELETE FROM detections WHERE id=?', (identity,))
    assert repo.is_suppressed(conn, 'r1', 'Parus major')
    assert len(repo.list_review_members(conn, 'r1')) == 1
    with pytest.raises(ValueError, match='suppressed'):
        observation(conn)
    assert repo.get_review(conn, 'r1') is not None


def test_caller_rollback_includes_all_model_work(connection):
    conn = connection
    review(conn)
    event(conn, review_id='r1')
    classified(conn)
    repo.suppress_species(conn, 'r1', 'Parus major')
    conn.rollback()
    assert repo.get_review(conn, 'r1') is None
    assert repo.get_event(conn, 'e1') is None
    assert not repo.is_suppressed(conn, 'r1', 'Parus major')
    with pytest.raises(ValueError, match='caller-owned transaction'):
        review(conn)


def test_no_child_media_ownership_or_public_select_change(connection):
    columns = {row[1] for row in connection.execute('PRAGMA table_info(frigate_review_events)')}
    assert not any('path' in column for column in columns)
    assert 'frigate_review_id' not in db.DETECTION_SELECT


def test_retry_preserves_provisional_evidence_and_pending_inventory(connection):
    conn = connection
    event(conn)
    classified(conn, phase='provisional')
    repo.set_event_retry(conn, 'e1', retry_count=1, next_retry_at=250, last_error='snapshot_unavailable')
    before = repo.get_event(conn, 'e1')
    repo.set_event_retry(conn, 'e1', retry_count=1, next_retry_at=250, last_error='snapshot_unavailable')
    assert repo.get_event(conn, 'e1') == before
    assert before['scientific_name'] == 'Parus major'
    assert before['classification_phase'] == 'provisional'
    assert repo.list_pending_events(conn) == [before]
    classified(conn, snapshot_time=120, snapshot_revision='final')
    assert repo.list_pending_events(conn) == []


@pytest.mark.parametrize('changes', [
    {'state': 'invalid'}, {'phase': 'invalid'}, {'score': float('nan')},
    {'score': 2}, {'scientific_name': ''}, {'snapshot_time': -1},
    {'retry_count': -1}, {'state': 'rejected', 'reason': None},
])
def test_invalid_classification_does_not_modify_evidence(connection, changes):
    conn = connection
    before = event(conn)
    with pytest.raises(ValueError):
        classified(conn, **changes)
    assert repo.get_event(conn, 'e1') == before


def test_row_factory_and_missing_observation_relationship(connection):
    conn = connection
    conn.row_factory = sqlite3.Row
    review(conn, end_time=200)
    event(conn, review_id='r1')
    classified(conn)
    with pytest.raises(ValueError, match='does not exist'):
        repo.link_event_to_observation(conn, 'e1', 999)
    identity = observation(conn)
    repo.link_event_to_observation(conn, 'e1', identity)
    assert repo.get_event(conn, 'e1')['observation_id'] == identity
    with pytest.raises(ValueError, match='Linked evidence'):
        classified(conn, snapshot_revision='changed', snapshot_time=120)


def test_membership_stub_survives_restart_and_hydrates_without_losing_link(tmp_path):
    path = tmp_path / 'stub.db'
    db.ensure_schema(path)
    with sqlite3.connect(path) as conn:
        conn.execute('BEGIN')
        review(conn)
        repo.upsert_event(conn, 'e1', camera_name='feeder', review_id='r1')
    with sqlite3.connect(path) as conn:
        assert repo.list_review_members(conn, 'r1')[0]['classification_state'] == 'pending'
        conn.execute('BEGIN')
        with pytest.raises(ValueError, match='hydrated'):
            classified(conn)
        row = event(conn)
        assert (row['review_id'], row['start_time'], row['label']) == ('r1', 101, 'bird')
        repo.upsert_event(conn, 'e1', camera_name='feeder')
        assert repo.get_event(conn, 'e1') == row
        assert classified(conn)
