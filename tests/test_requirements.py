"""Keep Python runtime/dev installation paths on the canonical manifests."""
from pathlib import Path
import re

import yaml

ROOT = Path(__file__).resolve().parent.parent


def _entries(name):
    return [line.strip() for line in (ROOT / name).read_text().splitlines()
            if line.strip() and not line.lstrip().startswith('#')]


def _pinned_names(entries):
    names = []
    for entry in entries:
        match = re.fullmatch(r'([A-Za-z0-9_.-]+)==([0-9][A-Za-z0-9_.+]*)', entry)
        assert match, f'Expected an exact package pin: {entry}'
        names.append(re.sub(r'[-_.]+', '-', match[1]).lower())
    assert len(names) == len(set(names)), 'Duplicate normalized package name'
    return set(names)


def test_canonical_manifests_separate_pinned_runtime_and_dev_dependencies():
    runtime = _pinned_names(_entries('requirements.txt'))
    dev_entries = _entries('requirements-dev.txt')
    assert dev_entries[0] == '-r requirements.txt'
    dev = _pinned_names(dev_entries[1:])
    assert {'pytest', 'pytest-cov'} <= dev
    assert not runtime & dev
    assert not runtime & {'pytest', 'pytest-cov', 'coverage', 'ruff', 'playwright'}
    assert 'playwright' not in dev
    assert {'flask', 'werkzeug', 'numpy', 'opencv-python', 'ai-edge-litert',
            'paho-mqtt', 'pillow', 'pyyaml', 'requests', 'apscheduler', 'tzdata'} <= runtime
    assert not (ROOT / 'requirements').exists()
    assert {p.name for p in ROOT.glob('requirements*.txt')} == {
        'requirements.txt', 'requirements-dev.txt',
    }


def test_docker_and_ci_install_canonical_manifests():
    dockerfile = (ROOT / 'Dockerfile').read_text()
    assert 'COPY requirements.txt ./requirements.txt' in dockerfile
    assert 'pip install --no-cache-dir -r requirements.txt' in dockerfile
    assert 'requirements-dev.txt' not in dockerfile
    workflow = yaml.safe_load((ROOT / '.github/workflows/test.yml').read_text())
    steps = workflow['jobs']['test']['steps']
    assert any('pip install -r requirements-dev.txt' in step.get('run', '')
               for step in steps)
    cache = next(step['with']['cache-dependency-path'] for step in steps
                 if step.get('with', {}).get('cache') == 'pip')
    assert set(cache.splitlines()) == {'requirements.txt', 'requirements-dev.txt'}


def test_supported_install_paths_do_not_reference_removed_manifests():
    paths = [ROOT / name for name in ('Dockerfile', 'README.md', 'Makefile')]
    for directory in ('.github/workflows', 'docs', 'scripts'):
        paths.extend(path for path in (ROOT / directory).rglob('*')
                     if path.is_file() and path.suffix in {'.md', '.yml', '.yaml', '.sh'})
    for path in paths:
        assert 'requirements/' not in path.read_text(), str(path.relative_to(ROOT))
