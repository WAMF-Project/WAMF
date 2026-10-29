"""Slice 3 uses temporary databases, fake inference and fake Frigate HTTP only."""

import io
import json
import sqlite3
import threading
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from PIL import Image

from app import db
from app import review_repository as repository
from app.classifier_runtime import ClassifierRuntimeSnapshot, terminal_model_identity
from app.frigate_client import FrigateClient, FrigateError, FrigateReviewError
from app.review_ingestion import ingest_event, ingest_review, metadata_connection
from app.review_reconciliation import ReviewReconciler
from app.terminal_classification import MAX_ATTEMPTS, TerminalClassifier
from app.terminal_evidence import TerminalEvidenceError, TerminalImage, decode_revision
from tests.test_review_ingestion import (
    envelope,
    event_row,
    read_event,
    read_review,
    review_row,
)


@pytest.fixture
def database(tmp_path):
    path = tmp_path / 'terminal.db'
    db.ensure_schema(path)
    return path


def seed(path, *, end=170, identity='e1', **metadata):
    ingest_review(path, envelope(review_row(members=(identity,), end=180), 'end'), ['feeder'], now=200)
    payload = {**event_row(identity, end=end), 'snapshot': {'frame_time': 130}, **metadata}
    ingest_event(path, envelope(payload, 'end' if end else 'new'), ['feeder'])


def category(species='Parus major', score=.9, index=12):
    return SimpleNamespace(display_name=species, category_name='bird', score=score, index=index)


def processor(client=None, classifier=None):
    client = client or Mock()
    if not isinstance(client, FrigateClient):
        client.acquire_terminal_image.return_value = TerminalImage(b'final-jpeg', 150, 'event_snapshot_after_end', 'terminal_event_api')
    classifier = classifier or Mock(return_value=[category()])
    return TerminalClassifier(client, classifier, model_id='aiy@v1:sha256:abc', threshold=.7,
                              background_index=964, common_name=lambda name: {'Parus major': 'Great Tit'}.get(name))


def run(path, terminal, now=200):
    terminal.run_batch(path, ['feeder'], threading.Event(), lambda: now)


def test_terminal_persistence_restart_and_repeated_end_are_idempotent(database):
    seed(database)
    terminal = processor()
    run(database, terminal)
    row = read_event(database)
    assert (row['classification_state'], row['classification_phase']) == ('classified', 'terminal')
    assert (row['scientific_name'], row['common_name'], row['category_name'], row['detection_index'], row['score']) == (
        'Parus major', 'Great Tit', 'bird', 12, .9)
    assert row['classifier_model'] == 'aiy@v1:sha256:abc'
    assert row['snapshot_time'] == 150
    assert len(decode_revision(row['snapshot_revision'])['sha256']) == 64
    assert (row['retry_count'], row['next_retry_at'], row['last_error']) == (0, None, None)
    for _ in range(3):
        ingest_event(database, envelope(event_row(end=170), 'end'), ['feeder'])
        ingest_review(database, envelope(review_row(), 'update'), ['feeder'], now=201)
        run(database, terminal, now=201)
    assert read_event(database) == row
    terminal.classify_snapshot.assert_called_once_with(b'final-jpeg')
    restarted = processor()
    run(database, restarted, now=300)
    restarted.classify_snapshot.assert_not_called()
    with sqlite3.connect(database) as conn:
        assert conn.execute('SELECT COUNT(*) FROM detections').fetchone() == (0,)
    assert read_review(database)['state'] == 'closing'


@pytest.mark.parametrize('result,reason', [
    ([], 'no_usable_classification'), ([category(score=.7)], 'below_threshold'),
    ([category(index=964)], 'background'),
])
def test_rejections_are_terminal_evidence(database, result, reason):
    seed(database)
    terminal = processor(classifier=Mock(return_value=result))
    run(database, terminal)
    row = read_event(database)
    assert (row['classification_state'], row['classification_phase'], row['reason']) == ('rejected', 'terminal', reason)
    assert row['classifier_model'] == 'aiy@v1:sha256:abc'
    run(database, terminal)
    terminal.classify_snapshot.assert_called_once()


def test_conflicting_species_stay_separate_constituent_results(database):
    terminal = processor(classifier=Mock(side_effect=[
        [category()], [category('Cyanistes caeruleus')], [category()],
    ]))
    for identity in ('e1', 'e2', 'e3'):
        seed(database, identity=identity)
    run(database, terminal)
    assert [read_event(database, identity)['scientific_name'] for identity in ('e1', 'e2', 'e3')] == [
        'Parus major', 'Cyanistes caeruleus', 'Parus major']


def test_retry_backoff_survives_restart_and_success_clears_it(database):
    seed(database)
    terminal = processor()
    terminal.client.acquire_terminal_image.side_effect = TerminalEvidenceError('snapshot_unavailable')
    run(database, terminal)
    row = read_event(database)
    assert row['classification_state'] == 'pending'
    assert row['classification_phase'] is None
    assert (row['retry_count'], row['next_retry_at']) == (1, 205)
    run(database, terminal, now=204)
    terminal.client.acquire_terminal_image.assert_called_once()
    ingest_event(database, envelope(event_row(end=170), 'end'), ['feeder'])
    assert read_event(database)['next_retry_at'] == 205
    restarted = processor()
    run(database, restarted, now=205)
    assert read_event(database)['classification_state'] == 'classified'
    assert read_event(database)['retry_count'] == 0


@pytest.mark.parametrize('failure', ['snapshot', 'classifier'])
def test_retry_exhaustion_is_failed_not_rejected(database, failure):
    seed(database)
    terminal = processor()
    if failure == 'snapshot':
        terminal.client.acquire_terminal_image.side_effect = TerminalEvidenceError('snapshot_unavailable')
    else:
        terminal.classify_snapshot.side_effect = RuntimeError('PRIVATE runtime details')
    now = 200
    for attempt in range(1, MAX_ATTEMPTS + 1):
        run(database, terminal, now)
        row = read_event(database)
        assert row['retry_count'] == attempt
        now = row['next_retry_at'] or now + 300
    assert (row['classification_state'], row['classification_phase'], row['next_retry_at']) == ('failed', 'terminal', None)
    assert row['reason'] == ('snapshot_unavailable' if failure == 'snapshot' else 'classifier_execution_failed')
    assert 'PRIVATE' not in json.dumps(row)
    if failure == 'classifier':
        assert 'sha256' in decode_revision(row['snapshot_revision'])
    calls = terminal.classify_snapshot.call_count
    run(database, terminal, now)
    assert terminal.classify_snapshot.call_count == calls


def test_nonretryable_failure_is_terminal_immediately(database):
    seed(database)
    terminal = processor(classifier=Mock(return_value=[category(score=float('nan'))]))
    run(database, terminal)
    row = read_event(database)
    assert (row['classification_state'], row['reason'], row['retry_count']) == ('failed', 'invalid_classifier_score', 1)


def test_false_positive_hint_survives_restart_and_never_runs_inference(database):
    seed(database, false_positive=True)
    terminal = processor()
    run(database, terminal)
    assert read_event(database)['reason'] == 'false_positive'
    assert read_event(database)['classification_state'] == 'rejected'
    terminal.client.acquire_terminal_image.assert_not_called()
    terminal.classify_snapshot.assert_not_called()


def test_cat_is_recorded_as_ineligible_without_bird_classifier(database):
    seed(database)
    with metadata_connection(database) as conn:
        repository.upsert_event(conn, 'cat', camera_name='feeder', label='cat', start_time=101, end_time=170, review_id='r1')
    terminal = processor()
    run(database, terminal)
    assert read_event(database, 'cat')['reason'] == 'non_bird'
    assert read_event(database, 'cat')['classification_phase'] == 'terminal'
    assert terminal.classify_snapshot.call_count == 1  # Bird only.


def test_no_end_or_no_review_membership_is_not_terminal_ready(database):
    seed(database, end=None)
    ingest_event(database, envelope(event_row('unmatched', end=170), 'end'), ['feeder'])
    terminal = processor()
    run(database, terminal)
    terminal.classify_snapshot.assert_not_called()
    assert read_event(database)['classification_state'] == 'pending'


def test_api_recovered_end_schedules_classification_in_owned_scan(database):
    seed(database, end=None)
    terminal = processor()
    client = terminal.client
    client.list_reviews.return_value = []
    client.get_review.return_value = review_row(end=180)
    client.get_tracked_event.return_value = event_row(end=170)
    worker = ReviewReconciler(database, ['feeder'], client, clock=lambda: 200, terminal_classifier=terminal)
    worker.scan()
    assert read_event(database)['classification_state'] == 'classified'
    assert read_review(database)['state'] == 'closing'
    assert worker.status.terminal_classified == 1
    assert worker.status.terminal_pending == 0


def test_metadata_reconciliation_cannot_clear_terminal_retry(database):
    seed(database)
    terminal = processor()
    terminal.client.acquire_terminal_image.side_effect = TerminalEvidenceError('snapshot_unavailable')
    run(database, terminal)
    worker = ReviewReconciler(database, ['feeder'], terminal.client, clock=lambda: 201)
    worker._defer('event', 'e1', FrigateReviewError('event_missing'))
    worker.reconcile_event('e1')
    assert read_event(database)['next_retry_at'] == 205
    assert read_event(database)['last_error'] == 'terminal:snapshot_unavailable'


def test_db_failure_reuses_uncommitted_outcome_without_reinvoking(database):
    seed(database)
    terminal = processor()
    persist = repository.persist_classification
    with (patch.object(repository, 'persist_classification', side_effect=sqlite3.OperationalError('locked')),
          pytest.raises(sqlite3.Error)):
        run(database, terminal)
    assert read_event(database)['classification_state'] == 'pending'
    with patch.object(repository, 'persist_classification', side_effect=persist):
        run(database, terminal)
    terminal.classify_snapshot.assert_called_once()
    assert read_event(database)['classification_state'] == 'classified'


def test_shutdown_after_inference_does_not_commit_outcome(database):
    seed(database)
    stop = threading.Event()
    terminal = processor(classifier=Mock(side_effect=lambda _: (stop.set(), [category()])[1]))
    terminal.run_batch(database, ['feeder'], stop, lambda: 200)
    assert read_event(database)['classification_state'] == 'pending'


def real_client():
    client = FrigateClient('http://fixture')
    client.get_tracked_event = Mock(return_value={**event_row(end=170), 'snapshot': {'frame_time': 160}})
    client.get_event_snapshot = Mock(return_value=b'terminal-jpeg')
    return client


def test_terminal_snapshot_and_later_metadata_win_over_initial(database):
    seed(database)
    client = real_client()
    client.remember_event_snapshot('e1', b'early-jpeg', {**event_row(), 'snapshot': {'frame_time': 110}})
    terminal = processor(client)
    run(database, terminal)
    row = read_event(database)
    assert row['snapshot_time'] == 160
    assert decode_revision(row['snapshot_revision'])['source'] == 'event_snapshot_after_end'
    terminal.classify_snapshot.assert_called_once_with(b'terminal-jpeg')
    client.get_event_snapshot.assert_called_once_with('e1', crop=True, quality=95, max_bytes=4 * 1024 * 1024)


def test_fallback_only_after_final_snapshot_retries_and_has_early_provenance(database):
    seed(database)
    client = real_client()
    client.remember_event_snapshot('e1', b'early-jpeg', {**event_row(), 'snapshot': {'frame_time': 110}})
    client.get_event_snapshot.side_effect = FrigateError('unavailable', status_code=404)
    terminal = processor(client)
    now = 200
    for _ in range(MAX_ATTEMPTS):
        run(database, terminal, now)
        now = read_event(database)['next_retry_at'] or now + 300
    row = read_event(database)
    assert row['classification_state'] == 'classified'
    assert row['reason'] == 'fallback_cached_snapshot'
    assert row['snapshot_time'] == 110
    assert decode_revision(row['snapshot_revision'])['source'] == 'cached_legacy_fallback'
    terminal.classify_snapshot.assert_called_once_with(b'early-jpeg')


@pytest.mark.parametrize('status', [None, 500, 403])
def test_cached_fallback_is_not_used_for_network_or_auth_errors(database, status):
    seed(database)
    client = real_client()
    client.remember_event_snapshot('e1', b'early', event_row())
    client.get_event_snapshot.side_effect = FrigateError('PRIVATE', status_code=status)
    with pytest.raises(TerminalEvidenceError):
        client.acquire_terminal_image(read_event(database), allow_fallback=True)


def test_missing_frame_timestamp_stays_null_without_fabrication(database):
    seed(database, snapshot=None)
    client = real_client()
    client.get_tracked_event.return_value = event_row(end=170)
    terminal = processor(client)
    run(database, terminal)
    row = read_event(database)
    assert row['classification_state'] == 'classified'
    assert row['snapshot_time'] is None
    assert decode_revision(row['snapshot_revision'])['frame_time'] is None


def test_api_still_active_is_not_a_terminal_snapshot(database):
    seed(database)
    client = real_client()
    client.get_tracked_event.return_value = event_row()
    with pytest.raises(TerminalEvidenceError, match='event_not_terminal'):
        client.acquire_terminal_image(read_event(database), allow_fallback=True)
    client.get_event_snapshot.assert_not_called()


def test_api_pruned_metadata_can_use_snapshot_after_durable_mqtt_end(database):
    seed(database)
    client = real_client()
    client.get_tracked_event.side_effect = FrigateReviewError('event_missing')
    image = client.acquire_terminal_image(read_event(database))
    assert image.content == b'terminal-jpeg'
    assert image.frame_time == 130


def test_cache_is_bounded_and_never_substitutes_other_event_or_camera(database):
    seed(database)
    client = real_client()
    for index in range(40):
        client.remember_event_snapshot(f'c{index}', b'image', event_row(f'c{index}'))
    assert len(client._snapshot_cache) == 32
    assert 'c0' not in client._snapshot_cache
    client.remember_event_snapshot('e1', b'wrong-event', event_row('other'))
    client.remember_event_snapshot('e1', b'wrong-camera', {**event_row(), 'camera': 'other'})
    client.get_event_snapshot.side_effect = FrigateError('unavailable', status_code=404)
    with pytest.raises(TerminalEvidenceError):
        client.acquire_terminal_image(read_event(database), allow_fallback=True)


def test_snapshot_download_limits_stream_and_closes_response():
    client = FrigateClient('http://fixture')
    response = Mock(status_code=200)
    response.iter_content.return_value = [b'image']
    with patch('app.frigate_client.requests.get', return_value=response) as get:
        assert client.get_event_snapshot('e1', crop=True, quality=95, max_bytes=8) == b'image'
    get.assert_called_once_with('http://fixture/api/events/e1/snapshot.jpg', params={'crop': 1, 'quality': 95},
                               timeout=(2, 2), stream=True, allow_redirects=False)
    response.close.assert_called_once()
    response.iter_content.return_value = [b'too-large']
    with patch('app.frigate_client.requests.get', return_value=response), pytest.raises(FrigateError):
        client.get_event_snapshot('e1', max_bytes=2)


def test_shared_preprocessing_and_actual_loaded_model_identity(monkeypatch):
    import speciesid

    image = io.BytesIO()
    Image.new('RGB', (32, 24)).save(image, format='JPEG')
    inference = Mock(return_value=[category()])
    monkeypatch.setattr(speciesid, 'classify', inference)
    assert speciesid.classify_snapshot(image.getvalue())[0].display_name == 'Parus major'
    assert inference.call_args.args[0].shape == (224, 224, 3)
    identity = terminal_model_identity(ClassifierRuntimeSnapshot(
        state='ready', model_id='aiy', model_version='v1', sha256='abc'))
    assert identity == 'aiy@v1:sha256:abc'
    assert terminal_model_identity(ClassifierRuntimeSnapshot()) is None


def test_old_snapshot_updates_do_not_erase_best_known_hint(database):
    seed(database, end=None)
    ingest_event(database, envelope({**event_row(), 'snapshot': {'frame_time': 150}}, 'update'), ['feeder'])
    ingest_event(database, envelope({**event_row(), 'snapshot': {'frame_time': 110}}, 'update'), ['feeder'])
    assert read_event(database)['snapshot_time'] == 150
    ingest_event(database, envelope(event_row(end=170), 'end'), ['feeder'])
    assert read_event(database)['snapshot_time'] == 150
    assert decode_revision(read_event(database)['snapshot_revision'])['terminal'] is True


def test_shared_interpreter_calls_are_serialized(monkeypatch):
    import speciesid

    entered, release, second_started = threading.Event(), threading.Event(), threading.Event()
    simultaneous = []
    active = [0]

    def invoke(_):
        active[0] += 1
        simultaneous.append(active[0])
        entered.set()
        assert release.wait(3)
        active[0] -= 1
        return []

    monkeypatch.setattr(speciesid, '_classify_locked', invoke)
    first = threading.Thread(target=speciesid.classify, args=(None,))

    def second_call():
        second_started.set()
        speciesid.classify(None)

    second = threading.Thread(target=second_call)
    first.start()
    try:
        assert entered.wait(3)
        second.start()
        assert second_started.wait(3)
    finally:
        release.set()
        first.join(timeout=3)
        if second.ident is not None:
            second.join(timeout=3)
    assert not first.is_alive() and not second.is_alive()
    assert simultaneous == [1, 1]


def test_terminal_api_false_positive_is_rejected_before_snapshot(database):
    seed(database)
    client = real_client()
    client.get_tracked_event.return_value = {**event_row(end=170), 'false_positive': True}
    terminal = processor(client)
    run(database, terminal)
    assert read_event(database)['reason'] == 'false_positive'
    terminal.classify_snapshot.assert_not_called()
    client.get_event_snapshot.assert_not_called()


def test_unusable_image_retries_then_success_uses_shared_preprocessing(database, monkeypatch):
    import speciesid

    seed(database)
    client = real_client()
    inference = Mock(return_value=[category()])
    monkeypatch.setattr(speciesid, 'classify', inference)
    terminal = processor(client, speciesid.classify_snapshot)
    run(database, terminal)
    assert read_event(database)['last_error'] == 'terminal:invalid_snapshot'
    assert read_event(database)['classification_state'] == 'pending'
    inference.assert_not_called()
    image = io.BytesIO()
    Image.new('RGB', (32, 24)).save(image, format='JPEG')
    client.get_event_snapshot.return_value = image.getvalue()
    run(database, terminal, now=205)
    assert read_event(database)['classification_state'] == 'classified'
    inference.assert_called_once()
