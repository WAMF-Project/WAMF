"""Isolated browser QA app: temporary saved config/secrets/DB, no live services."""

import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from dataclasses import replace
from types import SimpleNamespace

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def main():
    with TemporaryDirectory(prefix='wamf-browser-') as directory:
        root = Path(directory)
        config = {
            'config_version': 2,
            'mqtt': {'host': 'saved-broker', 'topic_prefix': 'frigate', 'authentication': {'enabled': True}},
            'frigate': {'frigate_url': 'http://fixture-frigate', 'camera': ['garden']},
            'classification': {'model': 'model.tflite', 'threshold': 0.7},
            'admin': {'auth_enabled': False},
            'storage': {'database_path': str(root / 'speciesid.db'), 'models_path': str(root / 'models')},
            'media': {'snapshots_path': str(root / 'snapshots'), 'clips_path': str(root / 'clips')},
        }
        path = root / 'config.yml'
        path.write_text(yaml.safe_dump(config))
        (root / 'secrets.yml').write_text(yaml.safe_dump({
            'secrets_version': 1,
            'mqtt': {'username': 'M2-SECRET-USER-SENTINEL', 'password': 'M2-SECRET-PASSWORD-SENTINEL'},
        }))
        os.environ['WHOSATMYFEEDER_CONFIG'] = str(path)
        import wamf_paths
        wamf_paths.REPO_ROOT = root  # Database artifacts must never use Bird-Lab storage.
        import webui
        webui.initialize_app()
        from flask import session, request, g
        from app import health
        from app.config_loader import load_runtime_config
        from app.mqtt_runtime import MqttRuntimeSnapshot
        from app.classifier_runtime import ClassifierRuntimeSnapshot
        from app.model_install import install_result
        from app.mqtt_connection import MESSAGES, MqttProbeResult
        from routes import admin
        admin.install_model = lambda *args, **kwargs: install_result('connection_error')

        # Fail closed if a test forgets interception; never contact a broker.
        admin.probe_mqtt_connection = lambda settings: MqttProbeResult(
            False, 'refused', MESSAGES['refused'], 0, '2026-10-18T12:00:00+00:00',
        )
        # Frigate diagnostics must also remain isolated if interception is missing.
        from app.frigate_connection import FrigateProbeResult, MESSAGES as FRIGATE_MESSAGES
        admin.probe_frigate_connection = lambda base_url: FrigateProbeResult(
            False, 'connection_error', FRIGATE_MESSAGES['connection_error'], 0,
            '2026-10-19T12:00:00+00:00',
        )
        from app.frigate_connection import DISCOVERY_MESSAGES, FrigateDiscoveryResult
        admin.discover_frigate_cameras = lambda base_url: FrigateDiscoveryResult(
            False, 'connection_error', DISCOVERY_MESSAGES['connection_error'], 0,
            '2026-10-19T12:00:00+00:00', None,
        )
        webui.app.secret_key = 'isolated-browser-session'
        original_auth_enabled = webui.is_admin_auth_enabled
        webui.is_admin_auth_enabled = lambda: (
            request.headers.get('X-WAMF-Test-Database-Auth') == 'enabled' or original_auth_enabled()
        )

        @webui.app.before_request
        def browser_session():
            session['admin_authenticated'] = True
            g.mqtt_fixture_state = request.headers.get('X-WAMF-Test-MQTT-State', 'connected')
        # The isolated session must exist before production authentication gates.
        hooks = webui.app.before_request_funcs[None]
        hooks.remove(browser_session)
        hooks.insert(0, browser_session)

        webui.app.view_functions['auth.login'] = lambda: '<h1>Log in</h1>'
        webui.get_retention_status = lambda: {
            'last_attempt_completed_at': '2026-10-18T12:00:00+00:00',
            'last_attempt_outcome': 'success',
            'last_success_completed_at': '2026-10-18T12:00:00+00:00',
        }
        snapshot = MqttRuntimeSnapshot(
            worker_generation='fixture', is_current=True,
            connection_state='connected', subscription_state='subscribed',
            worker_started_at='2026-10-18T11:00:00Z', last_connected_at='2026-10-18T11:01:00Z',
            last_event_message_at='2026-10-18T11:02:00Z', updated_at='2026-10-18T11:02:00Z',
            classifier=ClassifierRuntimeSnapshot(
                state='ready', path=str(Path(__file__).resolve().parents[2] / 'model.tflite'),
                sha256='95f59df4cff9053355abcd0b6e5e7e740f6eae19ed8cded32e2c3e8068195093',
            ),
        )
        def read_runtime():
            mode = g.mqtt_fixture_state
            changes = {
                'starting': dict(connection_state='starting', subscription_state='unknown'),
                'subscribing': dict(connection_state='subscribing', subscription_state='pending'),
                'reconnecting': dict(connection_state='reconnecting', subscription_state='unknown',
                                     last_disconnected_at='2026-10-18T11:03:00Z',
                                     last_disconnect_reason='unexpected_disconnect'),
                'disconnected': dict(connection_state='disconnected', subscription_state='unknown'),
                'failed': dict(connection_state='subscribing', subscription_state='failed'),
                'stale': dict(connection_state='stopped', subscription_state='unknown', is_current=False),
                'quiet': dict(last_event_message_at=None),
            }
            if mode == 'unknown':
                return MqttRuntimeSnapshot()
            return replace(snapshot, **changes.get(mode, {}))
        def health_config():
            saved = load_runtime_config()
            if g.mqtt_fixture_state == 'setup':
                saved['mqtt']['host'] = None
            return saved
        health.load_config = health_config
        from app.frigate_health import FrigateHealthCache

        def camera_health(config, *, refresh=False, interval=60):
            # Exercise production normalization/classification/presentation with
            # controlled HTTP observations and clocks; never use real services.
            from copy import deepcopy
            mode = request.headers.get('X-WAMF-Test-Frigate-State', 'healthy')
            clock = [0]
            sample = {'camera_fps': 5, 'process_fps': 5, 'detection_fps': 0,
                      'detection_enabled': True}
            inventory = [{'id': 'garden', 'display_name': 'Garden birds',
                          'enabled': True, 'detection_enabled': True}]
            monitored = ['garden']
            if mode == 'missing':
                inventory = []
            if mode in ('disabled', 'detection_disabled'):
                inventory[0]['enabled' if mode == 'disabled' else 'detection_enabled'] = False
                sample['detection_enabled'] = False
            if mode == 'mixed':
                monitored += ['missing_camera', 'disabled_camera', 'detection_disabled_camera']
                inventory += [
                    {'id': 'disabled_camera', 'display_name': 'Side garden', 'enabled': False, 'detection_enabled': False},
                    {'id': 'detection_disabled_camera', 'display_name': 'Feeder', 'enabled': True, 'detection_enabled': False},
                ]
            if mode == 'long':
                long_id = 'Garden_' + 'very_long_camera_identifier_' * 5
                monitored = [long_id]
                inventory[0].update(id=long_id, display_name='A friendly camera name ' * 8)
            cfg = deepcopy(config)
            cfg['frigate']['camera'] = monitored
            def observation(url):
                unavailable = mode in ('outage', 'access')
                code = 'connection_error' if mode == 'outage' else 'auth_rejected' if mode == 'access' else 'success'
                return {
                    'inventory': None if unavailable else inventory,
                    'stats': None if unavailable else {'cameras': {} if mode == 'unknown' else {monitored[0]: deepcopy(sample)},
                        'service': {'last_updated': 10000 + clock[0], 'uptime': 1000 + clock[0]}},
                    'config_code': code, 'stats_code': code, 'online': mode != 'outage',
                }
            cache = FrigateHealthCache(observation, monotonic=lambda: clock[0], wall_time=lambda: 10000 + clock[0])
            result = cache.get(cfg, refresh=True)
            if mode == 'unavailable':
                sample['camera_fps'] = 0
                clock[0] = 120
                cache.get(cfg, refresh=True)
                clock[0] = 180
                result = cache.get(cfg, refresh=True)
            if mode == 'stale':
                clock[0] = 121
                result = cache.get(cfg)
            return result

        health.frigate_health_cache = SimpleNamespace(get=camera_health)
        health.shutil.disk_usage = lambda path: (100, 10, 90)
        webui.app.extensions['mqtt_runtime_status'] = read_runtime
        webui.app.run(host='127.0.0.1', port=7768, use_reloader=False)


if __name__ == '__main__':
    main()
