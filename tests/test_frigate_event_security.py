"""Untrusted Frigate IDs across MQTT, SQLite, public JSON and media consumers."""

import importlib
import json
import logging
import sqlite3
import sys
from html.parser import HTMLParser
from unittest.mock import MagicMock, Mock

import pytest

from app import archive_media
from app.db import ensure_schema
from app.frigate_events import is_valid_frigate_event

HOSTILE_IDS = [
    "evt');window.F001=1;//", 'evt" onerror="window.F001=1',
    '../escape', 'evt/child', 'evt\\child', 'evt%2Fchild',
    'evt?query=value', 'evt#fragment', 'evt\r\nFORGED\x1b[2J',
    '', '.', '..', None, 123, {}, 'caf\u00e9',
]
VALID_IDS = ['1700000000.123456-abcdef', '1742045938.086773-9n5kxw',
             'evt-test-999', 'camera_1.event-ABC', '550e8400-e29b-41d4-a716-446655440000']


@pytest.mark.parametrize('event_id', HOSTILE_IDS)
def test_identifier_rejects_unsafe_values(event_id):
    assert not is_valid_frigate_event(event_id)


@pytest.mark.parametrize('event_id', VALID_IDS)
def test_identifier_preserves_supported_formats(event_id):
    assert is_valid_frigate_event(event_id)


@pytest.fixture
def detection_db(tmp_path):
    path = str(tmp_path / 'detections.db')
    ensure_schema(path)
    return path


@pytest.fixture
def classifier(flask_client, monkeypatch, detection_db):
    for name in ('numpy', 'tflite_support', 'tflite_support.task', 'PIL', 'PIL.Image'):
        if name not in sys.modules:
            monkeypatch.setitem(sys.modules, name, MagicMock())
    module = importlib.import_module('speciesid')
    monkeypatch.setattr(module, 'config', {
        'frigate': {'camera': ['birdcam'], 'frigate_url': 'http://frigate.invalid'},
        'classification': {'threshold': 0.7},
    })
    monkeypatch.setattr(module, 'DBPATH', detection_db)
    return module


@pytest.mark.parametrize('event_id', HOSTILE_IDS)
def test_mqtt_rejects_hostile_id_before_requests_logs_or_persistence(
    classifier, monkeypatch, detection_db, event_id, caplog
):
    get = Mock()
    classify = Mock()
    archive = Mock()
    monkeypatch.setattr(classifier.requests, 'get', get)
    monkeypatch.setattr(classifier, 'classify', classify)
    monkeypatch.setattr(classifier, 'archive_snapshot', archive)
    monkeypatch.setattr(classifier, 'archive_clip', archive)
    message = Mock(retain=False, topic='frigate/events')
    message.payload = json.dumps({'type': 'new', 'after': {
        'camera': 'birdcam', 'label': 'bird', 'id': event_id,
        'start_time': 1700000000,
    }})
    client = Mock()
    with caplog.at_level(logging.DEBUG, logger='speciesid'):
        classifier.on_message(client, None, message)
    get.assert_not_called()
    classify.assert_not_called()
    archive.assert_not_called()
    client.publish.assert_not_called()
    with sqlite3.connect(detection_db) as database:
        assert database.execute('SELECT COUNT(*) FROM detections').fetchone()[0] == 0
    assert [record.getMessage() for record in caplog.records] == [
        'Ignoring detection with invalid Frigate event identifier',
    ]


@pytest.mark.parametrize('event_id', VALID_IDS)
def test_valid_mqtt_identifier_reaches_sqlite_and_public_api(
    classifier, monkeypatch, detection_db, flask_client, event_id
):
    import webui
    from app import queries
    from tests.test_frigate_sublabel import _run_on_message

    monkeypatch.setattr(queries, 'DBPATH', detection_db)
    monkeypatch.setattr(webui, 'config', {**webui.config, 'admin': {'auth_enabled': False}})
    _run_on_message(detection_db, wamf_score=0.92, event_id=event_id)
    with sqlite3.connect(detection_db) as database:
        assert database.execute('SELECT frigate_event FROM detections').fetchall() == [(event_id,)]
    response = flask_client.get('/api/detections/recent')
    assert response.status_code == 200 and response.mimetype == 'application/json'
    assert response.json[0]['frigate_event'] == event_id


@pytest.mark.parametrize('operation', ['archive_snapshot', 'archive_clip'])
@pytest.mark.parametrize('event_id', HOSTILE_IDS)
def test_archive_rejects_invalid_id_before_network_files_or_system_events(
    operation, event_id, monkeypatch, caplog
):
    get = Mock()
    ensure = Mock()
    events = Mock()
    monkeypatch.setattr(archive_media.requests, 'get', get)
    monkeypatch.setattr(archive_media, 'ensure_storage_paths', ensure)
    monkeypatch.setattr(archive_media, 'log_system_event', events)
    with caplog.at_level(logging.WARNING):
        assert getattr(archive_media, operation)('http://frigate.invalid', event_id) is None
    get.assert_not_called()
    ensure.assert_not_called()
    events.assert_not_called()
    assert len(caplog.records) == 1
    assert 'invalid Frigate event identifier' in caplog.records[0].getMessage()
    assert '\n' not in caplog.records[0].getMessage()


@pytest.mark.parametrize('operation,extension', [('archive_snapshot', 'jpg'), ('archive_clip', 'mp4')])
@pytest.mark.parametrize('event_id', VALID_IDS)
def test_archives_preserve_identifier_urls_and_filenames(monkeypatch, tmp_path, operation, extension, event_id):
    monkeypatch.setattr(archive_media, 'ensure_storage_paths', Mock())
    monkeypatch.setattr(archive_media, 'get_snapshots_path', lambda: tmp_path)
    monkeypatch.setattr(archive_media, 'get_clips_path', lambda: tmp_path)
    monkeypatch.setattr(archive_media, 'log_system_event', Mock())
    response = Mock(status_code=200, content=b'media')
    response.iter_content.return_value = iter([b'me', b'', b'dia'])
    get = Mock(return_value=response)
    monkeypatch.setattr(archive_media.requests, 'get', get)
    result = getattr(archive_media, operation)('http://frigate.invalid', event_id)
    target = tmp_path / f'{event_id}.{extension}'
    assert result == str(target) and target.read_bytes() == b'media'
    media_name = 'snapshot.jpg' if extension == 'jpg' else 'clip.mp4'
    assert get.call_args.args[0] == f'http://frigate.invalid/api/events/{event_id}/{media_name}'


@pytest.mark.parametrize('event_id', VALID_IDS)
def test_media_proxy_preserves_identifier_urls(flask_client, monkeypatch, event_id):
    import webui
    monkeypatch.setattr(webui, 'config', {**webui.config,
        'admin': {'auth_enabled': False}, 'frigate': {'frigate_url': 'http://frigate.invalid'}})
    response = Mock(status_code=200, headers={'Content-Type': 'image/jpeg'})
    response.raw.stream.return_value = iter([b'media'])
    get = Mock(return_value=response)
    monkeypatch.setattr('app.frigate_proxy.requests.get', get)
    downstream = flask_client.get(f'/frigate/{event_id}/thumbnail.jpg')
    assert downstream.data == b'media' and downstream.mimetype == 'image/jpeg'
    downstream.close()
    response.close.assert_called_once_with()
    assert get.call_args.args[0] == f'http://frigate.invalid/api/events/{event_id}/thumbnail.jpg'


@pytest.mark.parametrize('event_id', HOSTILE_IDS[:4])
def test_sublabel_and_mqtt_publication_reject_invalid_ids(classifier, monkeypatch, event_id):
    post = Mock()
    monkeypatch.setattr(classifier.requests, 'post', post)
    client = Mock()
    assert classifier.set_sublabel('http://frigate.invalid', event_id, 'American Robin') is None
    classifier.publish_new_species(client, 'Robin', 'Turdus migratorius', 0.9, 'birdcam', event_id)
    post.assert_not_called()
    client.publish.assert_not_called()


class _Images(HTMLParser):
    def __init__(self):
        super().__init__()
        self.images = []

    def handle_starttag(self, tag, attrs):
        if tag == 'img':
            self.images.append(dict(attrs))


@pytest.fixture
def legacy_detection(detection_db, monkeypatch, flask_client):
    import webui
    from app import queries

    event_id = "evt');window.F001=1;//\" onerror=\"window.F001=1"
    monkeypatch.setattr(queries, 'DBPATH', detection_db)
    monkeypatch.setattr(webui, 'config', {**webui.config, 'admin': {'auth_enabled': False}})
    with sqlite3.connect(detection_db) as database:
        database.execute('''INSERT INTO detections
            (detection_time, detection_index, score, display_name, category_name,
             frigate_event, camera_name, wamf_snapshot_path, wamf_clip_path)
            VALUES (?, 42, 0.91, 'Turdus migratorius', 'bird', ?, 'birdcam', ?, ?)''',
            ('2024-06-01 08:30:00', event_id, '/snapshots/safe.jpg', '/clips/safe.mp4'))
    return event_id


def test_public_api_keeps_legacy_hostile_identifier_as_json_data(flask_client, legacy_detection):
    response = flask_client.get('/api/detections/recent')
    assert response.status_code == 200 and response.mimetype == 'application/json'
    assert response.json[0]['frigate_event'] == legacy_detection


@pytest.mark.parametrize('route', [
    '/', '/recent', '/detections/by_hour/2024-06-01/8',
    '/detections/by_scientific_name/Turdus%20migratorius/2024-06-01',
])
def test_initial_detection_images_use_data_attributes_not_executable_handlers(
    flask_client, legacy_detection, route, monkeypatch
):
    import webui
    monkeypatch.setattr(webui, 'queue_metadata_refresh', Mock())
    response = flask_client.get(route)
    assert response.status_code == 200
    parsed = _Images()
    parsed.feed(response.get_data(as_text=True))
    images = [image for image in parsed.images if 'data-frigate-event' in image]
    assert len(images) == 1
    assert images[0]['data-frigate-event'] == legacy_detection
    assert not any(name.startswith('on') for name in images[0])
    assert images[0]['data-snapshot-url'].endswith('/safe.jpg')
    assert images[0]['data-clip-url'].endswith('/safe.mp4')
