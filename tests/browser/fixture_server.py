"""Isolated browser QA app: temporary saved config/secrets/DB, no live services."""

import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def main():
    with TemporaryDirectory(prefix='wamf-browser-') as directory:
        root = Path(directory)
        config = {
            'config_version': 2,
            'mqtt': {'host': 'saved-broker', 'authentication': {'enabled': True}},
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
        from flask import session
        from app.mqtt_connection import MESSAGES, MqttProbeResult
        from routes import admin

        # Fail closed if a test forgets interception; never contact a broker.
        admin.probe_mqtt_connection = lambda settings: MqttProbeResult(
            False, 'refused', MESSAGES['refused'], 0, '2026-10-18T12:00:00+00:00',
        )
        webui.app.secret_key = 'isolated-browser-session'

        @webui.app.before_request
        def browser_session():
            session['admin_authenticated'] = True

        webui.app.view_functions['auth.login'] = lambda: '<h1>Log in</h1>'
        webui.get_retention_status = lambda: {
            'last_attempt_completed_at': '2026-10-18T12:00:00+00:00',
            'last_attempt_outcome': 'success',
            'last_success_completed_at': '2026-10-18T12:00:00+00:00',
        }
        webui.get_system_health = lambda: {
            'setup_required': True, 'overall_state': 'setup_required',
            'configuration_issues': [], 'restart_required': False,
            'database_healthy': True, 'archive_writable': True, 'disk_used_percent': 10,
        }
        webui.app.run(host='127.0.0.1', port=7768, use_reloader=False)


if __name__ == '__main__':
    main()
