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
            'storage': {'database_path': str(root / 'speciesid.db')},
            'media': {'snapshots_path': str(root / 'snapshots'), 'clips_path': str(root / 'clips')},
        }
        path = root / 'config.yml'
        path.write_text(yaml.safe_dump(config))
        (root / 'secrets.yml').write_text(yaml.safe_dump({
            'secrets_version': 1,
            'mqtt': {'username': 'M2-SECRET-USER-SENTINEL', 'password': 'M2-SECRET-PASSWORD-SENTINEL'},
        }))
        os.environ['WHOSATMYFEEDER_CONFIG'] = str(path)
        import webui
        from flask import session, request, g
        from app import health
        from app.config_loader import load_runtime_config
        from app.mqtt_runtime import MqttRuntimeSnapshot
        from app.mqtt_connection import MESSAGES, MqttProbeResult
        from routes import admin

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

        @webui.app.before_request
        def browser_session():
            session['admin_authenticated'] = True
            g.mqtt_fixture_state = request.headers.get('X-WAMF-Test-MQTT-State', 'connected')

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
        health.FrigateClient = lambda url: SimpleNamespace(get_version=lambda: 'fixture')
        health.shutil.disk_usage = lambda path: (100, 10, 90)
        webui.app.extensions['mqtt_runtime_status'] = read_runtime
        webui.app.run(host='127.0.0.1', port=7768, use_reloader=False)


if __name__ == '__main__':
    main()
