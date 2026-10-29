"""Synthetic MQTT/reconciliation evidence. No live services or media."""

import json
import sqlite3
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app import db
from app import review_repository as repository
from app.database_lifecycle import DatabaseLifecycleError, exclusive_database_lifecycle
from app.frigate_client import FrigateReviewError
from app.review_ingestion import ReviewInputError, ingest_event, ingest_review
from app.review_reconciliation import ReviewReconciler, retry_delay


@pytest.fixture
def path(tmp_path):
    target = tmp_path / 'review.db'
    db.ensure_schema(target)
    return target


def review_row(identity='r1', members=('e1',), end=None, camera='feeder'):
    return {'id': identity, 'camera': camera, 'start_time': 100, 'end_time': end,
            'severity': 'detection', 'data': {'detections': list(members), 'objects': ['bird'],
                                           'sub_labels': ['Great Tit']}}


def event_row(identity='e1', end=None):
    return {'id': identity, 'camera': 'feeder', 'label': 'bird', 'start_time': 101, 'end_time': end}


def envelope(row, kind='new'):
    return json.dumps({'type': kind, 'after': row}).encode()


def read_review(path, identity='r1'):
    with sqlite3.connect(path) as conn:
        return repository.get_review(conn, identity)


def read_event(path, identity='e1'):
    with sqlite3.connect(path) as conn:
        return repository.get_event(conn, identity)


def put_review(path, row=None, kind='new'):
    return ingest_review(path, envelope(row or review_row(), kind), ['feeder'], now=200)


def test_lifecycle_end_first_replays_and_membership(path):
    put_review(path, review_row(members=('e1', 'e1'), end=180), 'end')
    before = read_review(path)
    assert before['state'] == 'closing'
    put_review(path, review_row(), 'update')
    assert read_review(path) == before
    put_review(path, review_row(members=('e1', 'e2')), 'update')
    assert read_event(path, 'e2')['review_id'] == 'r1'
    assert read_review(path)['end_time'] == 180
    with sqlite3.connect(path) as conn:
        assert conn.execute('SELECT COUNT(*) FROM detections').fetchone() == (0,)
        assert conn.execute('SELECT COUNT(*) FROM frigate_review_events').fetchone() == (2,)
        assert all(row['classification_state'] == 'pending' for row in repository.list_review_members(conn, 'r1'))


def test_events_before_review_and_stub_hydration_preserve_provenance(path):
    ingest_event(path, envelope(event_row()), ['feeder'])
    put_review(path, review_row(members=('e1', 'e2')))
    assert read_event(path)['review_id'] == 'r1'
    assert read_event(path, 'e2')['start_time'] is None
    ingest_event(path, envelope(event_row('e2', end=150), 'end'), ['feeder'])
    with sqlite3.connect(path) as conn:
        conn.execute('BEGIN')
        repository.persist_classification(conn, 'e2', state='classified', phase='provisional',
                                          scientific_name='Parus major', score=.9,
                                          snapshot_time=120, snapshot_revision='classified-frame',
                                          classifier_model='test-model')
    put_review(path, review_row(members=('e1', 'e2')))
    row = read_event(path, 'e2')
    assert row['snapshot_revision'] == 'classified-frame'
    assert row['end_time'] == 150
    assert row['scientific_name'] == 'Parus major'


def test_conflicting_membership_is_durable_and_not_moved(path):
    put_review(path)
    assert put_review(path, review_row('r2')) == (True, 'membership_conflict')
    assert read_event(path)['review_id'] == 'r1'
    assert read_event(path)['last_error'] == 'membership_conflict:r2'
    assert read_review(path, 'r2')['reconciliation_status'] == 'incomplete'
    assert read_review(path)['last_error'] == 'membership_conflict'
    put_review(path, review_row('r2', members=()))
    assert read_review(path, 'r2')['last_error'] == 'membership_conflict'


def test_camera_conflict_preserves_valid_facts(path):
    put_review(path)
    ingest_review(path, envelope(review_row(camera='other')), ['other'], now=200)
    assert read_review(path)['camera_name'] == 'feeder'
    assert read_review(path)['last_error'] == 'camera_mismatch'


@pytest.mark.parametrize('payload', [b'[]', b'null', b'{', b'{}',
    envelope({**review_row(), 'start_time': float('nan')}),
    envelope({**review_row(), 'data': {'detections': ['e2', None]}}),
    envelope(review_row(), 'end'),
    envelope({**review_row(), 'data': {'detections': [], 'sub_labels': {}}}),
])
def test_malformed_review_does_not_mutate_prior_state(path, payload):
    put_review(path)
    before = path.read_bytes()
    with pytest.raises(ReviewInputError):
        ingest_review(path, payload, ['feeder'], now=200)
    assert path.read_bytes() == before


def test_callback_routing_retained_and_event_classification_boundary(path, monkeypatch):
    import speciesid

    monkeypatch.setattr(speciesid, 'DBPATH', path)
    monkeypatch.setattr(speciesid, 'config', {'mqtt': {'topic_prefix': 'custom'},
                                             'frigate': {'camera': ['feeder']}})
    legacy = Mock()
    monkeypatch.setattr(speciesid, '_process_live_message', legacy)
    writer, wake = Mock(), Mock()
    context = speciesid.MqttConsumerContext(speciesid._mqtt_runtime_settings(), writer, reconciler=wake)
    message = SimpleNamespace(topic='custom/reviews', retain=True, payload=envelope(review_row()))
    speciesid.on_message(Mock(), context, message)
    assert read_review(path) is None
    writer.record.assert_not_called()
    message.retain = False
    speciesid.on_message(Mock(), context, message)
    assert read_review(path)['state'] == 'active'
    legacy.assert_not_called()
    for kind in ('new', 'update', 'end'):
        message.topic = 'custom/events'
        message.payload = envelope(event_row(end=180 if kind == 'end' else None), kind)
        speciesid.on_message(Mock(), context, message)
    assert read_event(path)['end_time'] == 180
    # Review-capable activity waits for terminal species aggregation.
    legacy.assert_not_called()
    with sqlite3.connect(path) as conn:
        assert conn.execute('SELECT COUNT(*) FROM detections').fetchone() == (0,)


def reconciler(path, *, now=200):
    client = Mock()
    client.list_reviews.return_value = []
    client.get_review.return_value = review_row(end=180)
    client.get_tracked_event.return_value = event_row(end=170)
    client.get_review_for_event.return_value = review_row(end=180)
    worker = ReviewReconciler(path, ['feeder'], client, clock=lambda: now)
    return worker, client


def test_restart_missing_end_and_membership_recovered_without_output(path):
    ingest_event(path, envelope(event_row(end=170), 'end'), ['feeder'])
    worker, client = reconciler(path)
    worker.scan()
    assert read_event(path)['review_id'] == 'r1'
    assert read_review(path)['state'] == 'closing'
    client.get_review_for_event.assert_called_once()
    worker.scan()
    client.get_review_for_event.assert_called_once()
    assert worker.status.unmatched_events == 0
    assert worker.status.backlog == 0
    with sqlite3.connect(path) as conn:
        assert conn.execute('SELECT COUNT(*) FROM detections').fetchone() == (0,)


def test_known_open_review_recovers_end_after_restart(path):
    put_review(path)
    worker, client = reconciler(path)
    worker.scan()
    assert read_review(path)['end_time'] == 180
    assert read_review(path)['state'] == 'closing'
    assert read_review(path)['reconciliation_status'] == 'synced'
    client.get_review.assert_called_once_with('r1')


def test_outage_defers_and_bounded_backoff_recovers(path):
    put_review(path, review_row(members=()))
    worker, client = reconciler(path)
    client.list_reviews.side_effect = FrigateReviewError('timeout')
    client.get_review.side_effect = FrigateReviewError('timeout')
    worker.scan()
    row = read_review(path)
    assert row['end_time'] is None
    assert row['reconciliation_status'] == 'deferred'
    assert row['next_retry_at'] == 205
    worker.scan()
    client.get_review.assert_called_once()
    assert retry_delay(1000000) == 300
    worker = ReviewReconciler(path, ['feeder'], client, clock=lambda: 206)
    client.list_reviews.side_effect = None
    client.get_review.side_effect = None
    client.get_review.return_value = review_row(members=(), end=180)
    worker.scan()
    assert read_review(path)['state'] == 'closing'
    assert read_review(path)['retry_count'] == 0


def test_unsupported_is_explicit_and_silence_is_not_evidence(path):
    worker, client = reconciler(path)
    worker.scan()
    assert worker.status.api_state == 'available'  # Empty successful API is support.
    worker.next_discovery = 0
    client.list_reviews.side_effect = FrigateReviewError('unsupported')
    worker.scan()
    assert worker.status.api_state == 'unsupported'
    assert worker.status.error_code == 'unsupported'


def test_api_only_discovery_and_pending_counts(path):
    worker, client = reconciler(path)
    client.list_reviews.return_value = [review_row()]
    worker.scan()
    assert read_review(path)['state'] == 'active'
    assert read_event(path)['review_id'] == 'r1'
    assert worker.status.active_reviews == 1
    assert worker.status.api_state == 'available'


def test_worker_wakeup_is_bounded_and_stop_prevents_writes(path):
    worker, client = reconciler(path)
    for _ in range(1000):
        worker.wake()
    assert worker.wakeup.is_set()
    worker.stop.set()
    client.list_reviews.return_value = [review_row()]
    worker.scan()
    assert read_review(path) is None


def test_reconciliation_holds_restore_exclusion_during_network_and_stops(path):
    worker, client = reconciler(path)
    entered, release = threading.Event(), threading.Event()

    def lookup(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        return []

    client.list_reviews.side_effect = lookup
    with worker:
        assert entered.wait(3)
        try:
            with pytest.raises(DatabaseLifecycleError), exclusive_database_lifecycle(path):
                pytest.fail('Reconciliation must exclude offline restore')
        finally:
            release.set()
    assert not worker.thread.is_alive()
    with exclusive_database_lifecycle(path):
        pass


def test_worker_recovers_scan_failure_without_leaking_exception(path, monkeypatch, caplog):
    monkeypatch.setattr('app.review_reconciliation.POLL_SECONDS', .01)
    worker, _client = reconciler(path)
    put_review(path, review_row(members=()))
    scanned = threading.Event()
    original = worker.scan
    calls = []

    def scan():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError('PRIVATE-REMOTE-ERROR')
        original()
        scanned.set()

    worker.scan = scan
    with worker:
        assert scanned.wait(3)
    assert not worker.thread.is_alive()
    assert read_review(path)['state'] == 'closing'
    assert 'PRIVATE' not in caplog.text


def test_scan_batch_and_repeated_payloads_do_not_reset_backoff(path):
    worker, client = reconciler(path)
    for index in range(12):
        ingest_event(path, envelope(event_row(f'e{index}')), ['feeder'])
    client.get_tracked_event.side_effect = FrigateReviewError('timeout')
    worker.scan()
    assert client.get_tracked_event.call_count == 4
    row = read_event(path, 'e0')
    assert row['next_retry_at'] == 205
    for _ in range(10):
        ingest_event(path, envelope(event_row('e0')), ['feeder'])
    assert read_event(path, 'e0')['next_retry_at'] == 205
    worker.scan()
    assert client.get_tracked_event.call_count == 8  # Other durable jobs, not e0 again.


def test_expired_event_metadata_does_not_block_authoritative_review_membership(path):
    ingest_event(path, envelope(event_row()), ['feeder'])
    worker, client = reconciler(path)
    client.get_tracked_event.side_effect = FrigateReviewError('event_missing')
    worker.scan()
    row = read_event(path)
    assert row['review_id'] == 'r1'
    assert row['end_time'] is None
    assert row['last_error'] == 'event_missing'
    assert row['next_retry_at'] == 205
    assert row['classification_state'] == 'pending'


def test_metadata_db_failure_does_not_suppress_legacy_dispatch(path, monkeypatch):
    import speciesid

    monkeypatch.setattr(speciesid, 'config', {'mqtt': {'topic_prefix': 'custom'},
                                             'frigate': {'camera': ['feeder']}})
    monkeypatch.setattr(speciesid, 'ingest_event', Mock(side_effect=sqlite3.OperationalError('PRIVATE')))
    legacy = Mock()
    monkeypatch.setattr(speciesid, '_process_live_message', legacy)
    message = SimpleNamespace(topic='custom/events', retain=False, payload=envelope(event_row()))
    speciesid.on_message(Mock(), None, message)
    legacy.assert_called_once()


def test_malformed_event_is_rejected_without_state_or_classification(path, monkeypatch):
    import speciesid

    monkeypatch.setattr(speciesid, 'DBPATH', path)
    monkeypatch.setattr(speciesid, 'config', {'mqtt': {'topic_prefix': 'custom'},
                                             'frigate': {'camera': ['feeder']}})
    legacy = Mock()
    monkeypatch.setattr(speciesid, '_process_live_message', legacy)
    message = SimpleNamespace(topic='custom/events', retain=False,
                              payload=envelope({**event_row(), 'start_time': None}))
    speciesid.on_message(Mock(), None, message)
    legacy.assert_not_called()
    assert read_event(path) is None


def test_unavailable_list_endpoint_does_not_disable_working_id_lookup(path):
    put_review(path, review_row(members=()))
    worker, client = reconciler(path)
    client.list_reviews.side_effect = FrigateReviewError('unsupported')
    client.get_review.return_value = review_row(members=(), end=180)
    worker.scan()
    assert read_review(path)['state'] == 'closing'
    assert read_review(path)['reconciliation_status'] == 'synced'
    assert worker.status.api_state == 'degraded'  # One API path works, discovery does not.
