"""Isolated Slice 4 observations: species occurrences, never physical animals."""

import sqlite3
import threading
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from app import db
from app import review_repository as repository
from app.review_aggregation import (
    AGGREGATION_RULE,
    ReviewAggregator,
    aggregation_plan,
    finalize_review,
)
from app.review_ingestion import metadata_connection
from tests.test_terminal_classification import processor, run, seed


@pytest.fixture
def database(tmp_path):
    path = tmp_path / 'aggregation.db'
    db.ensure_schema(path)
    return path


def evidence(path, identities=('e1',), species=None):
    for identity in identities:
        seed(path, identity=identity)
    terminal = processor()
    run(path, terminal)
    with metadata_connection(path) as conn:
        repository.set_review_state(conn, 'r1', 'closing', reconciliation_status='synced')
        if species:
            for identity, name, score in species:
                conn.execute('UPDATE frigate_review_events SET scientific_name=?,score=? WHERE event_id=?',
                             (name, score, identity))


def rows(path, table='detections'):
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute(f'SELECT * FROM {table}')]


def finish(path):
    with metadata_connection(path) as conn:
        return finalize_review(conn, 'r1')


def worker(path, **kwargs):
    runner = ReviewAggregator(**kwargs)
    runner.run_batch(path, ['feeder'], threading.Event(), lambda: 200)
    return runner


def test_multi_species_and_same_species_tracks_are_occurrences_not_animals(database):
    evidence(database, ('a', 'b', 'c', 'd'), [
        ('a', 'Parus major', .96), ('b', 'Parus major', .73),
        ('c', 'Cyanistes caeruleus', .95), ('d', 'Cyanistes caeruleus', .54)])
    with metadata_connection(database) as conn:
        conn.execute("UPDATE frigate_review_events SET classification_state='rejected',reason='below_threshold' WHERE event_id='d'")
        conn.execute("UPDATE frigate_review_events SET start_time=110,end_time=172 WHERE event_id='b'")
    finish(database)
    grouped = {row['display_name']: row for row in rows(database)}
    assert set(grouped) == {'Parus major', 'Cyanistes caeruleus'}
    assert (grouped['Parus major']['score'], grouped['Parus major']['support_count'],
            grouped['Parus major']['frigate_event']) == (.96, 2, 'a')
    assert grouped['Parus major']['detection_time'] == datetime.fromtimestamp(101).isoformat(' ')
    assert grouped['Parus major']['observation_end_time'] == datetime.fromtimestamp(172).isoformat(' ')
    assert grouped['Cyanistes caeruleus']['support_count'] == 1
    assert all(row['aggregation_rule_version'] == AGGREGATION_RULE for row in grouped.values())
    review = rows(database, 'frigate_reviews')[0]
    assert (review['state'], review['aggregation_rule_version']) == ('finalized', AGGREGATION_RULE)
    # Two actual same-species birds and fragmentation intentionally share this result.
    assert 'bird_count' not in grouped['Parus major']
    assert [row['observation_id'] is not None for row in rows(database, 'frigate_review_events')] == [True, True, True, False]


@pytest.mark.parametrize('change', [
    "UPDATE frigate_reviews SET end_time=NULL,state='active'",
    "UPDATE frigate_reviews SET reconciliation_status='pending'",
    "UPDATE frigate_reviews SET reconciliation_status='incomplete'",
    "UPDATE frigate_review_events SET classification_state='pending',classification_phase=NULL",
    "UPDATE frigate_review_events SET classification_state='pending',next_retry_at=500,last_error='terminal:timeout'",
    "UPDATE frigate_review_events SET end_time=NULL",
    "UPDATE frigate_review_events SET last_error='membership_conflict:other'",
])
def test_unresolved_review_never_finalizes(database, change):
    evidence(database)
    with metadata_connection(database) as conn:
        conn.execute(change)
        assert aggregation_plan(conn, 'r1') is None
    finish(database)
    assert not rows(database)
    assert rows(database, 'frigate_reviews')[0]['state'] != 'finalized'


@pytest.mark.parametrize('state', ['rejected', 'failed'])
def test_no_accepted_species_finalizes_zero_rows(database, state):
    evidence(database)
    with metadata_connection(database) as conn:
        conn.execute('UPDATE frigate_review_events SET classification_state=?,reason=?', (state, 'unusable'))
    finish(database)
    assert not rows(database)
    assert rows(database, 'frigate_reviews')[0]['state'] == 'finalized'


def test_scientific_identity_normalized_not_common_name(database):
    evidence(database, ('a', 'b'), [('a', 'Parus major', .8), ('b', ' parus   MAJOR ', .95)])
    with metadata_connection(database) as conn:
        conn.execute("UPDATE frigate_review_events SET common_name=event_id")
    finish(database)
    assert len(rows(database)) == 1
    assert rows(database)[0]['support_count'] == 2
    assert rows(database)[0]['display_name'] == 'Parus major'
    assert rows(database)[0]['frigate_event'] == 'b'


@pytest.mark.parametrize('snap_a,snap_b,start_a,start_b,winner', [
    (100, 110, 100, 100, 'b'), (None, 100, 100, 100, 'b'),
    (100, 100, 105, 100, 'b'), (100, 100, 100, 100, 'a')])
def test_representative_ties_are_stable(database, snap_a, snap_b, start_a, start_b, winner):
    evidence(database, ('a', 'b'))
    with metadata_connection(database) as conn:
        for identity, snapshot, start in [('a', snap_a, start_a), ('b', snap_b, start_b)]:
            conn.execute('UPDATE frigate_review_events SET snapshot_time=?,start_time=? WHERE event_id=?', (snapshot, start, identity))
    finish(database)
    assert rows(database)[0]['frigate_event'] == winner


def test_transaction_rolls_back_second_species_and_restart_converges(database):
    evidence(database, ('a', 'b'), [('a', 'Parus major', .9), ('b', 'Cyanistes caeruleus', .95)])
    original = repository.create_grouped_observation
    calls = []

    def fail_second(*args, **kwargs):
        calls.append(1)
        if len(calls) == 2:
            raise sqlite3.OperationalError('injected')
        return original(*args, **kwargs)

    with patch.object(repository, 'create_grouped_observation', side_effect=fail_second), pytest.raises(sqlite3.OperationalError):
        finish(database)
    assert not rows(database)
    assert rows(database, 'frigate_reviews')[0]['state'] == 'closing'
    finish(database)
    before = rows(database)
    for _ in range(3):
        assert finish(database) == []
        worker(database)
    assert rows(database) == before


def test_historical_rows_untouched_and_exact_member_legacy_replaced(database):
    evidence(database, ('a', 'b'))
    with metadata_connection(database) as conn:
        for identity in ('historical', 'a', 'b'):
            conn.execute('INSERT INTO detections(detection_time,display_name,frigate_event,camera_name) VALUES(?,?,?,?)',
                         ('2000-01-01 01:02:03', 'Old name', identity, 'feeder'))
    old = rows(database)[0]
    finish(database)
    assert len(rows(database)) == 2
    assert next(row for row in rows(database) if row['frigate_event'] == 'historical') == old


def test_suppression_prevents_recreation(database):
    evidence(database)
    with metadata_connection(database) as conn:
        repository.suppress_species(conn, 'r1', ' PARUS major ')
    finish(database)
    assert not rows(database)
    assert rows(database, 'frigate_reviews')[0]['state'] == 'finalized'


def test_media_transfer_reference_cleanup_and_publication_once(database):
    evidence(database, ('a', 'b'))
    with metadata_connection(database) as conn:
        for identity in ('a', 'b'):
            conn.execute("INSERT INTO detections(detection_time,display_name,frigate_event,wamf_snapshot_path,wamf_clip_path) VALUES('2000-01-01',?,?,?,?)",
                         ('Parus major', identity, identity + '.jpg', identity + '.mp4'))
    publish, cleanup, archive = Mock(), Mock(), Mock()
    worker(database, publish=publish, cleanup=cleanup, archive=archive)
    row = rows(database)[0]
    assert (row['wamf_snapshot_path'], row['wamf_clip_path']) == ('a.jpg', 'a.mp4')
    assert {call.args[0] for call in cleanup.call_args_list} == {'b.jpg', 'b.mp4'}
    archive.assert_not_called()
    publish.assert_not_called()  # Transition rows already emitted legacy publications.
    worker(database, publish=publish, cleanup=cleanup, archive=archive)
    publish.assert_not_called()


def test_new_media_is_attached_and_failure_backoff_is_durable(database):
    evidence(database)
    archive, publish, cleanup = Mock(return_value=('image.jpg', 'clip.mp4')), Mock(), Mock()
    with patch('app.review_aggregation.finalize_review', side_effect=sqlite3.OperationalError('injected')):
        worker(database, archive=archive, publish=publish, cleanup=cleanup)
    assert not rows(database)
    publish.assert_not_called()
    assert {call.args[0] for call in cleanup.call_args_list} == {'image.jpg', 'clip.mp4'}
    review = rows(database, 'frigate_reviews')[0]
    assert (review['last_error'], review['next_retry_at']) == ('aggregation_failed', 205)
    archive.reset_mock()
    worker(database, archive=archive, publish=publish)
    archive.assert_not_called()
    ReviewAggregator(archive=archive, publish=publish).run_batch(database, ['feeder'], threading.Event(), lambda: 206)
    assert rows(database)[0]['wamf_snapshot_path'] == 'image.jpg'
    publish.assert_called_once()


def test_nonbird_never_produces_species(database):
    evidence(database, ('a', 'b'))
    with metadata_connection(database) as conn:
        conn.execute("UPDATE frigate_review_events SET label='cat',classification_state='rejected',reason='non_bird' WHERE event_id='b'")
    finish(database)
    assert rows(database)[0]['support_count'] == 1


def test_worker_does_not_starve_ready_review_behind_pending_reviews(database):
    evidence(database)
    with metadata_connection(database) as conn:
        for number in range(6):
            identity = str(number)
            repository.upsert_review(conn, identity, camera_name='feeder', start_time=0, end_time=2)
            repository.set_review_state(conn, identity, 'closing', reconciliation_status='synced')
            repository.upsert_event(conn, 'pending' + identity, camera_name='feeder', review_id=identity)
    worker(database)
    assert len(rows(database)) == 1


def test_grouped_rows_use_existing_queries(database, monkeypatch):
    from app import queries

    evidence(database, ('a', 'b'))
    finish(database)
    monkeypatch.setattr(queries, 'DBPATH', database)
    assert queries.get_detection_count() == 1
    assert queries.get_species_stats('Parus major') is not None


def test_callback_waits_unknown_and_degraded_but_falls_back_confirmed_unsupported(database, monkeypatch):
    import speciesid
    from tests.test_review_ingestion import envelope, event_row

    monkeypatch.setattr(speciesid, 'DBPATH', database)
    monkeypatch.setattr(speciesid, 'config', {'mqtt': {'topic_prefix': 'custom'}, 'frigate': {'camera': ['feeder']}})
    legacy = Mock()
    monkeypatch.setattr(speciesid, '_process_live_message', legacy)
    reconciler = SimpleNamespace(status=SimpleNamespace(api_state='unknown'), wake=Mock())
    context = speciesid.MqttConsumerContext(speciesid._mqtt_runtime_settings(), Mock(), reconciler=reconciler)
    message = SimpleNamespace(topic='custom/events', retain=False, payload=envelope(event_row(), 'new'))
    for state in ('unknown', 'degraded', 'available'):
        reconciler.status.api_state = state
        speciesid.on_message(Mock(), context, message)
    legacy.assert_not_called()
    reconciler.status.api_state = 'unsupported'
    speciesid.on_message(Mock(), context, message)
    legacy.assert_called_once()


def test_grouped_deletion_suppresses_preserves_review_and_shared_media(database, flask_client, monkeypatch):
    import webui

    evidence(database)
    finish(database)
    with metadata_connection(database) as conn:
        conn.execute("UPDATE detections SET wamf_snapshot_path='shared.jpg'")
        conn.execute("INSERT INTO detections(detection_time,display_name,frigate_event,wamf_snapshot_path) VALUES('2000-01-01','other','history','shared.jpg')")
    monkeypatch.setattr(webui, 'DBPATH', database)
    monkeypatch.setattr(webui, 'is_admin_auth_enabled', lambda: False)
    delete_media = Mock(return_value=[])
    monkeypatch.setattr(webui, 'delete_wamf_media_files', delete_media)
    response = flask_client.delete('/detections/e1')
    assert response.status_code == 200
    delete_media.assert_called_once_with(None, None)
    assert rows(database, 'frigate_review_events')[0]['observation_id'] is None
    assert rows(database, 'frigate_reviews')[0]['state'] == 'finalized'
    assert rows(database, 'frigate_review_suppressions')[0]['scientific_name'] == 'Parus major'
    worker(database)
    assert [row['frigate_event'] for row in rows(database)] == ['history']


def test_grouped_recent_api_and_history_render(database, flask_client, monkeypatch):
    import webui
    from app import queries

    evidence(database)
    finish(database)
    monkeypatch.setattr(webui, 'DBPATH', database)
    monkeypatch.setattr(queries, 'DBPATH', database)
    response = flask_client.get('/api/detections/recent')
    assert response.status_code == 200
    assert len(response.json) == 1
    assert response.json[0]['frigate_event'] == 'e1'
    assert response.json[0]['display_name'] == 'Parus major'
    assert flask_client.get('/recent').status_code == 200


def test_postcommit_notifications_one_per_species_not_support(database, monkeypatch):
    import speciesid

    evidence(database, ('a', 'b', 'c'), [('a', 'Parus major', .9), ('b', 'Parus major', .8), ('c', 'Cyanistes caeruleus', .95)])
    monkeypatch.setattr(speciesid, 'DBPATH', database)
    monkeypatch.setattr(speciesid, 'config', {'bridge': {}})
    post, sublabel, first = Mock(), Mock(), Mock()
    monkeypatch.setattr(speciesid, 'post_observation_event', post)
    monkeypatch.setattr(speciesid, 'set_sublabel', sublabel)
    monkeypatch.setattr(speciesid, 'publish_new_species', first)
    client = Mock()
    publish = lambda row: speciesid.publish_grouped_observation(client, row)
    worker(database, publish=publish)
    worker(database, publish=publish)
    assert post.call_count == client.publish.call_count == sublabel.call_count == 2
    assert {call.kwargs['scientific_name'] for call in post.call_args_list} == {'Parus major', 'Cyanistes caeruleus'}


def test_archive_bounded_atomic_representative_only(tmp_path, monkeypatch):
    from app import archive_media

    monkeypatch.setattr(archive_media, 'ensure_storage_paths', lambda: None)
    monkeypatch.setattr(archive_media, 'get_snapshots_path', lambda: tmp_path)
    monkeypatch.setattr(archive_media, 'get_clips_path', lambda: tmp_path)
    response = Mock()
    response.iter_content.return_value = [b'media']
    client = Mock()
    client.stream_event_media.return_value = response
    paths = archive_media.archive_representative(client, 'actual-event-id', threading.Event())
    assert paths == (str(tmp_path / 'actual-event-id.jpg'), str(tmp_path / 'actual-event-id.mp4'))
    assert (tmp_path / 'actual-event-id.jpg').read_bytes() == b'media'
    assert response.close.call_count == 2
    assert not list(tmp_path.glob('*.pending'))
    assert all(call.kwargs['timeout'] == (2, 2) for call in client.stream_event_media.call_args_list)


def test_archive_shutdown_does_not_leave_partial_media(tmp_path, monkeypatch):
    from app import archive_media

    monkeypatch.setattr(archive_media, 'ensure_storage_paths', lambda: None)
    monkeypatch.setattr(archive_media, 'get_snapshots_path', lambda: tmp_path)
    monkeypatch.setattr(archive_media, 'get_clips_path', lambda: tmp_path)
    stop = threading.Event()

    def chunks(**kwargs):
        yield b'partial'
        stop.set()
        yield b'later'

    response = Mock()
    response.iter_content.side_effect = chunks
    client = Mock()
    client.stream_event_media.return_value = response
    assert archive_media.archive_representative(client, 'e1', stop) == (None, None)
    assert not list(tmp_path.iterdir())
    response.close.assert_called_once()


def test_finalization_health_counts(database):
    from app.review_reconciliation import ReviewReconciler

    evidence(database)
    runtime = Mock()
    reconciler = ReviewReconciler(database, ['feeder'], Mock(), runtime)
    reconciler.refresh_counts()
    assert reconciler.status.aggregation_ready == 1
    finish(database)
    reconciler.refresh_counts()
    assert reconciler.status.aggregation_ready == 0
    assert reconciler.status.finalized_reviews == reconciler.status.grouped_observations == 1
    assert reconciler.status.valid()


def test_relative_and_absolute_media_references_protect_same_file(database, tmp_path, monkeypatch):
    import wamf_paths
    from app.archive_media import unreferenced_archive_paths

    snapshots, clips = tmp_path / 'snapshots', tmp_path / 'clips'
    snapshots.mkdir()
    clips.mkdir()
    monkeypatch.setattr(wamf_paths, 'get_snapshots_path', lambda config=None: snapshots)
    monkeypatch.setattr(wamf_paths, 'get_clips_path', lambda config=None: clips)
    with metadata_connection(database) as conn:
        conn.execute("INSERT INTO detections(detection_time,wamf_snapshot_path) VALUES('2000-01-01',?)",
                     (str(snapshots / 'shared.jpg'),))
        assert unreferenced_archive_paths(conn, ['shared.jpg', str(snapshots / 'shared.jpg'), 'unused.jpg']) == {'unused.jpg'}


def test_partial_existing_observation_completed_without_new_publication(database):
    evidence(database, ('a', 'b'))
    with metadata_connection(database) as conn:
        identity = repository.create_grouped_observation(conn, review_id='r1', representative_event_id='a',
            detection_time='1970-01-01 00:01:41', observation_end_time=None, support_count=1,
            aggregation_rule_version=AGGREGATION_RULE, score=.9)
    publish = Mock()
    worker(database, publish=publish)
    assert len(rows(database)) == 1
    assert rows(database)[0]['id'] == identity
    assert rows(database)[0]['support_count'] == 2
    publish.assert_not_called()


def test_existing_suppressed_partial_observation_removed(database):
    evidence(database)
    with metadata_connection(database) as conn:
        repository.create_grouped_observation(conn, review_id='r1', representative_event_id='e1',
            detection_time='1970-01-01 00:01:41', observation_end_time=None, support_count=1,
            aggregation_rule_version=AGGREGATION_RULE, score=.9)
        repository.suppress_species(conn, 'r1', 'Parus major')
    finish(database)
    assert not rows(database)
    assert rows(database, 'frigate_reviews')[0]['state'] == 'finalized'
