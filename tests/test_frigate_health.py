"""Network-free observations with independently controlled local/source clocks."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import threading
from unittest.mock import Mock

import pytest

from app.frigate_client import FrigateDiscoveryError
from app.frigate_health import FrigateHealthCache, collect_http_observation, freshness_seconds


class Harness:
    def __init__(self):
        self.now = 0
        self.wall = 10000
        self.config = {'frigate': {'frigate_url': 'http://frigate', 'camera': ['Garden']}}
        self.inventory = [{'id': 'Garden', 'display_name': 'Garden birds',
                           'enabled': True, 'detection_enabled': True}]
        self.camera = {'camera_fps': 5, 'process_fps': 5, 'detection_fps': 0,
                       'detection_enabled': True}
        self.config_code = self.stats_code = 'success'
        self.online = True
        self.timestamp = None
        self.uptime = 1000
        self.runtime_present = True
        self.complete = True
        self.calls = 0
        self.cache = FrigateHealthCache(self.fetch, monotonic=lambda: self.now,
                                       wall_time=lambda: self.wall + self.now)

    def fetch(self, url):
        self.calls += 1
        return {'inventory': deepcopy(self.inventory) if self.config_code == 'success' else None,
                'stats': {'cameras': {'Garden': deepcopy(self.camera)} if self.runtime_present else {},
                          'service': {'last_updated': self.wall + self.now if self.timestamp is None else self.timestamp,
                                      'uptime': self.uptime + self.now if self.uptime is not None else None}}
                if self.stats_code == 'success' else None,
                'config_code': self.config_code, 'stats_code': self.stats_code,
                'online': self.online, 'inventory_complete': self.complete}

    def read(self, *, refresh=True, interval=60):
        return self.cache.get(self.config, refresh=refresh, interval=interval)

    def row(self, **kwargs):
        return self.read(**kwargs)['rows'][0]

    def advance(self, seconds=60):
        self.now += seconds
        return self.row()


@pytest.fixture
def h():
    return Harness()


def test_healthy_quiet_optional_pid_absence_and_copy(h):
    row = h.row()
    assert row['status'] == 'healthy'
    assert row['display_name'] == 'Garden birds'
    row['status'] = 'missing'
    assert h.row()['status'] == 'healthy'
    assert h.calls == 1


@pytest.mark.parametrize('value', [None, -1, float('nan'), float('inf'), '5', True])
@pytest.mark.parametrize('field', ['camera_fps', 'process_fps'])
def test_unusable_fps_is_unknown(h, field, value):
    from app.frigate_client import normalize_camera_stats
    h.camera[field] = value
    h.camera = normalize_camera_stats({'cameras': {'Garden': h.camera}})['cameras']['Garden']
    assert h.row()['status'] == 'unknown'


def test_conflicting_detection_evidence_is_unknown(h):
    h.camera['detection_enabled'] = False
    assert h.row()['reason_code'] == 'contradictory'


def test_source_timestamp_already_stale(h):
    h.timestamp = h.wall - 121
    assert h.row()['status'] == 'stale'


def test_service_startup_grace(h):
    h.camera['camera_fps'] = 0
    h.uptime = 0
    assert h.row()['reason_code'] == 'starting'
    assert h.advance()['reason_code'] == 'starting'
    assert h.advance()['reason_code'] == 'pending'
    assert h.advance()['status'] == 'unavailable'


@pytest.mark.parametrize('field,value,status', [
    ('enabled', False, 'disabled'), ('detection_enabled', False, 'detection_disabled'),
    ('enabled', None, 'unknown'), ('detection_enabled', None, 'healthy'),
])
def test_config_states_precede_zero_runtime(h, field, value, status):
    h.inventory[0][field] = value
    if status in ('disabled', 'detection_disabled'):
        h.camera.update(camera_fps=0, process_fps=0, detection_enabled=False)
    assert h.row()['status'] == status


def test_empty_authoritative_inventory_missing_and_case_sensitive_dedup(h):
    h.config['frigate']['camera'] = ['Garden', 'garden', 'Garden']
    result = h.read()
    assert [row['id'] for row in result['rows']] == ['Garden', 'garden']
    assert [row['status'] for row in result['rows']] == ['healthy', 'missing']
    assert h.config['frigate']['camera'] == ['Garden', 'garden', 'Garden']
    h.inventory = []
    h.now = 60
    assert h.row()['status'] == 'missing'


@pytest.mark.parametrize('kind', ['capture', 'processing'])
def test_grace_two_distinct_bad_samples_and_recovery(h, kind):
    h.camera['camera_fps' if kind == 'capture' else 'process_fps'] = 0
    assert h.row()['reason_code'] == 'starting'
    assert h.advance()['status'] == 'unknown'
    assert h.advance()['reason_code'] == 'pending'
    assert h.advance()['status'] == 'unavailable'
    assert h.read()['degraded'] is True
    h.camera.update(camera_fps=5, process_fps=5)
    assert h.advance()['status'] == 'healthy'
    assert h.read()['degraded'] is False


def test_same_timestamp_cannot_confirm_even_after_grace(h):
    h.row()
    h.now = 120
    h.timestamp = h.wall + 120
    h.camera['camera_fps'] = 0
    assert h.row()['reason_code'] == 'pending'
    assert h.advance()['status'] == 'unknown'
    assert h.advance()['status'] == 'unknown'
    assert h.advance()['status'] == 'stale'


@pytest.mark.parametrize('reset', ['restart', 'reenable', 'failure', 'stale_gap', 'connection'])
def test_pending_reset(h, reset):
    h.row()
    h.now = 120
    h.camera['camera_fps'] = 0
    assert h.row()['reason_code'] == 'pending'
    h.now = 180
    if reset == 'restart':
        h.uptime = -180  # source now reports zero uptime
    elif reset == 'reenable':
        h.inventory[0]['enabled'] = False
    elif reset == 'failure':
        h.stats_code = 'timeout'
    elif reset == 'stale_gap':
        h.now = 300
    else:
        h.config['frigate']['frigate_url'] = 'http://other'
    assert h.row()['status'] != 'unavailable'
    h.inventory[0]['enabled'] = True
    h.stats_code = 'success'
    h.uptime = 1000
    assert (h.advance()['status'] == 'unavailable') is (reset == 'stale_gap')


@pytest.mark.parametrize('config_code,stats_code,status', [
    ('success', 'timeout', 'unknown'), ('timeout', 'success', 'unknown'),
    ('auth_rejected', 'success', 'unknown'), ('success', 'auth_rejected', 'unknown'),
])
def test_endpoint_failures(h, config_code, stats_code, status):
    h.config_code, h.stats_code = config_code, stats_code
    result = h.read()
    assert result['rows'][0]['status'] == status
    assert result['access_warning'] is ('auth_rejected' in (config_code, stats_code))
    assert result['online'] is True


@pytest.mark.parametrize('state', ['missing', 'disabled', 'detection_disabled'])
def test_config_conclusions_survive_stats_failure(h, state):
    h.stats_code = 'timeout'
    if state == 'missing':
        h.inventory = []
    else:
        h.inventory[0]['enabled' if state == 'disabled' else 'detection_enabled'] = False
    assert h.row()['status'] == state


def test_outage_no_camera_failures_and_negative_cache(h):
    h.row()
    h.now = 60
    h.online = False
    h.config_code = h.stats_code = 'timeout'
    result = h.read()
    assert result['warnings'] == []
    assert result['rows'][0]['status'] == 'unknown'
    assert result['degraded'] is False
    assert 'cannot be reached' in result['service_message']
    h.now = 74
    h.read()
    assert h.calls == 2
    h.now = 75
    h.read()
    assert h.calls == 3
    h.now = 135
    assert h.row()['status'] == 'stale'


def test_missing_runtime_is_unknown_and_filtered_inventory_never_missing(h):
    h.runtime_present = False
    assert h.row()['status'] == 'unknown'
    h.now = 60
    h.complete = False
    h.inventory = []
    assert h.row()['status'] == 'unknown'


@pytest.mark.parametrize('timestamp', [0, -1, 10031, 1e100])
def test_implausible_timestamp(h, timestamp):
    h.timestamp = timestamp
    assert h.row()['status'] == 'unknown'


def test_expiry_cached_reads_and_custom_cadence(h):
    assert freshness_seconds(60) == 120
    assert freshness_seconds(180) == 390
    assert h.row(refresh=False)['status'] == 'unknown'
    assert h.calls == 0
    h.row()
    h.now = 121
    assert h.row(refresh=False)['status'] == 'stale'
    assert h.row(refresh=False, interval=180)['status'] == 'healthy'
    assert h.calls == 1
    h.row()
    assert h.calls == 2


def test_concurrent_refresh_coalesces_and_cached_reader_does_not_block(h):
    entered, release = threading.Event(), threading.Event()
    fetch = h.fetch
    def delayed(url):
        entered.set()
        assert release.wait(5)
        return fetch(url)
    h.cache._fetcher = delayed
    with ThreadPoolExecutor(max_workers=3) as pool:
        first = pool.submit(h.read)
        assert entered.wait(5)
        second = pool.submit(h.read)
        assert h.row(refresh=False)['status'] == 'unknown'
        release.set()
        assert first.result()['rows'][0]['status'] == 'healthy'
        assert second.result()['rows'][0]['status'] == 'healthy'
    assert h.calls == 1


def test_auth_failure_discards_cached_health_and_connection_change_invalidates(h):
    h.row()
    h.stats_code = 'auth_rejected'
    h.now = 60
    assert h.row()['status'] == 'unknown'
    assert h.cache._history == {}
    h.config['frigate']['frigate_url'] = 'http://PRIVATE:PRIVATE@other'
    assert h.row(refresh=False)['status'] == 'unknown'
    assert h.calls == 2


@pytest.mark.parametrize('config_ok,stats_ok', [(True, True), (True, False), (False, True), (False, False)])
def test_minimal_request_pattern(h, config_ok, stats_ok):
    client = Mock()
    client.get_camera_inventory.return_value = h.inventory
    client.get_camera_stats.return_value = h.fetch('')['stats']
    if not config_ok:
        client.get_camera_inventory.side_effect = FrigateDiscoveryError('timeout')
    if not stats_ok:
        client.get_camera_stats.side_effect = FrigateDiscoveryError('timeout')
    client.check_api_connection.return_value = 'success'
    assert collect_http_observation(client)['online'] is True
    client.get_camera_inventory.assert_called_once_with()
    client.get_camera_stats.assert_called_once_with()
    assert client.check_api_connection.call_count == (0 if config_ok or stats_ok else 1)
    client.get_version.assert_not_called()
