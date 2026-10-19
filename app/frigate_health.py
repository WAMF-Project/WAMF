"""Read-only Frigate observations. No timers, persistence, or import-time I/O."""

from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import threading
import time

from app.frigate_client import FrigateDiscoveryError, normalize_camera_stats

CACHE_SECONDS = 60
FAILURE_CACHE_SECONDS = 15
GRACE_SECONDS = 120
CONFIRM_SECONDS = 60
IMPAIRED = frozenset({'missing', 'disabled', 'detection_disabled', 'unavailable'})
LABELS = {
    'healthy': 'Healthy', 'missing': 'Missing', 'disabled': 'Disabled',
    'detection_disabled': 'Detection disabled', 'unavailable': 'Unavailable',
    'unknown': 'Unknown', 'stale': 'Stale',
}
REASONS = {
    'healthy': 'Capture and processing are reporting frames.',
    'missing': 'Not found in the current Frigate camera inventory.',
    'disabled': 'Camera is disabled in Frigate.',
    'detection_disabled': 'Detection is disabled in Frigate.',
    'capture_unavailable': 'Frigate repeatedly reports no captured frames.',
    'processing_unavailable': 'Frigate repeatedly reports no processed frames.',
    'pending': 'Waiting for another runtime observation.',
    'starting': 'Waiting for camera startup observations.',
    'incomplete': 'Camera health evidence is incomplete.',
    'contradictory': 'Camera configuration and runtime evidence disagree.',
    'timestamp': 'Runtime observation time could not be verified.',
    'stale': 'Previous camera health evidence has expired.',
    'service': 'Camera health unavailable while Frigate cannot be reached.',
    'access': 'Camera health access is unavailable.',
}


def freshness_seconds(interval=60):
    return max(120, 2 * interval + 30) if interval > 60 else 120


def normalize_observation(value):
    """Validate the private worker result too; no arbitrary child data escapes."""
    from app.frigate_connection import MESSAGES

    config_code, stats_code = value['config_code'], value['stats_code']
    if config_code not in MESSAGES or stats_code not in MESSAGES or type(value['online']) is not bool:
        raise ValueError('Invalid observation')
    inventory = value.get('inventory') if config_code == 'success' else None
    if config_code == 'success':
        if not isinstance(inventory, list) or not all(
            isinstance(item, dict) and set(item) == {'id', 'display_name', 'enabled', 'detection_enabled'}
            and isinstance(item['id'], str) and bool(item['id']) and item['id'].isprintable()
            and isinstance(item['display_name'], str) and item['display_name'].isprintable()
            and (len(item['display_name']) <= 256 or item['display_name'] == item['id'])
            and all(item[key] is None or type(item[key]) is bool for key in ('enabled', 'detection_enabled'))
            for item in inventory
        ):
            raise ValueError('Invalid inventory')
    stats = normalize_camera_stats(value['stats']) if stats_code == 'success' else None
    return {'inventory': inventory, 'stats': stats, 'config_code': config_code,
            'stats_code': stats_code, 'online': value['online'],
            'inventory_complete': value.get('inventory_complete', True) is True}


def collect_http_observation(client):
    """Called only inside the owned, deadline-bounded diagnostic interpreter."""
    result = {'inventory': None, 'stats': None, 'config_code': 'internal_error',
              'stats_code': 'internal_error', 'online': None}
    for key, code_key, operation in (
        ('inventory', 'config_code', client.get_camera_inventory),
        ('stats', 'stats_code', client.get_camera_stats),
    ):
        try:
            result[key] = operation()
            result[code_key] = 'success'
        except FrigateDiscoveryError as exc:
            result[code_key] = exc.code
    # A validated protected response already establishes API reachability.
    if 'success' in (result['config_code'], result['stats_code']):
        result['online'] = True
    else:
        result['online'] = client.check_api_connection() == 'success'
    return result


def fetch_observation(base_url):
    from app.frigate_connection import _run_worker

    code, observation, _, _ = _run_worker(base_url, 10, observe=True)
    if code == 'success':
        return observation
    return {'inventory': None, 'stats': None, 'config_code': code,
            'stats_code': code, 'online': False}


def _iso(value):
    try:
        return datetime.fromtimestamp(value, timezone.utc).isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return None


class FrigateHealthCache:
    """One connection generation, shared by monitor and explicit health views.

    Refreshers coalesce; cache-only readers never wait for network I/O. Only
    normalized data crosses the worker boundary. Returned values are copies.
    """

    def __init__(self, fetcher=None, *, monotonic=None, wall_time=None):
        self._fetcher = fetcher or fetch_observation
        self._monotonic = monotonic or time.monotonic
        self._wall_time = wall_time or time.time
        self._condition = threading.Condition()
        self._generation = None
        self._refreshing = False
        self._observation = None
        self._received = None
        self._received_wall = None
        self._expires = 0
        self._seen = {}
        self._pending = {}
        self._startup = None
        self._last_timestamp = None
        self._history = {}

    def get(self, config, *, refresh=False, interval=60):
        frigate = (config or {}).get('frigate', {})
        url = frigate.get('frigate_url', '')
        # The URL is the complete current Frigate connection contract, including
        # any embedded authentication. Keep only its digest in retained state.
        generation = hashlib.sha256(url.encode()).digest()
        ids = list(dict.fromkeys(frigate.get('camera', [])))
        with self._condition:
            if generation != self._generation:
                self._generation = generation
                self._observation = self._received = self._received_wall = None
                self._expires = 0
                self._seen.clear()
                self._pending.clear()
                self._startup = self._last_timestamp = None
                self._history.clear()
            while refresh and self._refreshing:
                self._condition.wait()
                if generation != self._generation:
                    return FrigateHealthCache().get(config)
            if not refresh or self._monotonic() < self._expires:
                return self._present(ids, interval)
            self._refreshing = True
        try:
            observation = self._fetcher(url)
        except Exception:
            # Never log exceptions from potentially credential-bearing inputs.
            observation = {'inventory': None, 'stats': None, 'config_code': 'internal_error',
                           'stats_code': 'internal_error', 'online': False}
        with self._condition:
            self._refreshing = False
            if generation == self._generation:
                if self._received is not None:
                    gap = self._monotonic() - self._received
                    previous = ((self._observation or {}).get('stats') or {}).get('service', {}).get('last_updated')
                    source_age = self._received_wall - previous if previous is not None else 0
                    if gap + max(0, source_age) > freshness_seconds(interval):
                        self._pending.clear()
                self._observation = observation
                self._received = self._monotonic()
                self._received_wall = self._wall_time()
                success = observation['config_code'] == observation['stats_code'] == 'success'
                self._expires = self._received + (CACHE_SECONDS if success else FAILURE_CACHE_SECONDS)
                if 'auth_rejected' in (observation['config_code'], observation['stats_code']):
                    self._history.clear()
                    self._seen.clear()
                    self._pending.clear()
                else:
                    for source, code in (('inventory', 'config_code'), ('stats', 'stats_code')):
                        if observation[code] == 'success':
                            self._history[source] = self._received
                self._reset_for_restart()
                result = self._present(ids, interval, update=True)
            else:
                result = FrigateHealthCache().get(config)
            self._condition.notify_all()
            return result

    def _reset_for_restart(self):
        stats = self._observation.get('stats') or {}
        service = stats.get('service', {})
        timestamp, uptime = service.get('last_updated'), service.get('uptime')
        if timestamp is None:
            return
        startup = timestamp - uptime if uptime is not None else None
        if ((self._last_timestamp is not None and timestamp < self._last_timestamp)
                or (startup is not None and self._startup is not None
                    and abs(startup - self._startup) > 30)):
            self._pending.clear()
            self._seen.clear()
        self._last_timestamp = timestamp
        if startup is not None:
            self._startup = startup

    def _present(self, ids, interval, *, update=False):
        now = self._monotonic()
        limit = freshness_seconds(interval)
        obs = self._observation or {}
        age = now - self._received if self._received is not None else None
        expired = age is not None and age > limit
        online = obs.get('online') if not expired else None
        access = 'auth_rejected' in (obs.get('config_code'), obs.get('stats_code'))
        inventory = obs.get('inventory')
        config_valid = (obs.get('config_code') == 'success' and isinstance(inventory, list)
                        and obs.get('inventory_complete', True) is True)
        cameras = {item['id']: item for item in inventory} if config_valid else {}
        stats = obs.get('stats') or {}
        service = stats.get('service', {})
        timestamp, uptime = service.get('last_updated'), service.get('uptime')
        source_age = self._received_wall - timestamp if (
            timestamp is not None and self._received_wall is not None) else None
        timestamp_valid = (source_age is not None and timestamp > 0 and source_age >= -30
                           and _iso(timestamp) is not None)
        runtime_stale = timestamp_valid and source_age + (age or 0) > limit
        runtime_valid = (obs.get('stats_code') == 'success' and timestamp_valid
                         and not runtime_stale and not expired)
        if expired or runtime_stale or access or online is False:
            self._pending.clear()
        rows = []
        # Stop retaining removed camera debounce/grace records.
        self._seen = {key: value for key, value in self._seen.items() if key in ids}
        self._pending = {key: value for key, value in self._pending.items() if key in ids}
        for camera_id in ids:
            camera = cameras.get(camera_id)
            runtime = stats.get('cameras', {}).get(camera_id)
            row = {
                'id': camera_id, 'display_name': camera['display_name'] if camera else camera_id,
                'inventory_present': camera_id in cameras if config_valid else None,
                'configured_enabled': camera.get('enabled') if camera else None,
                'detection_enabled': camera.get('detection_enabled') if camera else None,
                'runtime_present': camera_id in stats.get('cameras', {}) if runtime_valid else None,
                'capture_fps': runtime.get('camera_fps') if runtime and runtime_valid else None,
                'process_fps': runtime.get('process_fps') if runtime and runtime_valid else None,
                'inventory_observed_at': _iso(self._received_wall) if config_valid else None,
                'runtime_observed_at': _iso(timestamp) if timestamp_valid else None,
            }
            status, reason = 'unknown', 'incomplete'
            if access:
                reason = 'access'
            elif expired:
                status, reason = 'stale', 'stale'
            elif online is False:
                reason = 'service'
                if self._history and any(now - observed > limit for observed in self._history.values()):
                    status = 'stale'
            elif config_valid:
                if camera is None:
                    status = reason = 'missing'
                elif camera['enabled'] is False:
                    status = reason = 'disabled'
                elif camera['enabled'] is True:
                    detect = camera['detection_enabled']
                    runtime_detect = runtime.get('detection_enabled') if runtime and runtime_valid else None
                    if detect is not None and runtime_detect is not None and detect != runtime_detect:
                        reason = 'contradictory'
                    elif (runtime_detect if runtime_detect is not None else detect) is False:
                        row['detection_enabled'] = False
                        status = reason = 'detection_disabled'
                    elif runtime_stale:
                        status = reason = 'stale'
                    elif not timestamp_valid and obs.get('stats_code') == 'success':
                        reason = 'timestamp'
                    elif runtime_valid and runtime:
                        row['detection_enabled'] = runtime_detect if runtime_detect is not None else detect
                        capture, process = row['capture_fps'], row['process_fps']
                        if capture is not None and process is not None and row['detection_enabled'] is True:
                            if capture > 0 and process > 0:
                                status = reason = 'healthy'
                            else:
                                reason = 'capture_unavailable' if capture == 0 else 'processing_unavailable'
                                started = self._seen.setdefault(camera_id, now)
                                if now - started < GRACE_SECONDS or (uptime is not None and uptime < GRACE_SECONDS):
                                    reason = 'starting'
                                else:
                                    pending = self._pending.get(camera_id)
                                    if update:
                                        if pending is None or pending['reason'] != reason:
                                            pending = {'reason': reason, 'first': now, 'timestamp': timestamp,
                                                       'last_timestamp': timestamp, 'confirmed': False}
                                            self._pending[camera_id] = pending
                                        elif timestamp > pending['last_timestamp']:
                                            pending['last_timestamp'] = timestamp
                                            if now - pending['first'] >= CONFIRM_SECONDS and timestamp - pending['timestamp'] >= CONFIRM_SECONDS:
                                                pending['confirmed'] = True
                                    if pending and pending['reason'] == reason and pending['confirmed']:
                                        status = 'unavailable'
                                    else:
                                        reason = 'pending'
            if status == 'unknown' and reason == 'incomplete':
                needed_source = 'stats' if config_valid else 'inventory'
                observed = self._history.get(needed_source)
                if observed is not None and now - observed > limit:
                    status = reason = 'stale'
            if status == 'healthy':
                self._seen.setdefault(camera_id, now)
            if update and reason not in ('pending', 'capture_unavailable', 'processing_unavailable'):
                self._pending.pop(camera_id, None)
            if status in ('disabled', 'missing'):
                self._seen.pop(camera_id, None)
            row.update(status=status, reason_code=reason, safe_reason=REASONS[reason],
                       label=LABELS[status], tone='warning' if status in IMPAIRED else
                       'success' if status == 'healthy' else 'neutral')
            rows.append(row)
        counts = Counter(row['status'] for row in rows)
        summary = ' · '.join(f'{counts[state]} {LABELS[state].lower()}' for state in LABELS if counts[state])
        warnings = [row for row in rows if row['status'] in IMPAIRED]
        return deepcopy({
            'rows': rows, 'warnings': warnings, 'summary': summary,
            'online': online, 'access_warning': access,
            'degraded': bool(warnings) and online is not False or access,
            'coverage_complete': bool(rows) and not any(row['status'] in ('unknown', 'stale') for row in rows),
            'service_message': REASONS['service'] if online is False else REASONS['access'] if access else None,
        })


frigate_health_cache = FrigateHealthCache()
