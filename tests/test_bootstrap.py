import re
from copy import deepcopy
from pathlib import Path

import pytest
import yaml
from werkzeug.security import check_password_hash, generate_password_hash

from app import bootstrap, config_editor
from app.config_migration import (
    CURRENT_CONFIG_VERSION,
    ConfigMigrationValidationError,
    ConfigVersionError,
)
from app.config_persistence import (
    ConfigBackupError,
    ConfigLockTimeoutError,
    ConfigPersistenceTransaction,
    ConfigWriteError,
)


@pytest.fixture
def config_path(tmp_path, monkeypatch):
    path = tmp_path / 'config.yml'
    monkeypatch.setenv('WHOSATMYFEEDER_CONFIG', str(path))
    monkeypatch.delenv('WAMF_SECRET_KEY', raising=False)
    return path


def write_admin(path, **admin):
    path.write_text(yaml.safe_dump({
        'config_version': CURRENT_CONFIG_VERSION,
        'admin': {'auth_enabled': True, **admin},
        'api': {'token_hash': 'keep'},
    }))


def temporary_password(output):
    return re.search(r'temporary admin password: (\S+)', output).group(1)


def test_missing_default_config_is_copied(tmp_path, monkeypatch, capsys):
    root = tmp_path / 'app'
    (root / 'config').mkdir(parents=True)
    example = root / 'config/config.yml.example'
    example.write_text(
        f'config_version: {CURRENT_CONFIG_VERSION}\n'
        'admin:\n  auth_enabled: false\nwebui:\n  port: 7767\n'
    )
    path = root / 'config/config.yml'
    monkeypatch.delenv('WHOSATMYFEEDER_CONFIG', raising=False)
    monkeypatch.setattr(bootstrap, 'REPO_ROOT', root)
    monkeypatch.setattr(bootstrap, 'get_config_path', lambda: str(path))
    bootstrap.bootstrap_config()
    assert path.read_text() == example.read_text()
    assert path.stat().st_mode & 0o777 == 0o600
    assert config_editor.get_config_backup_paths(path) == []
    assert 'Review external settings' in capsys.readouterr().out


def test_missing_override_fails_without_creation(config_path):
    with pytest.raises(ValueError, match='WHOSATMYFEEDER_CONFIG file does not exist'):
        bootstrap.bootstrap_config()
    assert not config_path.exists()


@pytest.mark.parametrize('secret', [None, '', '  ', '<long random session secret>'])
@pytest.mark.parametrize('password_hash', [None, '', '<generated werkzeug password hash>', 'broken', 'invalid$salt$deadbeef', 'pbkdf2:sha256:1$salt$bad'])
def test_invalid_credentials_bootstrap_once(config_path, capsys, secret, password_hash):
    write_admin(config_path, session_secret=secret, password_hash=password_hash)
    config = bootstrap.bootstrap_config()
    output = capsys.readouterr().out
    password = temporary_password(output)
    admin = config['admin']
    assert len(admin['session_secret']) == 64
    assert check_password_hash(admin['password_hash'], password)
    assert password not in config_path.read_text()
    assert config['api'] == {'token_hash': 'keep'}
    persisted = config_path.read_bytes()
    assert bootstrap.bootstrap_config() == config
    assert config_path.read_bytes() == persisted
    assert capsys.readouterr().out == ''


def test_valid_credentials_preserved_byte_for_byte(config_path, capsys):
    password_hash = generate_password_hash('existing-password')
    write_admin(config_path, session_secret='existing-secret', password_hash=password_hash)
    original = config_path.read_bytes()
    bootstrap.bootstrap_config()
    assert config_path.read_bytes() == original
    assert capsys.readouterr().out == ''


def test_only_invalid_field_replaced(config_path, capsys):
    password_hash = generate_password_hash('existing-password')
    write_admin(config_path, session_secret='<placeholder>', password_hash=password_hash)
    assert bootstrap.bootstrap_config()['admin']['password_hash'] == password_hash
    assert capsys.readouterr().out == ''
    write_admin(config_path, session_secret='existing-secret', password_hash='bad')
    assert bootstrap.bootstrap_config()['admin']['session_secret'] == 'existing-secret'
    assert 'temporary admin password' in capsys.readouterr().out


def test_disabled_auth_does_not_generate(config_path, capsys):
    config_path.write_text(
        f'config_version: {CURRENT_CONFIG_VERSION}\nadmin:\n  auth_enabled: false\n'
    )
    original = config_path.read_bytes()
    bootstrap.bootstrap_config()
    assert original == config_path.read_bytes()
    assert capsys.readouterr().out == ''


def test_environment_secret_is_preserved(config_path, monkeypatch):
    write_admin(config_path, session_secret='<placeholder>', password_hash=generate_password_hash('valid'))
    monkeypatch.setenv('WAMF_SECRET_KEY', 'existing-environment-secret')
    original = config_path.read_bytes()
    bootstrap.bootstrap_config()
    assert config_path.read_bytes() == original


@pytest.mark.parametrize('content', [
    'admin:\n  auth_enabled: true\napi:\n  token_hash: keep\n',
    'admin: {auth_enabled: true}\napi: {token_hash: keep}\n',
])
def test_missing_admin_keys_are_inserted(config_path, content):
    config_path.write_text(content)
    config = bootstrap.bootstrap_config()
    assert config['admin']['auth_enabled'] is True
    assert bootstrap.valid_password_hash(config['admin']['password_hash'])
    assert config['api']['token_hash'] == 'keep'


def test_example_comments_and_unrelated_values_preserved(config_path):
    content = (bootstrap.REPO_ROOT / 'config/config.yml.example').read_text()
    config_path.write_text(content)
    config = bootstrap.bootstrap_config()
    expected = content.replace('<long random session secret>', config['admin']['session_secret']).replace('<generated werkzeug password hash>', config['admin']['password_hash'])
    assert config_path.read_text() == expected


def configured():
    return {
        'config_version': CURRENT_CONFIG_VERSION,
        'frigate': {
            'frigate_url': 'http://localhost:5000',
            'camera': ['test'],
        },
        'mqtt': {'host': 'localhost', 'topic_prefix': 'frigate'},
        'classification': {'model': 'model.tflite', 'threshold': 0.7},
    }


def legacy_configured():
    config = configured()
    config.pop('config_version')
    config.pop('mqtt')
    config['frigate'].update(mqtt_server='localhost', main_topic='frigate')
    return config


def test_preflight_accepts_configured_native_install():
    assert bootstrap.preflight(configured()) == []


def test_preflight_reports_example_placeholders():
    config = yaml.safe_load((bootstrap.REPO_ROOT / 'config/config.yml.example').read_text())
    issues = bootstrap.preflight(config)
    assert 'mqtt.host' in issues
    assert 'frigate.camera' in issues


def test_preflight_requires_mqtt_credentials_when_enabled():
    config = configured()
    config['mqtt']['authentication'] = {'enabled': True}
    issues = bootstrap.preflight(config)
    assert 'mqtt.authentication.username' in issues
    assert 'mqtt.authentication.password' in issues


def test_preflight_accepts_canonical_only_mqtt_without_mutating_config():
    config = configured()
    config['mqtt'] = {
        'host': 'canonical-broker',
        'port': 2883,
        'topic_prefix': 'kingfisher',
        'authentication': {
            'enabled': True,
            'username': 'canonical-user',
            'password': 'canonical-secret',
        },
    }
    original = deepcopy(config)

    assert bootstrap.preflight(config) == []
    assert config == original


def test_preflight_does_not_interpret_legacy_only_mqtt():
    issues = bootstrap.preflight(legacy_configured())

    assert 'mqtt.host' in issues
    assert 'mqtt.topic_prefix' in issues


def test_preflight_canonical_mqtt_wins_over_conflicting_legacy_values():
    config = configured()
    config['frigate'].update(
        mqtt_server='',
        mqtt_port=0,
        main_topic='',
        mqtt_auth=True,
        mqtt_username='',
        mqtt_password='',
    )
    config['mqtt'] = {
        'host': 'canonical-broker',
        'port': 2883,
        'topic_prefix': 'kingfisher',
        'authentication': {'enabled': False},
    }

    assert bootstrap.preflight(config) == []


def test_preflight_reports_missing_canonical_mqtt_without_exposing_secret():
    secret = 'SENTINEL-MQTT-PASSWORD'
    config = configured()
    config['mqtt'] = {
        'authentication': {'enabled': True, 'password': secret},
    }

    issues = bootstrap.preflight(config)

    assert 'mqtt.host' in issues
    assert 'mqtt.topic_prefix' in issues
    assert 'mqtt.authentication.username' in issues
    assert secret not in repr(issues)


def test_native_preflight_returns_setup_issues(config_path, capsys):
    config_path.write_text((bootstrap.REPO_ROOT / 'config/config.yml.example').read_text())
    assert 'mqtt.host' in bootstrap.prepare_native_startup()
    assert 'temporary admin password' in capsys.readouterr().out
    assert 'frigate.camera' in bootstrap.prepare_native_startup()
    assert capsys.readouterr().out == ''


def test_startup_migrates_legacy_config_with_exact_backup_and_is_idempotent(
    config_path,
    monkeypatch,
):
    legacy = legacy_configured()
    legacy['admin'] = {'auth_enabled': False}
    original = yaml.safe_dump(legacy, sort_keys=False)
    config_path.write_text(original)
    config_path.chmod(0o644)

    migrated = bootstrap.bootstrap_config()

    backups = config_editor.get_config_backup_paths(config_path)
    assert migrated['config_version'] == CURRENT_CONFIG_VERSION
    assert migrated['mqtt']['host'] == 'localhost'
    assert migrated['mqtt']['topic_prefix'] == 'frigate'
    assert 'mqtt_server' not in migrated['frigate']
    assert 'main_topic' not in migrated['frigate']
    assert len(backups) == 1
    assert Path(backups[0]).read_text() == original
    assert config_path.stat().st_mode & 0o777 == 0o600
    assert Path(backups[0]).stat().st_mode & 0o777 == 0o600

    active_after_first_start = config_path.read_bytes()
    backups_after_first_start = list(backups)
    writes = []

    class TrackingTransaction(ConfigPersistenceTransaction):
        def write(self, *args, **kwargs):
            writes.append((args, kwargs))
            return super().write(*args, **kwargs)

    monkeypatch.setattr(bootstrap, "ConfigPersistenceTransaction", TrackingTransaction)
    assert bootstrap.bootstrap_config() == migrated
    assert writes == []
    assert config_path.read_bytes() == active_after_first_start
    assert config_editor.get_config_backup_paths(config_path) == backups_after_first_start


def test_valid_but_incomplete_legacy_config_migrates(config_path):
    config_path.write_text('admin: {auth_enabled: false}\n')

    migrated = bootstrap.bootstrap_config()

    validation = bootstrap.validate_config(migrated)
    assert migrated['config_version'] == CURRENT_CONFIG_VERSION
    assert validation.is_valid is True
    assert validation.is_ready is False


def test_structurally_invalid_migration_is_not_activated(config_path):
    original = 'frigate: {mqtt_port: invalid}\n'
    config_path.write_text(original)

    with pytest.raises(ConfigMigrationValidationError, match='mqtt.port'):
        bootstrap.bootstrap_config()

    assert config_path.read_text() == original
    assert config_editor.get_config_backup_paths(config_path) == []


def test_future_version_is_not_rewritten_on_startup(config_path):
    original = f'config_version: {CURRENT_CONFIG_VERSION + 1}\n'
    config_path.write_text(original)

    with pytest.raises(ConfigVersionError, match='newer than'):
        bootstrap.bootstrap_config()

    assert config_path.read_text() == original
    assert config_editor.get_config_backup_paths(config_path) == []


def test_migration_backup_failure_leaves_legacy_active(config_path, monkeypatch):
    original = yaml.safe_dump(legacy_configured(), sort_keys=False)
    config_path.write_text(original)

    def fail_backup(_path):
        raise ConfigBackupError('controlled backup failure')

    monkeypatch.setattr('app.config_persistence._create_backup', fail_backup)
    with pytest.raises(ConfigBackupError):
        bootstrap.bootstrap_config()

    assert config_path.read_text() == original


def test_migration_replacement_failure_leaves_legacy_active(
    config_path,
    monkeypatch,
):
    original = yaml.safe_dump(legacy_configured(), sort_keys=False)
    config_path.write_text(original)

    def fail_replace(_source, _target):
        raise OSError('controlled replacement failure')

    monkeypatch.setattr('app.config_persistence.os.replace', fail_replace)
    with pytest.raises(ConfigWriteError):
        bootstrap.bootstrap_config()

    assert config_path.read_text() == original


def test_migration_lock_failure_leaves_legacy_active(config_path, monkeypatch):
    original = yaml.safe_dump(legacy_configured(), sort_keys=False)
    config_path.write_text(original)
    real_transaction = ConfigPersistenceTransaction

    with real_transaction(config_path):
        monkeypatch.setattr(
            bootstrap,
            'ConfigPersistenceTransaction',
            lambda path: real_transaction(path, lock_timeout=0),
        )
        with pytest.raises(ConfigLockTimeoutError):
            bootstrap.bootstrap_config()

    assert config_path.read_text() == original


def test_default_paths_are_independent_of_cwd(tmp_path, monkeypatch):
    from app.db import NAMES_DB_PATH
    monkeypatch.delenv('WHOSATMYFEEDER_CONFIG', raising=False)
    monkeypatch.chdir(tmp_path)
    assert Path(config_editor.get_config_path()) == bootstrap.REPO_ROOT / 'config/config.yml'
    assert Path(NAMES_DB_PATH) == bootstrap.REPO_ROOT / 'birdnames.db'


def test_relative_override_uses_callers_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('WHOSATMYFEEDER_CONFIG', 'custom.yml')
    assert config_editor.get_config_path() == str(tmp_path / 'custom.yml')


def test_native_example_port():
    config = yaml.safe_load((bootstrap.REPO_ROOT / 'config/config.yml.example').read_text())
    assert config['webui']['port'] == bootstrap.DEFAULT_PORT == 7767


def test_generated_password_can_login_and_be_changed(config_path, capsys, flask_client, monkeypatch):
    import webui
    monkeypatch.setenv('WHOSATMYFEEDER_CONFIG', str(config_path))
    monkeypatch.setitem(webui.app.config, 'SECRET_KEY', webui.app.secret_key)
    write_admin(config_path, session_secret='<placeholder>', password_hash='<placeholder>')
    bootstrap.bootstrap_config()
    password = temporary_password(capsys.readouterr().out)
    monkeypatch.setattr(webui, 'config', webui.config)
    webui.load_config()
    with flask_client.session_transaction() as session:
        session.clear()
    assert flask_client.post('/login', data={'password': password}).status_code == 302
    with flask_client.session_transaction() as session:
        assert session['admin_authenticated']
        csrf = session['csrf_token']
    response = flask_client.post('/admin/password', data={
        'current_password': password,
        'new_password': 'a-new-personal-password',
        'confirm_password': 'a-new-personal-password',
        'csrf_token': csrf,
    })
    assert response.status_code == 302
    config = bootstrap.bootstrap_config()
    assert check_password_hash(config['admin']['password_hash'], 'a-new-personal-password')
    assert not check_password_hash(config['admin']['password_hash'], password)
    assert capsys.readouterr().out == ''
    flask_client.get('/logout')
    assert flask_client.post('/login', data={'password': 'a-new-personal-password'}).status_code == 302


def test_no_password_disclosed_if_persistence_fails(config_path, monkeypatch, capsys):
    from app.config_persistence import ConfigWriteError

    write_admin(config_path, session_secret='<placeholder>', password_hash='<placeholder>')
    original = config_path.read_bytes()

    def fail_write(self, config, **kwargs):
        raise ConfigWriteError('controlled persistence failure')

    monkeypatch.setattr(bootstrap.ConfigPersistenceTransaction, 'write', fail_write)
    with pytest.raises(ConfigWriteError):
        bootstrap.bootstrap_config()
    assert capsys.readouterr().out == ''
    assert config_path.read_bytes() == original


@pytest.mark.parametrize('section,value', [('classification', 'invalid'), ('webui', 'invalid')])
def test_preflight_reports_malformed_sections(section, value):
    config = configured()
    config[section] = value
    with pytest.raises(ValueError, match=section):
        bootstrap.preflight(config)
