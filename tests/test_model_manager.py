"""Classification Settings, loaded identity and explicit selection; all offline."""

from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import pytest
import yaml

from app.classifier_runtime import ClassifierRuntimeSnapshot, loaded_classifier
from app.config_forms import build_settings_candidate
from app.config_persistence import ConfigWriteError
from app.model_catalogue import load_catalogue
from app.model_install import InstallSnapshot, install_result
from app.model_manager import build_model_manager
from app.model_registry import artifact_path, resolve_classifier_model
from app.mqtt_runtime import MqttRuntimeSnapshot, _RuntimeStore

ROOT = Path(__file__).resolve().parent.parent
BASE = '/admin/config/models'


@pytest.fixture
def context(flask_client, monkeypatch, tmp_path):
    import webui
    from app import model_manager
    from routes import admin

    catalogue = load_catalogue()
    # A real valid bundled model in an isolated managed slot, no live artifact.
    additional = replace(catalogue.default_artifact, version='aiy-v1', bundled_path=None,
                         filename='fixture.tflite', download_url=catalogue.get_artifact('bird', 'aiy-v1').download_url)
    model = replace(catalogue.models[0], artifacts=(catalogue.default_artifact, additional))
    catalogue = replace(catalogue, models=(model,))
    monkeypatch.setattr(model_manager, 'load_catalogue', lambda: catalogue)
    config = {
        'config_version': 2, 'classification': {'model': 'model.tflite', 'threshold': 0.81},
        'mqtt': {'host': 'fixture-broker'}, 'frigate': {'frigate_url': 'http://fixture', 'camera': ['garden']},
        'storage': {'models_path': str(tmp_path / 'models')},
        'unrelated': {'concurrent_value': 42}, 'admin': {'auth_enabled': False},
    }
    path = tmp_path / 'config.yml'
    path.write_text(yaml.safe_dump(config))
    (tmp_path / 'secrets.yml').write_text('secrets_version: 1\n')
    monkeypatch.setenv('WHOSATMYFEEDER_CONFIG', str(path))
    monkeypatch.setattr(webui, 'config', {'admin': {'auth_enabled': False}})
    reload = Mock()
    monkeypatch.setattr(webui, 'load_config', reload)
    monkeypatch.setattr(admin, 'install_model', Mock(return_value=install_result('installed')))
    observation = loaded_classifier(resolve_classifier_model(config))
    runtime = MqttRuntimeSnapshot(is_current=True, classifier=observation)
    monkeypatch.setitem(webui.app.extensions, 'mqtt_runtime_status', lambda: runtime)
    return flask_client, path, catalogue, config, additional, runtime, reload


def install_fixture(context):
    _, _, _, config, artifact, _, _ = context
    path = artifact_path(artifact, config)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes((ROOT / 'model.tflite').read_bytes())
    return path


def test_catalogue_legacy_active_and_uninstalled_state(context):
    client, path, _, _, _, _, _ = context
    before = path.read_bytes()
    state = client.get(BASE).json
    bundled, available = state['models']
    assert bundled['active'] and bundled['selected'] and bundled['bundled'] and bundled['valid']
    assert available['can_install'] and not available['can_select']
    assert not state['restart_required']
    assert state['custom']  # Recognition for display does not migrate legacy config.
    assert path.read_bytes() == before
    assert client.get(BASE).headers['Cache-Control'] == 'no-store'


def test_explicit_selection_preserves_config_and_running_model(context):
    client, path, _, config, _, runtime, reload = context
    install_fixture(context)
    # Simulate a newer Settings write after the page was initially rendered.
    config['unrelated']['concurrent_value'] = 77
    path.write_text(yaml.safe_dump(config))
    response = client.post(BASE + '/bird/aiy-v1/select')
    assert response.status_code == 200 and response.json['success']
    saved = yaml.safe_load(path.read_text())
    assert saved['classification'] == {'model_id': 'bird', 'model_version': 'aiy-v1', 'threshold': 0.81}
    assert saved['unrelated']['concurrent_value'] == 77
    state = response.json['manager']
    assert state['restart_required']
    assert state['models'][0]['active'] and not state['models'][0]['selected']
    assert state['models'][1]['selected'] and not state['models'][1]['active']
    assert runtime.classifier.model_id is None
    reload.assert_called_once()


@pytest.mark.parametrize('kind', ['missing', 'invalid', 'unknown_id', 'unknown_version'])
def test_selection_rejects_unsupported_or_unusable_without_writing(context, kind):
    client, path, _, _, _, _, reload = context
    target = '/bird/aiy-v1/select'
    if kind == 'invalid':
        install_fixture(context).write_bytes(b'corrupt')
    elif kind == 'unknown_id':
        target = '/unknown/aiy-v1/select'
    elif kind == 'unknown_version':
        target = '/bird/unknown/select'
    before = path.read_bytes()
    response = client.post(BASE + target)
    assert response.status_code == 409 and not response.json['success']
    assert path.read_bytes() == before
    reload.assert_not_called()


def test_selection_persistence_failure_preserves_previous_config(context, monkeypatch):
    client, path, _, _, _, _, reload = context
    install_fixture(context)
    before = path.read_bytes()
    monkeypatch.setattr('app.config_persistence._write_temporary_file', Mock(side_effect=ConfigWriteError('PRIVATE')))
    response = client.post(BASE + '/bird/aiy-v1/select')
    assert response.status_code == 500 and response.json['code'] == 'save_failed'
    assert 'PRIVATE' not in response.get_data(as_text=True)
    assert path.read_bytes() == before
    reload.assert_not_called()


def test_selection_uses_install_lock_and_refuses_overlap(context):
    from app.model_install import model_install_guard
    from wamf_paths import get_models_path
    client, path, _, config, _, _, _ = context
    install_fixture(context)
    before = path.read_bytes()
    with model_install_guard(get_models_path(config)):
        response = client.post(BASE + '/bird/aiy-v1/select')
    assert response.status_code == 409 and response.json['code'] == 'install_in_progress'
    assert path.read_bytes() == before


def test_selected_missing_artifact_remains_selected_with_problem(context):
    _, _, catalogue, config, _, runtime, _ = context
    config['classification'] = {'model_id': 'bird', 'model_version': 'aiy-v1', 'threshold': 0.81}
    state = build_model_manager(config, runtime, catalogue=catalogue)
    assert state['models'][1]['selected'] and not state['models'][1]['valid']
    assert state['models'][0]['active']
    assert state['selected_error'] and state['restart_required']


def test_managed_running_identity_clears_restart_requirement(context):
    _, _, catalogue, config, _, _, _ = context
    install_fixture(context)
    config['classification'] = {'model_id': 'bird', 'model_version': 'aiy-v1', 'threshold': 0.81}
    runtime = MqttRuntimeSnapshot(is_current=True, classifier=loaded_classifier(resolve_classifier_model(config, catalogue)))
    state = build_model_manager(config, runtime, catalogue=catalogue)
    assert state['models'][1]['active'] and state['models'][1]['selected']
    assert not state['restart_required']
    stale = build_model_manager(config, replace(runtime, is_current=False), catalogue=catalogue)
    assert not stale['running_known'] and not any(model['active'] for model in stale['models'])


@pytest.mark.parametrize('selected_version,running_version,current', [
    ('aiy-v1', 'bundled-1', True),
    ('aiy-v1', 'aiy-v1', True),
    ('bundled-1', 'aiy-v1', True),
    ('aiy-v1', None, False),
    ('aiy-v1', 'aiy-v1', False),
])
def test_pending_selection_requires_difference_from_authoritative_running_identity(
    context, selected_version, running_version, current,
):
    _, _, catalogue, config, _, _, _ = context
    install_fixture(context)
    config['classification'] = {'model_id': 'bird', 'model_version': selected_version, 'threshold': 0.81}
    runtime = None
    if running_version is not None:
        running_config = {**config, 'classification': {'model_id': 'bird', 'model_version': running_version}}
        runtime = MqttRuntimeSnapshot(is_current=current, classifier=loaded_classifier(
            resolve_classifier_model(running_config, catalogue)))
    state = build_model_manager(config, runtime, catalogue=catalogue)
    known_running = running_version if current else None
    assert state['running_known'] == (known_running is not None)
    assert state['restart_required'] == (known_running is not None and selected_version != known_running)
    for model in state['models']:
        assert model['selected'] == (model['version'] == selected_version)
        assert model['active'] == (model['version'] == known_running)
        assert model['selected_for_next_start'] == (
            model['version'] == selected_version and selected_version != known_running
        )
    if known_running is None:
        assert 'Unknown' in state['running_label']


def test_custom_path_is_not_silently_converted(context):
    _, path, catalogue, config, _, _, _ = context
    config['classification']['model'] = 'custom.tflite'
    state = build_model_manager(config, catalogue=catalogue)
    assert state['selected_label'] == 'Custom model · custom.tflite'
    assert state['custom'] and not any(model['selected'] for model in state['models'])
    assert yaml.safe_load(path.read_text())['classification']['model'] == 'model.tflite'


@pytest.mark.parametrize('action', ['install', 'select'])
def test_mutations_require_admin_session_and_csrf(context, monkeypatch, action):
    import webui
    client, _, _, _, _, _, _ = context
    monkeypatch.setattr(webui, 'config', {'admin': {'auth_enabled': True}})
    url = BASE + '/bird/aiy-v1/' + action
    with client.session_transaction() as session:
        session.clear()
    assert client.post(url).status_code == 302
    assert client.get(BASE + '/install-status').status_code == 302
    with client.session_transaction() as session:
        session['admin_authenticated'] = True
        session['csrf_token'] = 'test-token'
    assert client.post(url).status_code == 400
    assert client.post(url, headers={'X-CSRFToken': 'wrong'}).status_code == 400
    install_fixture(context)
    assert client.post(url, headers={'X-CSRFToken': 'test-token'}).status_code == 200


@pytest.mark.parametrize('code', ['installed', 'install_in_progress', 'checksum_mismatch', 'validation_failed', 'connection_error'])
def test_install_route_delegates_catalogue_identity_and_safe_outcomes(context, monkeypatch, code):
    from routes import admin
    client, path, _, _, _, _, _ = context
    before = path.read_bytes()
    service = Mock(return_value=install_result(code))
    monkeypatch.setattr(admin, 'install_model', service)
    response = client.post(BASE + '/bird/aiy-v1/install', json={'url': 'PRIVATE', 'path': '/private'})
    service.assert_called_once_with('bird', 'aiy-v1')
    assert response.json['code'] == code
    assert response.status_code == (409 if code == 'install_in_progress' else 200)
    assert 'PRIVATE' not in response.get_data(as_text=True)
    assert before == path.read_bytes()


def test_install_status_reports_progress_or_unknown(context, monkeypatch):
    from routes import admin
    client = context[0]
    monkeypatch.setattr(admin, 'get_install_status', lambda: None)
    assert client.get(BASE + '/install-status').json == {'operation': None}
    progress = InstallSnapshot('bird', 'aiy-v1', 'downloading', 50, 100, 'start', 'update')
    monkeypatch.setattr(admin, 'get_install_status', lambda: progress)
    response = client.get(BASE + '/install-status')
    assert response.json['operation']['percent'] == 50
    assert response.headers['Cache-Control'] == 'no-store'


def test_unchanged_path_from_stale_settings_form_preserves_managed_selection():
    config = {'classification': {'model_id': 'bird', 'model_version': 'aiy-v1', 'threshold': 0.7}}
    result = build_settings_candidate(config, {'classification_model': 'model.tflite',
        'classification_model_original': 'model.tflite', 'classification_threshold': '0.91'})
    assert result['classification'] == {**config['classification'], 'threshold': 0.91}
    edited = build_settings_candidate(config, {'classification_model': 'model.tflite',
        'classification_model_original': ''})
    assert edited['classification'] == {'model': 'model.tflite', 'threshold': 0.7}


def test_settings_rebuilds_partial_update_from_current_locked_config(context, monkeypatch):
    import webui
    from app.config_editor import write_config_preserving_admin

    client, path, _, config, _, _, _ = context
    install_fixture(context)
    def intervening_selection(*args, **kwargs):
        changed = deepcopy(config)
        changed['classification'] = {'model_id': 'bird', 'model_version': 'aiy-v1', 'threshold': 0.81}
        changed['unrelated']['concurrent_value'] = 123
        path.write_text(yaml.safe_dump(changed))
        return write_config_preserving_admin(*args, **kwargs)
    monkeypatch.setattr(webui, 'write_config_preserving_admin', intervening_selection)
    response = client.post('/admin/config/save', data={
        'classification_threshold': '0.91', 'classification_model': 'model.tflite',
        'classification_model_original': 'model.tflite',
    })
    assert response.status_code == 302
    saved = yaml.safe_load(path.read_text())
    assert saved['classification'] == {'model_id': 'bird', 'model_version': 'aiy-v1', 'threshold': 0.91}
    assert saved['unrelated']['concurrent_value'] == 123


@pytest.mark.parametrize('managed', [False, True])
def test_worker_reports_only_successfully_loaded_identity(context, monkeypatch, managed):
    import speciesid
    _, _, catalogue, config, _, _, _ = context
    install_fixture(context)
    config = deepcopy(config)
    if managed:
        config['classification'] = {'model_id': 'bird', 'model_version': 'aiy-v1'}
    monkeypatch.setattr(speciesid, 'config', config)
    monkeypatch.setattr(speciesid, 'resolve_classifier_model', lambda *a, **kw: resolve_classifier_model(config, catalogue))
    initialize = Mock()
    monkeypatch.setattr(speciesid, 'initialize_classifier', initialize)
    writer = Mock()
    speciesid.initialize_worker_classifier(writer)
    observed = writer.record_classifier.call_args.args[0]
    assert observed.state == 'ready' and observed.loaded_at and observed.sha256
    assert observed.model_id == ('bird' if managed else None)
    assert observed.model_version == ('aiy-v1' if managed else None)
    initialize.side_effect = ValueError('PRIVATE')
    with pytest.raises(ValueError):
        speciesid.initialize_worker_classifier(writer)
    assert writer.record_classifier.call_args.args[0] == ClassifierRuntimeSnapshot(state='failed')


def test_generation_changes_clear_loaded_identity_and_reject_late_writer():
    store = _RuntimeStore()
    old = store.begin()
    ready = ClassifierRuntimeSnapshot(state='ready', model_id='bird', model_version='bundled-1')
    store.record_classifier(old, ready)
    assert store.read().classifier == ready
    store.stop(old)
    assert not store.read().is_current and store.read().classifier.state == 'stopped'
    current = store.begin()
    store.record_classifier(old, ready)
    assert store.read().classifier.state == 'starting'
    store.record_classifier(current, ready)
    assert store.read().classifier == ready
