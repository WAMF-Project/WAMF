"""Installer coordinator, IPC, deadlines and process ownership, all offline."""

from copy import deepcopy
from dataclasses import replace
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
from unittest.mock import Mock

import pytest

from app import model_install as install
from app.config_migration import ConfigVersionError
from app.model_catalogue import load_catalogue
from app.model_registry import discover_local_models


ROOT = Path(__file__).resolve().parent.parent
URL = "https://github.com/WAMF-Project/WAMF/releases/download/models-v1/fixture.tflite"


@pytest.fixture
def context(tmp_path):
    catalogue = load_catalogue()
    artifact = replace(catalogue.default_artifact, version="fixture", bundled_path=None, download_url=URL)
    catalogue = replace(catalogue, models=(replace(catalogue.models[0], artifacts=(artifact,)),), default_artifact=artifact)
    config = {"storage": {"models_path": str(tmp_path / "models")},
              "classification": {"model": "model.tflite", "threshold": 0.7}}
    return catalogue, config


def scripted_worker(monkeypatch, source):
    real_popen = subprocess.Popen
    children = []
    def launch(args, **kwargs):
        child = real_popen([sys.executable, "-c", source], **kwargs)
        children.append(child)
        return child
    monkeypatch.setattr(install.subprocess, "Popen", launch)
    return children


def test_real_bundled_install_is_available_without_http(tmp_path):
    service = install.ModelInstaller()
    config = {"storage": {"models_path": str(tmp_path / "models")}}
    result = service.install_model("bird", "bundled-1", config=config)
    assert result.code == "already_available_bundled" and result.success
    assert service.snapshot().terminal_result == result
    assert not list((tmp_path / "models").rglob("*.tflite"))


@pytest.mark.parametrize("model_id,version,code", [
    ("absent", "1", "unknown_model"), ("bird", "absent", "unknown_version"),
    ("../bird", "fixture", "unknown_model"),
])
def test_unknown_identity_does_not_launch_worker(context, monkeypatch, model_id, version, code):
    factory = Mock(side_effect=AssertionError("No worker"))
    monkeypatch.setattr(install.subprocess, "Popen", factory)
    result = install.ModelInstaller().install_model(model_id, version, catalogue=context[0], config=context[1])
    assert result.code == code
    factory.assert_not_called()


@pytest.mark.parametrize("url", [None, "http://github.com/file", "https://other.example/file"])
def test_download_source_required_before_launch(context, monkeypatch, url):
    catalogue, config = context
    artifact = replace(catalogue.default_artifact, download_url=url)
    catalogue = replace(catalogue, models=(replace(catalogue.models[0], artifacts=(artifact,)),))
    factory = Mock()
    monkeypatch.setattr(install.subprocess, "Popen", factory)
    result = install.ModelInstaller().install_model("bird", "fixture", catalogue=catalogue, config=config)
    assert result.code in {"invalid_catalogue", "invalid_catalogue_source"}
    factory.assert_not_called()
    assert not Path(config["storage"]["models_path"]).exists()


def test_hard_cap_checked_before_launch(context, monkeypatch):
    catalogue, config = context
    monkeypatch.setattr(install, "MAX_ARTIFACT_BYTES", 8)
    factory = Mock()
    monkeypatch.setattr(install.subprocess, "Popen", factory)
    assert install.ModelInstaller().install_model("bird", "fixture", catalogue=catalogue, config=config).code == "oversized"
    factory.assert_not_called()


def test_coordinator_progress_and_safe_result_protocol(context, monkeypatch):
    source = '''
import json, sys
payload = json.load(sys.stdin)
total = payload['artifact']['size_bytes']
for phase, received in [('downloading', 0), ('downloading', total // 2), ('verifying', total), ('validating', total), ('installing', total)]:
    print(json.dumps({'phase': phase, 'bytes_received': received}), flush=True)
print(json.dumps({'result': 'installed', 'message': 'PRIVATE-SIGNED-URL', 'url': 'PRIVATE'}), flush=True)
'''
    children = scripted_worker(monkeypatch, source)
    class RecordingInstaller(install.ModelInstaller):
        def __init__(self):
            super().__init__()
            self.recorded = []
        def _publish(self, **kwargs):
            super()._publish(**kwargs)
            self.recorded.append(self.snapshot())
    service = RecordingInstaller()
    result = service.install_model("bird", "fixture", catalogue=context[0], config=context[1])
    assert result.code == "installed"
    assert [item.phase for item in service.recorded] == ["downloading", "downloading", "verifying", "validating", "installing", "complete"]
    assert 49 < service.recorded[1].percent <= 50
    assert service.snapshot().percent == 100
    assert service.snapshot().terminal_result == result
    assert "PRIVATE" not in repr(service.snapshot())
    assert all(child.returncode == 0 and child.stdin.closed and child.stdout.closed for child in children)


@pytest.mark.parametrize("source", [
    "print('not JSON')", "print('{}')", "print('[]')", "print('x' * 10000)",
    "print('{\"result\": \"unknown-private-code\"}')",
    "print('{\"phase\": \"downloading\", \"bytes_received\": -1}')",
    "print('{\"result\": \"installed\"}'); print('{\"result\": \"installed\"}')",
    "print('{\"result\": \"installed\"}'); raise SystemExit(1)",
])
def test_malformed_worker_output_is_safe(context, monkeypatch, source):
    children = scripted_worker(monkeypatch, source)
    service = install.ModelInstaller()
    result = service.install_model("bird", "fixture", catalogue=context[0], config=context[1])
    assert result.code == "internal_error"
    assert service.snapshot().phase == "failed"
    assert all(child.returncode is not None for child in children)


def test_overall_timeout_kills_reaps_and_releases_lock(context, monkeypatch):
    children = scripted_worker(monkeypatch, "import time; time.sleep(60)")
    service = install.ModelInstaller()
    started = time.monotonic()
    for _ in range(2):
        result = service.install_model("bird", "fixture", catalogue=context[0], config=context[1], timeout_seconds=0.1)
        assert result.code == "timeout"
    assert time.monotonic() - started < 3
    assert all(child.returncode is not None and child.stdin.closed and child.stdout.closed for child in children)
    with install.model_install_guard(Path(context[1]["storage"]["models_path"])):
        pass


def test_thread_and_process_lock_duplicates_leave_first_snapshot_untouched(context, monkeypatch):
    service = install.ModelInstaller()
    entered, release = threading.Event(), threading.Event()
    def wait(*args):
        entered.set()
        assert release.wait(5)
        return install.install_result("installed")
    monkeypatch.setattr(service, "_run_worker", wait)
    results = []
    thread = threading.Thread(target=lambda: results.append(service.install_model(
        "bird", "fixture", catalogue=context[0], config=context[1])))
    thread.start()
    try:
        assert entered.wait(5)
        snapshot = service.snapshot()
        assert service.install_model("bird", "fixture", catalogue=context[0], config=context[1]).code == "install_in_progress"
        assert service.snapshot() == snapshot
        other = install.ModelInstaller()
        assert other.install_model("bird", "fixture", catalogue=context[0], config=context[1]).code == "install_in_progress"
    finally:
        release.set()
        thread.join(5)
    assert results[0].success and not thread.is_alive()


@pytest.mark.parametrize("timeout", [0, -1, True, float("nan"), float("inf")])
def test_invalid_timeout_is_a_programmer_error(timeout):
    with pytest.raises(ValueError):
        install.ModelInstaller().install_model("bird", "bundled-1", timeout_seconds=timeout)


@pytest.mark.parametrize("name", ["../escape.tflite", "/escape.tflite"])
def test_unsafe_catalogue_filename_rejected(context, name):
    catalogue, config = context
    artifact = replace(catalogue.default_artifact, filename=name)
    catalogue = replace(catalogue, models=(replace(catalogue.models[0], artifacts=(artifact,)),))
    result = install.ModelInstaller().install_model("bird", "fixture", catalogue=catalogue, config=config)
    assert result.code == "invalid_catalogue"


def test_worker_end_to_end_install_preserves_config_runtime_and_old_versions(context, monkeypatch, tmp_path, flask_client):
    # The real worker, download loop and M1 validation run. Only its HTTP session
    # is replaced in this isolated child; model bytes come from the local bundle.
    source = f'''
import sys
sys.path.insert(0, {str(ROOT)!r})
from pathlib import Path
from app import model_download
class Response:
    status_code = 200
    headers = {{}}
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def iter_content(self, chunk_size):
        with Path({str(ROOT / 'model.tflite')!r}).open('rb') as body:
            while chunk := body.read(chunk_size): yield chunk
class Session:
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def get(self, *args, **kwargs): return Response()
model_download.requests.Session = Session
from app.model_install_worker import main
main()
'''
    real_popen = subprocess.Popen
    children = []
    def launch(args, **kwargs):
        child = real_popen([sys.executable, "-c", source, str(os.getpid())], **kwargs)
        children.append(child)
        return child
    monkeypatch.setattr(install.subprocess, "Popen", launch)
    config = context[1]
    before = deepcopy(config)
    config_file = tmp_path / "config.yml"
    config_file.write_text("classification:\n  model: model.tflite\n")
    original = config_file.read_bytes()
    import speciesid
    monkeypatch.setattr("app.config_editor.write_config_preserving_admin", Mock(side_effect=AssertionError("No save")))
    monkeypatch.setattr("app.process_control.schedule_restart", Mock(side_effect=AssertionError("No restart")))
    sentinel = object()
    monkeypatch.setattr(speciesid, "classifier", sentinel)
    service = install.ModelInstaller()
    result = service.install_model("bird", "fixture", catalogue=context[0], config=config)
    assert result.code == "installed"
    assert service.snapshot().bytes_received == context[0].default_artifact.size_bytes
    assert config == before and config_file.read_bytes() == original
    assert speciesid.classifier is sentinel
    installed, = discover_local_models(context[0], config)
    assert installed.installed
    assert all(child.returncode == 0 for child in children)
    # An exact repeat uses the validator but never needs the HTTP fixture.
    assert service.install_model("bird", "fixture", catalogue=context[0], config=config).code == "already_installed"


def test_configured_selection_is_protected_even_with_known_empty_runtime(context, monkeypatch):
    config = context[1]
    path = Path(config["storage"]["models_path"]) / "bird/fixture/model.tflite"
    config["classification"] = {"model": str(path)}
    service = install.ModelInstaller()
    def inspected(payload, *args):
        assert str(path) in payload["protected_paths"]
        return install.install_result("repair_blocked")
    monkeypatch.setattr(service, "_run_worker", inspected)
    assert service.install_model("bird", "fixture", catalogue=context[0], config=config, running_paths=()).code == "repair_blocked"


def test_lock_symlink_is_rejected_without_touching_target(context, tmp_path):
    root = Path(context[1]["storage"]["models_path"])
    root.mkdir()
    target = tmp_path / "unrelated"
    target.write_bytes(b"keep")
    (root / ".install.lock").symlink_to(target)
    result = install.ModelInstaller().install_model("bird", "fixture", catalogue=context[0], config=context[1])
    assert result.code == "unsafe_path" and target.read_bytes() == b"keep"


def test_lock_permission_failure_is_safe(context, monkeypatch):
    from contextlib import contextmanager
    @contextmanager
    def denied(root):
        raise PermissionError("PRIVATE path")
        yield
    monkeypatch.setattr(install, "model_install_guard", denied)
    service = install.ModelInstaller()
    result = service.install_model("bird", "fixture", catalogue=context[0], config=context[1])
    assert result.code == "permission_error" and "PRIVATE" not in repr(service.snapshot())


def test_large_control_payload_cannot_block_parent_before_deadline(context, monkeypatch):
    catalogue, config = context
    artifact = replace(catalogue.default_artifact, notes="x" * 5000)
    catalogue = replace(catalogue, models=(replace(catalogue.models[0], artifacts=(artifact,)),))
    factory = Mock()
    monkeypatch.setattr(install.subprocess, "Popen", factory)
    result = install.ModelInstaller().install_model("bird", "fixture", catalogue=catalogue, config=config)
    assert result.code == "invalid_catalogue"
    factory.assert_not_called()


@pytest.mark.parametrize("error", [ValueError("PRIVATE config"), ConfigVersionError("PRIVATE version")])
def test_canonical_config_load_failure_is_sanitized(context, monkeypatch, error):
    monkeypatch.setattr(install, "load_runtime_config", Mock(side_effect=error))
    service = install.ModelInstaller()
    result = service.install_model("bird", "fixture", catalogue=context[0])
    assert result.code == "configuration_error"
    assert "PRIVATE" not in repr(service.snapshot())


def test_process_safe_lock_excludes_a_separate_interpreter(context):
    root = Path(context[1]["storage"]["models_path"])
    source = f'''
from pathlib import Path
from app.model_install import model_install_guard, InstallError
try:
    with model_install_guard(Path({str(root)!r})):
        print('unexpected-writer')
except InstallError as exc:
    print(exc.code)
'''
    with install.model_install_guard(root):
        child = subprocess.run([sys.executable, "-c", source], capture_output=True, text=True, timeout=5, cwd=ROOT)
    assert child.returncode == 0 and child.stdout.strip() == "install_in_progress"


def test_parent_death_kills_installer_and_releases_inherited_lock(tmp_path):
    pid_path = tmp_path / "worker.pid"
    model_root = tmp_path / "models"
    parent_source = f'''
import os, subprocess, sys
sys.path.insert(0, {str(ROOT)!r})
from dataclasses import replace
from app.model_catalogue import load_catalogue
from app import model_install
c = load_catalogue()
a = replace(c.default_artifact, version='fixture', bundled_path=None, download_url={URL!r})
c = replace(c, models=(replace(c.models[0], artifacts=(a,)),), default_artifact=a)
real_popen = subprocess.Popen
worker_source = {f'''import os,sys,time
sys.path.insert(0, {str(ROOT)!r})
from pathlib import Path
from app import model_download
class Session:
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def get(self, *args, **kwargs):
        Path({str(pid_path)!r}).write_text(str(os.getpid()))
        time.sleep(60)
model_download.requests.Session = Session
from app.model_install_worker import main
main()
'''!r}
def launch(args, **kwargs):
    return real_popen([sys.executable, '-c', worker_source, str(os.getpid())], **kwargs)
model_install.subprocess.Popen = launch
model_install.ModelInstaller().install_model('bird', 'fixture', catalogue=c, config={{'storage': {{'models_path': {str(model_root)!r}}}}})
'''
    parent = subprocess.Popen([sys.executable, "-c", parent_source], stdout=subprocess.DEVNULL,
                              stderr=subprocess.PIPE, start_new_session=True)
    worker_pid = None
    try:
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline and not pid_path.exists() and parent.poll() is None:
            time.sleep(0.02)
        assert pid_path.exists(), parent.stderr.read().decode() if parent.poll() is not None else "worker did not start"
        worker_pid = int(pid_path.read_text())
        parent.kill()
        parent.wait(timeout=3)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            info = Path(f"/proc/{worker_pid}/stat")
            if not info.exists() or info.read_text().split()[2] == "Z":
                break
            time.sleep(0.02)
        else:
            pytest.fail("Installer outlived its parent")
        with install.model_install_guard(model_root):
            pass
        assert not list(model_root.rglob("model.tflite"))
    finally:
        if parent.poll() is None:
            parent.kill()
        parent.wait()
        parent.stderr.close()
        if worker_pid:
            try:
                os.kill(worker_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
