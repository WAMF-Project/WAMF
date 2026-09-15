"""Docker packaging must retain the native bootstrap and storage contract."""
from pathlib import Path
import shlex
import subprocess

import pytest
import yaml

import wamf_paths

ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.parametrize('compose_name', ['docker-compose.yml', 'docker-compose.yml.example'])
@pytest.mark.parametrize('example_name', ['config.yml.example', 'config.docker.yml.example'])
def test_compose_persists_resolved_application_paths(compose_name, example_name, monkeypatch):
    service = yaml.safe_load((ROOT / compose_name).read_text())['services']['wamf']
    config = yaml.safe_load((ROOT / 'config' / example_name).read_text())
    monkeypatch.setattr(wamf_paths, 'REPO_ROOT', Path('/app'))
    monkeypatch.setattr(wamf_paths, '_load_config', lambda: config)
    mounts = dict(volume.split(':', 1) for volume in service['volumes'])
    assert mounts['./config'] == '/app/config'
    assert wamf_paths.get_database_path().parent == Path(mounts['./data'])
    assert wamf_paths.get_snapshots_path() == Path(mounts['./media']) / 'wamf/snapshots'
    assert wamf_paths.get_clips_path() == Path(mounts['./media']) / 'wamf/clips'
    assert service['ports'] == ['7767:7767']
    assert config['webui']['port'] == 7767
    assert config['webui']['host'] == '0.0.0.0'
    assert service['environment'] == {'TZ': 'Europe/London'}


def test_dockerfile_includes_runtime_and_bootstrap_resources():
    lines = (ROOT / 'Dockerfile').read_text().splitlines()
    copies = {}
    for line in lines:
        if line.startswith('COPY '):
            source, destination = shlex.split(line)[1:]
            copies[source] = destination
    for resource in ('model.tflite', 'birdnames.db', 'speciesid.py', 'webui.py',
                     'wamf_paths.py', 'version.py', 'retention.py', 'app/',
                     'routes/', 'templates/', 'static/', 'integrations/'):
        assert resource in copies, f'Missing runtime resource: {resource}'
        assert (ROOT / resource).exists()
    assert copies['config/config.yml.example'] == '/usr/local/share/wamf/config.yml.example'
    assert copies['scripts/docker-entrypoint.sh'] == '/usr/local/bin/wamf-entrypoint'
    assert 'FROM python:3.11-slim' in lines
    assert 'EXPOSE 7767' in lines
    assert 'CMD ["python", "./speciesid.py"]' in lines
    assert 'ENTRYPOINT ["sh", "/usr/local/bin/wamf-entrypoint"]' in lines


@pytest.mark.parametrize('existing', [False, True])
def test_entrypoint_seeds_empty_mount_and_preserves_existing_files(tmp_path, existing):
    config_dir = tmp_path / 'config'
    bundled_example = tmp_path / 'bundled-example.yml'
    bundled_example.write_text((ROOT / 'config/config.yml.example').read_text())
    if existing:
        config_dir.mkdir()
        (config_dir / 'config.yml.example').write_text('existing example\n')
        (config_dir / 'config.yml').write_text('existing private configuration\n')
        (config_dir / 'config.yml.backup.bak').write_text('existing backup\n')
    script = (ROOT / 'scripts/docker-entrypoint.sh').read_text()
    script = script.replace('/app/config', shlex.quote(str(config_dir)))
    script = script.replace('/usr/local/share/wamf/config.yml.example', shlex.quote(str(bundled_example)))
    # Exit code propagation also checks that the entrypoint delegates to CMD.
    result = subprocess.run(['sh', '-c', script, 'entrypoint', 'sh', '-c', 'exit 7'], capture_output=True, text=True)
    assert result.returncode == 7, result.stderr
    if existing:
        assert (config_dir / 'config.yml.example').read_text() == 'existing example\n'
        assert (config_dir / 'config.yml').read_text() == 'existing private configuration\n'
        assert (config_dir / 'config.yml.backup.bak').read_text() == 'existing backup\n'
    else:
        assert (config_dir / 'config.yml.example').read_text() == bundled_example.read_text()
        assert not (config_dir / 'config.yml').exists()  # Python owns config/credential creation.
