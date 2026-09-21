"""Parent-owned shutdown, including real isolated process groups and workers."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from unittest.mock import MagicMock, patch

import pytest
import yaml

from app.process_control import WorkerSupervisor


@pytest.mark.parametrize('signum', [signal.SIGINT, signal.SIGTERM])
def test_supervisor_stops_all_children_before_joining_and_restores_handlers(signum):
    calls = []
    children = [MagicMock(pid=101), MagicMock(pid=102)]
    for index, child in enumerate(children):
        child.terminate.side_effect = lambda index=index: calls.append(('terminate', index))
        child.join.side_effect = lambda index=index: calls.append(('join', index))
    previous = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    with WorkerSupervisor(MagicMock(side_effect=children)) as workers:
        workers.start(lambda: None)
        workers.start(lambda: None)
        signal.getsignal(signum)(signum, None)
        assert workers.stopping
        assert workers.start(lambda: None) is None
    assert calls == [('terminate', 0), ('terminate', 1), ('join', 0), ('join', 1)]
    assert all(signal.getsignal(sig) == handler for sig, handler in previous.items())


def test_startup_failure_cleans_up_already_started_children():
    running = MagicMock(pid=101)
    failed = MagicMock(pid=None)
    failed.start.side_effect = RuntimeError('failed to start')
    with pytest.raises(RuntimeError, match='failed to start'):
        with WorkerSupervisor(MagicMock(side_effect=[running, failed])) as workers:
            workers.start(lambda: None)
            workers.start(lambda: None)
    running.terminate.assert_called_once()
    running.join.assert_called_once()
    failed.terminate.assert_not_called()
    failed.join.assert_not_called()


def test_signal_during_start_keeps_child_tracked():
    child = MagicMock(pid=101)
    factory = MagicMock(return_value=child)
    with WorkerSupervisor(factory) as workers:
        child.start.side_effect = lambda: workers._request_shutdown(signal.SIGINT, None)
        workers.start(lambda: None)
        assert workers.start(lambda: None) is None
    factory.assert_called_once()
    child.terminate.assert_called_once()
    child.join.assert_called_once()


def test_inherited_handler_does_not_swallow_termination_before_worker_initializes():
    workers = WorkerSupervisor(MagicMock())
    with patch('app.process_control.os.getpid', return_value=workers.owner_pid + 1), patch('app.process_control.os._exit') as exit_child:
        workers._request_shutdown(signal.SIGINT, None)
        exit_child.assert_not_called()
        workers._request_shutdown(signal.SIGTERM, None)
        exit_child.assert_called_once_with(0)
        assert not workers.stopping


@pytest.mark.parametrize('signum', [signal.SIGINT, signal.SIGTERM])
def test_watchdog_does_not_respawn_when_shutdown_requested_during_wait(monkeypatch, signum):
    import speciesid
    monkeypatch.setattr(speciesid, 'config', {
        'frigate': {'frigate_url': 'http://frigate', 'mqtt_server': 'mqtt',
                    'main_topic': 'frigate', 'camera': ['birdcam']},
        'classification': {'model': 'model.tflite', 'threshold': 0.7},
    })
    flask = MagicMock(pid=101)
    mqtt = MagicMock(pid=102)
    mqtt.is_alive.return_value = False
    def request_shutdown(timeout=None):
        if timeout is not None:
            signal.getsignal(signum)(signum, None)
    mqtt.join.side_effect = request_shutdown
    with patch('speciesid.load_config'), patch('speciesid.setupdb'), patch('speciesid.log_system_event'), patch('speciesid.multiprocessing.Process', side_effect=[flask, mqtt]) as factory:
        speciesid.main()
    assert factory.call_count == 2
    flask.terminate.assert_called_once()
    flask.join.assert_called_once()
    # The already-exited MQTT child is also reaped during cleanup.
    assert mqtt.join.call_args.kwargs == {}


# Execute the actual native entry point and actual worker entry functions. Only
# ML inference, external services, and Flask's socket-serving loop are replaced.
# Signal handlers, multiprocessing, watchdog, and process reaping are real.
RUNNER = r'''
import json
import os
from pathlib import Path
import runpy
import signal
import sys
import time
from unittest.mock import MagicMock

for name in ('PIL', 'PIL.Image', 'PIL.ImageOps'):
    sys.modules[name] = MagicMock()

def worker(role):
    assert signal.getsignal(signal.SIGINT) == signal.SIG_IGN
    assert signal.getsignal(signal.SIGTERM) == signal.SIG_DFL
    record = json.dumps({'role': role, 'pid': os.getpid(), 'ppid': os.getppid()}) + '\n'
    fd = os.open(os.environ['WORKER_PIDS'], os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    os.write(fd, record.encode())
    os.close(fd)
    if role == 'mqtt' and os.environ.get('CRASH_ONCE'):
        try:
            fd = os.open(os.environ['CRASH_ONCE'], os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(fd)
            return
    while True:
        time.sleep(1)

from flask import Flask
Flask.run = lambda self, **kwargs: worker('flask')
import paho.mqtt.client as mqtt
class Client:
    def __init__(self, *args, **kwargs):
        pass
    def connect(self, *args):
        pass
    def loop_forever(self):
        worker('mqtt')
mqtt.Client = Client
from app import health
health.start_health_monitor = lambda: None
runpy.run_path('speciesid.py', run_name='__main__')
'''


def _records(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()]


def _wait_for_workers(process, path, count):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        records = _records(path)
        if len(records) >= count:
            return records
        if process.poll() is not None:
            _, stderr = process.communicate()
            pytest.fail(f'Parent exited before workers were ready: {stderr}')
        time.sleep(0.02)
    pytest.fail('Workers did not become ready')


def _run_signal_test(tmp_path, signum, setup, group_signal, crash_once=False):
    config = {
        'admin': {'auth_enabled': False},
        'frigate': {'frigate_url': 'http://frigate',
                    'mqtt_server': 'your-mqtt-server' if setup else 'mqtt',
                    'main_topic': 'frigate', 'camera': ['birdcam']},
        'classification': {'model': 'model.tflite', 'threshold': 0.7},
        'storage': {'database_path': str(tmp_path / 'data/speciesid.db')},
        'media': {'snapshots_path': str(tmp_path / 'snapshots'),
                  'clips_path': str(tmp_path / 'clips')},
    }
    config_path = tmp_path / 'config.yml'
    config_path.write_text(yaml.safe_dump(config))
    pid_path = tmp_path / 'workers.jsonl'
    env = dict(os.environ, WHOSATMYFEEDER_CONFIG=str(config_path), WORKER_PIDS=str(pid_path))
    if crash_once:
        env['CRASH_ONCE'] = str(tmp_path / 'crashed')
    root = Path(__file__).resolve().parent.parent
    process = subprocess.Popen([sys.executable, '-c', RUNNER], cwd=root, env=env,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, start_new_session=True)
    try:
        expected = 1 if setup else (3 if crash_once else 2)
        records = _wait_for_workers(process, pid_path, expected)
        assert all(record['ppid'] == process.pid for record in records)
        assert [record['role'] for record in records].count('flask') == 1
        if group_signal:
            os.killpg(process.pid, signum)
        else:
            process.send_signal(signum)
        _, stderr = process.communicate(timeout=10)
        assert process.returncode == 0, stderr
        assert 'all child processes joined' in stderr
        assert 'KeyboardInterrupt' not in stderr
        assert len(_records(pid_path)) == expected, 'Watchdog respawned during shutdown'
        for record in records:
            assert not Path(f"/proc/{record['pid']}").exists(), 'Child survived or was not reaped'
        assert ('MQTT subprocess exited unexpectedly; restarting' in stderr) is crash_once
    finally:
        # Isolated session: this cannot signal the user's WAMF or pytest itself.
        # Teardown also prevents a broken regression from leaving orphan workers.
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=3)


@pytest.mark.parametrize('signum', [signal.SIGINT, signal.SIGTERM])
@pytest.mark.parametrize('setup', [False, True], ids=['configured', 'setup'])
@pytest.mark.parametrize('group_signal', [False, True], ids=['parent-only', 'process-group'])
def test_native_process_tree_shutdown(tmp_path, signum, setup, group_signal):
    _run_signal_test(tmp_path, signum, setup, group_signal)


def test_watchdog_recovers_worker_then_shutdown_reaps_replacement(tmp_path):
    _run_signal_test(tmp_path, signal.SIGTERM, False, False, crash_once=True)
