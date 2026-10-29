"""M4 runtime health and presentation: no broker or live services."""

from dataclasses import replace
from unittest.mock import MagicMock

import pytest

from app import health
from app.mqtt_presentation import build_mqtt_presentation
from app.mqtt_runtime import MqttRuntimeSnapshot
from app.review_runtime import ReviewRuntimeSnapshot


def config():
    return {
        'mqtt': {'host': 'broker.local', 'port': 2883, 'topic_prefix': 'frigate',
                 'authentication': {'enabled': True, 'username': 'PRIVATE-USER', 'password': 'PRIVATE-PASSWORD'}},
        'retention': {'schedule': {'timezone': 'Europe/London'}},
    }


def connected(**changes):
    return replace(MqttRuntimeSnapshot(
        worker_generation='generation', is_current=True,
        connection_state='connected', subscription_state='subscribed',
        worker_started_at='2026-07-18T11:00:00+00:00',
        last_connected_at='2026-07-18T11:01:00+00:00',
        updated_at='2026-07-18T11:01:01+00:00',
    ), **changes)


@pytest.mark.parametrize('api,observed,expected', [
    ('unknown', None, 'Unknown'), ('unknown', '2026-07-18T11:00:00Z', 'Available'),
    ('available', None, 'Available'), ('degraded', None, 'Degraded'),
    ('unsupported', None, 'Unavailable'), ('unsupported', '2026-07-18T11:00:00Z', 'Degraded'),
])
def test_review_capability_distinguishes_silence_api_and_subscription(api, observed, expected):
    view = build_mqtt_presentation(connected(
        review_subscription_state='subscribed', last_review_message_at=observed,
        reviews=ReviewRuntimeSnapshot(worker_state='running', api_state=api, unmatched_events=3),
    ), config())
    fields = dict(view['details'])
    assert fields['Configured review topic'] == 'frigate/reviews'
    assert fields['Review capability'] == expected
    assert fields['Unmatched tracked events'] == 3
    assert view['online'] is True


def test_review_diagnostics_never_render_arbitrary_error_text():
    view = build_mqtt_presentation(connected(
        review_last_issue='PRIVATE-EXCEPTION',
        reviews=ReviewRuntimeSnapshot(worker_state='running', error_code='PRIVATE-PAYLOAD'),
    ), config())
    assert 'PRIVATE' not in repr(view)


def test_terminal_evidence_status_is_compact_and_safe():
    fields = dict(build_mqtt_presentation(connected(reviews=ReviewRuntimeSnapshot(
        worker_state='running', api_state='available', terminal_pending=3, terminal_retrying=1,
        terminal_classified=4, terminal_rejected=2, terminal_failed=1,
    )), config())['details'])
    assert fields['Terminal classification pending / retrying'] == '3 / 1'
    assert fields['Terminal classified / rejected / failed'] == '4 / 2 / 1'


@pytest.mark.parametrize('connection,subscription,current,status,online', [
    ('connected', 'subscribed', True, 'Connected', True),
    ('connected', 'pending', True, 'Unknown', None),
    ('subscribing', 'pending', True, 'Subscribing', False),
    ('subscribing', 'failed', True, 'Subscription failed', False),
    ('starting', 'unknown', True, 'Starting', False),
    ('reconnecting', 'unknown', True, 'Reconnecting', False),
    ('disconnected', 'unknown', True, 'Disconnected', False),
    ('stopped', 'unknown', False, 'Stale', None),
    ('connected', 'subscribed', False, 'Stale', None),
    ('unknown', 'unknown', True, 'Unknown', None),
])
def test_mapping(connection, subscription, current, status, online):
    view = build_mqtt_presentation(connected(connection_state=connection, subscription_state=subscription,
                                             is_current=current), config())
    assert view['status'] == status
    assert view['online'] is online
    assert (view['tone'] == 'success') is (status == 'Connected')


@pytest.mark.parametrize('snapshot', [None, {}, object(), MqttRuntimeSnapshot(),
                                     connected(connection_state=[]), connected(subscription_state={}),
                                     connected(is_current='yes')])
def test_unknown_or_malformed_snapshot_is_safe(snapshot):
    view = build_mqtt_presentation(snapshot, config())
    assert view['status'] == 'Unknown'
    assert view['online'] is None


@pytest.mark.parametrize('mqtt', [{}, None, {'host': ''}, {'host': 'broker', 'port': 99999},
                                  {'host': 'broker', 'topic_prefix': []},
                                  {'host': 'mqtt://PRIVATE-USER:PRIVATE-PASSWORD@broker'}])
def test_setup_overrides_old_connected_state_and_redacts_broker(mqtt):
    view = build_mqtt_presentation(connected(), {'mqtt': mqtt})
    assert view['status'] == 'Setup required'
    assert view['online'] is None
    assert 'PRIVATE' not in repr(view)


def test_unrelated_setup_does_not_hide_operational_mqtt():
    source = {**config(), 'frigate': [], 'classification': {'model': None}, 'camera': None}
    assert build_mqtt_presentation(connected(), source)['status'] == 'Connected'


def test_safe_details_timezone_and_quiet_topic():
    view = build_mqtt_presentation(connected(), config())
    details = dict(view['details'])
    assert details['Configured broker'] == 'broker.local:2883'
    assert details['Configured event topic'] == 'frigate/events'
    assert details['Last connected'] == '18 Jul 2026 · 12:01 BST'
    assert details['Last event message received'] == 'No events received yet'
    assert view['online'] is True
    assert view['timezone_note'] == 'Times shown in Europe/London'
    assert 'PRIVATE' not in repr(view)


def test_bad_timestamps_and_disconnect_reasons_never_echo_input():
    view = build_mqtt_presentation(connected(
        last_event_message_at='PRIVATE-PAYLOAD', last_connected_at='2026-01-01T00:00:00',
        last_disconnect_reason=['PRIVATE-EXCEPTION'], last_disconnected_at='PRIVATE',
    ), config())
    assert dict(view['details'])['Last event message received'] == 'Unknown'
    assert dict(view['details'])['Disconnect reason'] == 'Not recorded'
    assert 'PRIVATE' not in repr(view)


def test_config_unavailable_and_invalid_timezone_are_isolated():
    view = build_mqtt_presentation(connected(), None)
    assert view['status'] == 'Unknown'
    assert view['online'] is None
    source = config()
    source['retention']['schedule']['timezone'] = 'bad'
    view = build_mqtt_presentation(connected(last_disconnect_reason='unexpected_disconnect'), source)
    assert view['status'] == 'Connected'
    assert dict(view['details'])['Disconnect reason'] == 'Unexpected disconnection'
    assert dict(view['details'])['Last connected'].endswith('UTC')


@pytest.fixture
def health_dependencies(monkeypatch):
    monkeypatch.setattr(health, 'preflight', lambda config: [])
    monkeypatch.setattr(health, 'frigate_health_cache', MagicMock())
    health.frigate_health_cache.get.return_value = {'online': True, 'degraded': False}
    monkeypatch.setattr(health, 'connect_db', MagicMock())
    monkeypatch.setattr(health.shutil, 'disk_usage', lambda path: (100, 10, 90))
    for name in ('get_snapshots_path', 'get_clips_path'):
        path = MagicMock()
        path.return_value.exists.return_value = True
        monkeypatch.setattr(health, name, path)
    monkeypatch.setattr(health, '_detection_worker_enabled', None)


@pytest.mark.parametrize('snapshot,overall', [(connected(), 'healthy'),
    (connected(connection_state='starting', subscription_state='unknown'), 'degraded'),
    (connected(is_current=False), 'degraded'), (None, 'degraded')])
def test_overall_health_uses_real_consumer(snapshot, overall, health_dependencies):
    result = health.calculate_system_health({**config(), 'frigate': {'frigate_url': 'http://frigate'}},
                                          mqtt_runtime_status=lambda: snapshot)
    assert result['overall_state'] == overall


def test_admin_render_reads_once_and_does_not_probe(flask_client, monkeypatch, health_dependencies, caplog):
    import webui
    from app import mqtt_connection
    from routes import admin

    monkeypatch.setattr(webui, 'config', {'admin': {'auth_enabled': False}})
    source = {**config(), 'frigate': {'frigate_url': 'http://frigate'}}
    monkeypatch.setattr(health, 'load_config', lambda: source)
    monkeypatch.setattr(admin, 'load_runtime_config', lambda: source)
    reader = MagicMock(return_value=connected(last_event_message_at='2026-07-18T11:02:00Z'))
    monkeypatch.setitem(webui.app.extensions, 'mqtt_runtime_status', reader)
    probe = MagicMock(side_effect=AssertionError('Health must not probe MQTT'))
    monkeypatch.setattr(mqtt_connection, 'probe_mqtt_connection', probe)
    response = flask_client.get('/admin')
    assert response.status_code == 200
    assert b'Connected' in response.data
    assert b'18 Jul 2026' in response.data
    assert b'PRIVATE' not in response.data
    assert 'PRIVATE' not in caplog.text
    reader.assert_called_once()
    health.frigate_health_cache.get.assert_called_once()
    probe.assert_not_called()
    # Cache is request-local, not a stale cross-request health cache.
    flask_client.get('/admin')
    assert reader.call_count == 2


@pytest.mark.parametrize('snapshot,expected', [(connected(), True),
    (connected(connection_state='subscribing', subscription_state='pending'), False),
    (connected(is_current=False), None), (None, None)])
def test_compatibility_api_keeps_fields_with_runtime_boolean(flask_client, monkeypatch, health_dependencies,
                                                           snapshot, expected):
    import webui

    monkeypatch.setattr(webui, 'config', {'admin': {'auth_enabled': False}, 'api': {'token_auth_enabled': False}})
    monkeypatch.setattr(health, 'load_config', lambda: {**config(), 'frigate': {'frigate_url': 'http://frigate'}})
    monkeypatch.setitem(webui.app.extensions, 'mqtt_runtime_status', lambda: snapshot)
    monkeypatch.setattr(webui, 'get_retention_status', lambda: None)
    response = flask_client.get('/admin/api/health')
    assert response.status_code == 200
    assert response.json['mqtt'] is expected
    assert set(response.json) == {'version', 'frigate', 'mqtt', 'database', 'archive_storage',
                                  'disk_used_percent', 'last_retention_run', 'orphan_count', 'missing_count'}
    assert 'PRIVATE' not in response.get_data(as_text=True)


@pytest.mark.parametrize('failure', ['runtime', 'config'])
def test_failure_isolation_keeps_admin_usable(flask_client, monkeypatch, health_dependencies, failure):
    import webui
    from routes import admin

    monkeypatch.setattr(webui, 'config', {'admin': {'auth_enabled': False}})
    source = {**config(), 'frigate': {'frigate_url': 'http://frigate'}}
    failing = MagicMock(side_effect=ValueError('PRIVATE-EXCEPTION'))
    monkeypatch.setattr(health, 'load_config', failing if failure == 'config' else lambda: source)
    monkeypatch.setattr(admin, 'load_runtime_config', failing if failure == 'config' else lambda: source)
    monkeypatch.setitem(webui.app.extensions, 'mqtt_runtime_status', failing if failure == 'runtime' else connected)
    response = flask_client.get('/admin')
    assert response.status_code == 200
    assert b'Unknown' in response.data
    assert b'PRIVATE' not in response.data


@pytest.mark.parametrize('source,expected', [
    ({'health': {'check_interval_seconds': 17}, 'bridge': {'health_check_interval_seconds': 2}}, 17),
    ({'perch': {'health_check_interval_seconds': 19}, 'bridge': {'health_check_interval_seconds': 2}}, 19),
    ({'bridge': {'health_check_interval_seconds': 23}}, 23),
])
def test_monitor_interval_uses_canonical_precedence_with_legacy_fallback(source, expected):
    assert health._health_check_interval(source) == expected


def test_monitor_uses_runtime_and_canonical_interval(health_dependencies, monkeypatch):
    source = {**config(), 'frigate': {'frigate_url': 'http://frigate'},
              'health': {'check_interval_seconds': 17}, 'bridge': {'health_check_interval_seconds': 2}}
    monkeypatch.setattr(health, 'load_config', lambda: source)
    stop = MagicMock()
    stop.is_set.return_value = False
    stop.wait.return_value = True
    reader = MagicMock(return_value=connected())
    monkeypatch.setattr(health, 'record_health_transition', MagicMock())
    health.health_monitor_loop(stop, mqtt_runtime_status=reader)
    reader.assert_called_once()
    stop.wait.assert_called_once_with(17)


def test_native_flask_and_monitor_share_the_explicit_m3_read_boundary(monkeypatch):
    import speciesid

    monkeypatch.setattr(speciesid, 'config', {})
    monkeypatch.setattr(speciesid, 'configure_worker_signals', lambda: None)
    monkeypatch.setattr(speciesid, 'preflight', lambda source: [])
    monkeypatch.setattr(health, '_detection_worker_enabled', None)
    read = MagicMock(return_value=connected())
    monitor = MagicMock()
    monkeypatch.setattr(speciesid, 'get_mqtt_runtime_status', read)
    monkeypatch.setattr(speciesid, 'start_health_monitor', monitor)
    monkeypatch.setattr(speciesid.app, 'run', MagicMock())
    monkeypatch.setitem(speciesid.app.extensions, 'mqtt_runtime_status', None)
    reader = object()
    speciesid.run_webui(reader)
    accessor = speciesid.app.extensions['mqtt_runtime_status']
    assert accessor() == connected()
    read.assert_called_once_with(reader)
    assert monitor.call_args.kwargs['mqtt_runtime_status'] is accessor


def test_config_failure_does_not_render_zero_as_known_archive_stats(flask_client, monkeypatch):
    import webui
    from routes import admin

    monkeypatch.setattr(webui, 'config', {'admin': {'auth_enabled': False}})
    failing = MagicMock(side_effect=ValueError('PRIVATE-EXCEPTION'))
    monkeypatch.setattr(webui, 'get_admin_stats', failing)
    monkeypatch.setattr(webui, 'get_recent_system_events', failing)
    monkeypatch.setattr(health, 'load_config', failing)
    monkeypatch.setattr(admin, 'load_runtime_config', failing)
    response = flask_client.get('/admin')
    assert response.status_code == 200
    assert b'Unavailable' in response.data
    assert b'PRIVATE' not in response.data
