"""Network-free Frigate process contract and real deadline cleanup tests."""
import json
import subprocess
import sys
import time
from unittest.mock import MagicMock

import pytest

from app import frigate_connection as connection


@pytest.mark.parametrize("code", list(connection.MESSAGES))
def test_parent_contract_and_private_transport(monkeypatch, code, caplog):
    process = MagicMock(returncode=0)
    process.communicate.return_value = (code, None)
    process.poll.return_value = 0
    factory = MagicMock(return_value=process)
    monkeypatch.setattr(connection.subprocess, "Popen", factory)
    settings = "http://PRIVATE-USER:PRIVATE-PASSWORD@frigate"
    result = connection.probe_frigate_connection(settings)
    assert result.code == code
    assert result.success is (code == "success")
    assert result.safe_message == connection.MESSAGES[code]
    assert result.duration_ms >= 0
    assert result.completed_at.endswith("+00:00")
    assert "PRIVATE" not in repr(result) + caplog.text
    args, kwargs = factory.call_args
    assert "PRIVATE" not in repr(args) + repr(kwargs)
    assert kwargs["stderr"] is subprocess.DEVNULL
    payload = json.loads(process.communicate.call_args_list[0].args[0])
    assert payload["base_url"] == settings


@pytest.mark.parametrize("output,returncode", [("PRIVATE-PASSWORD", 0), ("success", 1)])
def test_invalid_child_output_is_never_returned(monkeypatch, output, returncode):
    process = MagicMock(returncode=returncode)
    process.communicate.return_value = (output, None)
    monkeypatch.setattr(connection.subprocess, "Popen", MagicMock(return_value=process))
    result = connection.probe_frigate_connection("http://frigate")
    assert result.code == "internal_error"
    assert "PRIVATE" not in repr(result)


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf"), True, "5"])
def test_invalid_deadline_never_launches_worker(monkeypatch, timeout):
    factory = MagicMock()
    monkeypatch.setattr(connection.subprocess, "Popen", factory)
    assert connection.probe_frigate_connection(
        "http://frigate", timeout_seconds=timeout
    ).code == "configuration_error"
    factory.assert_not_called()


def test_real_stalled_child_is_killed_and_reaped(monkeypatch):
    real_popen = subprocess.Popen
    children = []

    def launch(*args, **kwargs):
        # No broker, DNS or application startup: model blocking library work.
        process = real_popen([sys.executable, "-c", "import time; time.sleep(60)"], **kwargs)
        children.append(process)
        return process

    monkeypatch.setattr(connection.subprocess, "Popen", launch)
    started = time.monotonic()
    for _ in range(2):
        result = connection.probe_frigate_connection("http://frigate", timeout_seconds=0.1)
        assert result.code == "timeout"
    assert time.monotonic() - started < 3
    assert all(child.returncode is not None for child in children)
    assert all(child.stdin.closed and child.stdout.closed for child in children)


def test_real_worker_invalid_url_is_safe_without_network():
    result = connection.probe_frigate_connection(
        ""
    )
    assert result.code == "configuration_error"
    assert "PRIVATE" not in repr(result)


def test_probe_does_not_read_or_write_configuration(tmp_path, monkeypatch):
    config = tmp_path / "config.yml"
    secrets = tmp_path / "secrets.yml"
    config.write_text("deliberately invalid config")
    secrets.write_text("PRIVATE-PASSWORD")
    monkeypatch.setenv("WHOSATMYFEEDER_CONFIG", str(config))
    before = {path.name: path.read_bytes() for path in tmp_path.iterdir()}
    result = connection.probe_frigate_connection("")
    assert result.code == "configuration_error"
    assert before == {path.name: path.read_bytes() for path in tmp_path.iterdir()}


