"""Production diagnostics use real IPC, fake MQTT callbacks, and no broker."""

from dataclasses import FrozenInstanceError, asdict
from datetime import datetime, timezone
import multiprocessing
import signal
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from paho.mqtt.packettypes import PacketTypes
from paho.mqtt.reasoncodes import ReasonCode

from app.mqtt_runtime import MqttRuntimeDiagnostics, MqttRuntimeSnapshot, get_mqtt_runtime_status
from app.mqtt_settings import MqttSettings
from app.process_control import WorkerSupervisor


@pytest.fixture
def runtime():
    owner = MqttRuntimeDiagnostics()
    try:
        yield owner
    finally:
        owner.close()


@pytest.fixture
def consumer(runtime, monkeypatch):
    import speciesid

    writer = runtime.begin_worker()
    writer.record('started')
    context = speciesid.MqttConsumerContext(
        MqttSettings(host='broker', topic_prefix='custom',
                     username='PRIVATE-USER', password='PRIVATE-PASSWORD'), writer,
    )
    client = MagicMock()
    client.subscribe.return_value = (0, 17)
    monkeypatch.setattr(speciesid, 'log_system_event', MagicMock())
    return client, context


def connect(client, context, reason='Success'):
    import speciesid

    speciesid.on_connect(client, context, speciesid.mqtt.ConnectFlags(False),
                         ReasonCode(PacketTypes.CONNACK, reason), None)


def subscribe(client, context, codes=(0,), mid=17):
    import speciesid

    speciesid.on_subscribe(
        client, context, mid,
        [ReasonCode(PacketTypes.SUBACK, identifier=code) for code in codes], None,
    )


def test_initial_and_started_state_are_safe_ordinary_data(runtime):
    initial = get_mqtt_runtime_status(runtime.reader)
    assert initial == MqttRuntimeSnapshot(updated_at=initial.updated_at)
    writer = runtime.begin_worker()
    starting = get_mqtt_runtime_status(runtime.reader)
    assert starting.worker_generation == writer.generation
    assert starting.worker_started_at is None  # Child has not actually started.
    assert starting.connection_state == 'starting'
    assert starting.is_current
    assert not starting.is_operational
    writer.record('started')
    started = get_mqtt_runtime_status(runtime.reader)
    assert isinstance(started, MqttRuntimeSnapshot)
    assert datetime.fromisoformat(started.worker_started_at).tzinfo is not None
    assert type(asdict(started)) is dict
    with pytest.raises(FrozenInstanceError):
        started.connection_state = 'connected'


def test_loaded_classifier_observation_crosses_existing_worker_ipc(runtime):
    from app.classifier_runtime import ClassifierRuntimeSnapshot

    writer = runtime.begin_worker()
    identity = ClassifierRuntimeSnapshot(state='ready', model_id='bird', model_version='aiy-v1')
    child = multiprocessing.Process(target=writer.record_classifier, args=(identity,))
    child.start()
    child.join(timeout=5)
    assert child.exitcode == 0
    assert get_mqtt_runtime_status(runtime.reader).classifier == identity
    runtime.stop_worker(writer)
    assert get_mqtt_runtime_status(runtime.reader).classifier.state == 'stopped'


def test_connack_requires_matching_successful_suback(runtime, consumer):
    client, context = consumer
    connect(client, context)
    pending = get_mqtt_runtime_status(runtime.reader)
    assert pending.connection_state == 'subscribing'
    assert pending.subscription_state == 'pending'
    assert pending.last_connected_at is not None
    assert not pending.is_operational
    client.subscribe.assert_called_once_with('custom/events')
    subscribe(client, context, mid=99)
    assert get_mqtt_runtime_status(runtime.reader) == pending
    subscribe(client, context)
    connected = get_mqtt_runtime_status(runtime.reader)
    assert connected.connection_state == 'connected'
    assert connected.subscription_state == 'subscribed'
    assert connected.is_operational


@pytest.mark.parametrize('codes', [(), (128,), (0, 128), (135,)])
def test_failed_subscription_is_never_operational(runtime, consumer, codes):
    client, context = consumer
    connect(client, context)
    subscribe(client, context, codes)
    state = get_mqtt_runtime_status(runtime.reader)
    assert state.connection_state == 'subscribing'
    assert state.subscription_state == 'failed'
    assert state.reason_code == 'subscription_rejected'
    assert not state.is_operational


def test_immediate_subscribe_failure_is_observed_without_changing_behaviour(runtime, consumer):
    client, context = consumer
    client.subscribe.return_value = (4, None)
    connect(client, context)
    assert get_mqtt_runtime_status(runtime.reader).subscription_state == 'failed'
    client.subscribe.assert_called_once_with('custom/events')
    client.reconnect.assert_not_called()


@pytest.mark.parametrize('reason,code', [
    ('Bad user name or password', 'auth_rejected'), ('Not authorized', 'auth_rejected'),
    ('Server unavailable', 'connection_rejected'),
])
def test_connack_rejections(runtime, consumer, reason, code):
    client, context = consumer
    connect(client, context, reason)
    state = get_mqtt_runtime_status(runtime.reader)
    assert state.connection_state == 'disconnected'
    assert state.reason_code == code
    assert state.last_connected_at is None
    client.subscribe.assert_not_called()


@pytest.mark.parametrize('reason', ['Success', 'Unspecified error', 'Keep alive timeout'])
def test_disconnect_and_reconnect_require_new_ack(runtime, consumer, reason):
    import speciesid

    client, context = consumer
    connect(client, context)
    subscribe(client, context)
    before = get_mqtt_runtime_status(runtime.reader)
    rc = ReasonCode(PacketTypes.DISCONNECT, reason)
    speciesid.on_disconnect(client, context, speciesid.mqtt.DisconnectFlags(False), rc, None)
    disconnected = get_mqtt_runtime_status(runtime.reader)
    assert disconnected.connection_state == ('disconnected' if reason == 'Success' else 'reconnecting')
    assert disconnected.subscription_state == 'unknown'
    assert disconnected.last_disconnected_at is not None
    assert disconnected.last_disconnect_code == rc.value
    assert not disconnected.is_operational
    assert client.reconnect.call_count == (0 if reason == 'Success' else 1)
    subscribe(client, context)  # Late acknowledgement of the previous connection.
    assert get_mqtt_runtime_status(runtime.reader) == disconnected
    client.subscribe.return_value = (0, 18)
    connect(client, context)
    pending = get_mqtt_runtime_status(runtime.reader)
    assert pending.last_connected_at >= before.last_connected_at
    assert pending.last_disconnected_at == disconnected.last_disconnected_at
    assert not pending.is_operational
    subscribe(client, context, mid=17)
    assert get_mqtt_runtime_status(runtime.reader) == pending
    subscribe(client, context, mid=18)
    assert get_mqtt_runtime_status(runtime.reader).is_operational


def test_reconnect_retry_timing_and_safe_state(runtime, consumer):
    import speciesid

    client, context = consumer
    connect(client, context)
    subscribe(client, context)
    client.reconnect.side_effect = [OSError('offline'), RuntimeError('offline'), None]
    observed = []
    with patch.object(speciesid.time, 'sleep', side_effect=lambda seconds: observed.append(
        (seconds, get_mqtt_runtime_status(runtime.reader))
    )):
        speciesid.on_disconnect(client, context, speciesid.mqtt.DisconnectFlags(False),
                                ReasonCode(PacketTypes.DISCONNECT, 'Unspecified error'), None)
    assert client.reconnect.call_count == 3
    assert [seconds for seconds, state in observed] == [60, 60]
    assert all(state.connection_state == 'reconnecting' and not state.is_operational
               for seconds, state in observed)


@pytest.mark.parametrize('retained', [False, True])
def test_receipt_precedes_filtering_decoding_and_processing(runtime, consumer, retained, monkeypatch):
    import speciesid

    client, context = consumer
    process = MagicMock(side_effect=ValueError('malformed'))
    monkeypatch.setattr(speciesid, '_process_live_message', process)
    message = SimpleNamespace(topic='other/events', retain=retained, payload=b'PRIVATE-PAYLOAD')
    speciesid.on_message(client, context, message)
    assert get_mqtt_runtime_status(runtime.reader).last_event_message_at is None
    message.topic = 'custom/events'
    before = datetime.now(timezone.utc)
    speciesid.on_message(client, context, message)
    state = get_mqtt_runtime_status(runtime.reader)
    assert datetime.fromisoformat(state.last_event_message_at) >= before
    assert process.call_count == (0 if retained else 1)
    assert 'PRIVATE' not in repr(state) + repr(asdict(state))


def test_quiet_consumer_stays_operational_without_events_or_heartbeat(runtime, consumer):
    client, context = consumer
    connect(client, context)
    subscribe(client, context)
    first = get_mqtt_runtime_status(runtime.reader)
    assert first.last_event_message_at is None
    assert first.is_operational
    assert get_mqtt_runtime_status(runtime.reader) == first


def test_generations_clear_history_and_reject_old_updates_and_invalidation(runtime, consumer):
    client, old = consumer
    connect(client, old)
    subscribe(client, old)
    old.runtime.record('event_received')
    runtime.stop_worker(old.runtime)
    stopped = get_mqtt_runtime_status(runtime.reader)
    assert stopped.connection_state == 'stopped'
    assert not stopped.is_current
    assert not stopped.is_operational
    new = runtime.begin_worker()
    starting = get_mqtt_runtime_status(runtime.reader)
    assert starting.worker_generation != old.runtime.generation
    for key in ('worker_started_at', 'last_connected_at', 'last_disconnected_at', 'last_event_message_at'):
        assert getattr(starting, key) is None
    old.runtime.record('connack_accepted')
    old.runtime.record('subscribed')
    runtime.stop_worker(old.runtime)
    assert get_mqtt_runtime_status(runtime.reader) == starting
    new.record('started')
    assert get_mqtt_runtime_status(runtime.reader).worker_started_at is not None


def _real_consumer(writer, ready, release):
    writer.record('started')
    writer.record('connack_accepted')
    writer.record('subscribed')
    writer.record('event_received')
    ready.set()
    release.wait(10)


def _read_in_child(reader, connection):
    connection.send(asdict(get_mqtt_runtime_status(reader)))
    connection.close()


@pytest.mark.parametrize('start_method', ['fork', 'spawn'])
def test_updates_cross_both_worker_and_flask_process_boundaries(runtime, start_method):
    ctx = multiprocessing.get_context(start_method)
    ready, release = ctx.Event(), ctx.Event()
    writer = runtime.begin_worker()
    with WorkerSupervisor(ctx.Process) as workers:
        process = workers.start(_real_consumer, args=(writer, ready, release),
                                on_exit=lambda: runtime.stop_worker(writer))
        assert ready.wait(5)
        parent = get_mqtt_runtime_status(runtime.reader)
        assert parent.is_operational
        assert parent.last_event_message_at is not None
        receive, send = ctx.Pipe(duplex=False)
        reader_process = workers.start(_read_in_child, args=(runtime.reader, send))
        assert receive.poll(5)
        assert receive.recv() == asdict(parent)
        reader_process.join(5)
        assert reader_process.exitcode == 0
        receive.close()
        send.close()
        release.set()
        process.join(5)
        assert process.exitcode == 0
        workers.reap(process)
        assert not get_mqtt_runtime_status(runtime.reader).is_operational


@pytest.mark.parametrize('termination', ['terminate', 'kill'])
def test_abrupt_worker_exit_is_invalidated_by_supervisor(runtime, termination):
    ready, release = multiprocessing.Event(), multiprocessing.Event()
    writer = runtime.begin_worker()
    with WorkerSupervisor(multiprocessing.Process) as workers:
        process = workers.start(_real_consumer, args=(writer, ready, release),
                                on_exit=lambda: runtime.stop_worker(writer))
        assert ready.wait(5)
        assert get_mqtt_runtime_status(runtime.reader).is_operational
        getattr(process, termination)()
        process.join(5)
        assert not process.is_alive()
        workers.reap(process)
        stopped = get_mqtt_runtime_status(runtime.reader)
        assert stopped.connection_state == 'stopped'
        assert not stopped.is_current
        assert not stopped.is_operational
        writer.record('subscribed')
        assert get_mqtt_runtime_status(runtime.reader) == stopped
    new = runtime.begin_worker()
    new.record('started')
    assert get_mqtt_runtime_status(runtime.reader).worker_generation == new.generation


def test_safe_reasons_and_secret_non_leakage(runtime, consumer, caplog):
    client, context = consumer
    context.runtime.record('PRIVATE-RAW-EXCEPTION', ValueError('PRIVATE-PASSWORD'))
    context.runtime.record('disconnect', SimpleNamespace(value='PRIVATE-PASSWORD'))
    state = get_mqtt_runtime_status(runtime.reader)
    assert state.last_disconnect_code is None
    assert state.last_disconnect_reason == 'unexpected_disconnect'
    assert 'PRIVATE' not in repr(state) + repr(context) + caplog.text


def test_unavailable_manager_fails_closed_without_interrupting_worker(runtime):
    writer = runtime.begin_worker()
    runtime._manager._process.kill()
    runtime._manager._process.join(5)
    writer.record('started')
    state = get_mqtt_runtime_status(runtime.reader)
    assert state.connection_state == 'unknown'
    assert state.reason_code == 'diagnostics_unavailable'
    assert not state.is_current
    assert not state.is_operational
    assert get_mqtt_runtime_status(None) == MqttRuntimeSnapshot()


def test_supervisor_closes_manager_after_workers_and_before_reexec(monkeypatch):
    owner = MqttRuntimeDiagnostics()
    manager_process = owner._manager._process
    writer = owner.begin_worker()
    events = []
    process = MagicMock(pid=123)
    process.terminate.side_effect = lambda: events.append('terminate')
    process.join.side_effect = lambda: events.append('join')
    def close():
        assert not get_mqtt_runtime_status(owner.reader).is_current
        assert events == ['terminate', 'join']
        owner.close()
        events.append('close')
    def reexec():
        assert not manager_process.is_alive()
        assert events == ['terminate', 'join', 'close']
    monkeypatch.setattr('app.process_control.reexec_application', reexec)
    with WorkerSupervisor(MagicMock(return_value=process), on_workers_stopped=close) as workers:
        workers.start(lambda: None, on_exit=lambda: owner.stop_worker(writer))
        writer.record('connack_accepted')
        writer.record('subscribed')
        workers._request_shutdown(signal.SIGUSR1, None)
    assert not manager_process.is_alive()


def test_classifier_failure_keeps_generation_start_visible_until_supervisor_withdraws(runtime, monkeypatch):
    import speciesid

    writer = runtime.begin_worker()
    monkeypatch.setattr(speciesid, 'configure_worker_signals', lambda: None)
    monkeypatch.setattr(speciesid, 'config', {'mqtt': {'host': 'broker'},
                                           'classification': {'model': 'missing.tflite'}})
    monkeypatch.setattr(speciesid, 'initialize_classifier', MagicMock(side_effect=ValueError('PRIVATE')))
    with patch.object(speciesid.mqtt, 'Client') as factory:
        with pytest.raises(ValueError):
            speciesid.run_mqtt_client(writer)
    factory.assert_not_called()
    state = get_mqtt_runtime_status(runtime.reader)
    assert state.worker_started_at is not None
    assert state.connection_state == 'starting'
    runtime.stop_worker(writer)
    assert get_mqtt_runtime_status(runtime.reader).connection_state == 'stopped'


def test_worker_entry_wires_context_without_changing_client_setup(runtime, monkeypatch):
    import speciesid

    writer = runtime.begin_worker()
    monkeypatch.setattr(speciesid, 'configure_worker_signals', lambda: None)
    monkeypatch.setattr(speciesid, 'config', {
        'mqtt': {'host': 'broker', 'topic_prefix': 'expected'},
        'classification': {'model': 'model.tflite'},
    })
    initialize = MagicMock()
    monkeypatch.setattr(speciesid, 'initialize_classifier', initialize)
    with patch.object(speciesid.mqtt, 'Client') as factory, patch.object(speciesid, 'log_system_event'):
        client = factory.return_value
        client.subscribe.return_value = (0, 17)
        def connecting(*args):
            initialize.assert_called_once()
            context = client.user_data_set.call_args.args[0]
            assert context.settings.host == 'broker'
            assert context.runtime.generation == writer.generation
            connect(client, context)
            assert not get_mqtt_runtime_status(runtime.reader).is_operational
            subscribe(client, context)
        client.connect.side_effect = connecting
        speciesid.run_mqtt_client(writer)
        assert factory.call_args.kwargs['callback_api_version'] is speciesid.mqtt.CallbackAPIVersion.VERSION2
        client.connect.assert_called_once_with('broker', 1883)
        client.subscribe.assert_called_once_with('expected/events')
        client.loop_forever.assert_called_once_with()
        assert get_mqtt_runtime_status(runtime.reader).is_operational


def test_native_watchdog_withdraws_old_generation_before_starting_replacement(runtime, monkeypatch):
    import speciesid

    monkeypatch.setattr(speciesid, 'config', {
        'mqtt': {'host': 'broker', 'topic_prefix': 'frigate'},
        'frigate': {'frigate_url': 'http://frigate', 'camera': ['garden']},
        'classification': {'model': 'model.tflite', 'threshold': 0.7},
    })
    for name in ('load_config', 'ensure_schema', 'log_system_event'):
        monkeypatch.setattr(speciesid, name, MagicMock())
    monkeypatch.setattr(speciesid, 'MqttRuntimeDiagnostics', lambda: runtime)
    flask = MagicMock(pid=101)
    flask.is_alive.side_effect = [True, True, False, False]
    old, replacement = MagicMock(pid=102), MagicMock(pid=103)
    old.is_alive.return_value = False
    replacement.is_alive.return_value = True
    generations = []
    start_states = []
    real_begin = runtime.begin_worker
    def begin():
        start_states.append(get_mqtt_runtime_status(runtime.reader))
        writer = real_begin()
        generations.append(writer.generation)
        return writer
    monkeypatch.setattr(runtime, 'begin_worker', begin)
    final = []
    real_close = runtime.close
    def close():
        final.append(get_mqtt_runtime_status(runtime.reader))
        real_close()
    monkeypatch.setattr(runtime, 'close', close)
    processes = iter([flask, old, replacement])
    def factory(target, args):
        process = next(processes)
        if target is speciesid.run_mqtt_client:
            writer, runtime_config, database_path = args
            assert runtime_config is speciesid.config
            assert database_path == speciesid.DBPATH
            def start():
                writer.record('started')
                if process is old:
                    writer.record('connack_accepted')
                    writer.record('subscribed')
            process.start.side_effect = start
        else:
            assert target is speciesid.run_webui
            assert args == (runtime.reader, speciesid.config, speciesid.DBPATH)
        return process
    monkeypatch.setattr(speciesid.multiprocessing, 'Process', factory)
    speciesid.main()
    assert len(generations) == 2
    assert generations[0] != generations[1]
    assert start_states[1].worker_generation == generations[0]
    assert start_states[1].connection_state == 'stopped'
    assert not start_states[1].is_current
    assert final[0].worker_generation == generations[1]
    assert final[0].connection_state == 'stopped'
    assert not final[0].is_current


def test_start_failure_still_withdraws_generation_and_closes_resources(runtime):
    writer = runtime.begin_worker()
    child = MagicMock(pid=None)
    child.start.side_effect = RuntimeError('could not start')
    states = []
    def closed():
        states.append(get_mqtt_runtime_status(runtime.reader))
    with pytest.raises(RuntimeError):
        with WorkerSupervisor(MagicMock(return_value=child), on_workers_stopped=closed) as workers:
            workers.start(lambda: None, on_exit=lambda: runtime.stop_worker(writer))
    assert states[0].connection_state == 'stopped'
    assert not states[0].is_current
    child.terminate.assert_not_called()


def test_manager_is_created_before_application_threads_and_remains_usable_from_them():
    release = threading.Event()
    default_method = multiprocessing.get_start_method()
    owner = MqttRuntimeDiagnostics()
    writer = owner.begin_worker()
    def application_worker():
        writer.record('started')
        writer.record('connack_accepted')
        writer.record('subscribed')
        release.set()
    thread = threading.Thread(target=application_worker, name='mqtt-manager-regression-thread')
    try:
        # Match native ownership: create the manager first, then run application
        # work. No subsequent diagnostic operation may fork the threaded caller.
        with patch('os.fork', side_effect=AssertionError('Diagnostics must not fork after startup')):
            thread.start()
            assert release.wait(5)
            assert get_mqtt_runtime_status(owner.reader).is_operational
            owner.stop_worker(writer)
            assert not get_mqtt_runtime_status(owner.reader).is_current
            thread.join(timeout=5)
            owner.close()
        assert multiprocessing.get_start_method() == default_method
    finally:
        owner.close()
        thread.join(timeout=5)
    assert not thread.is_alive()


@pytest.mark.parametrize('setup_required', [False, True])
def test_native_supervisor_creates_diagnostics_before_any_worker(runtime, monkeypatch, setup_required):
    import speciesid

    order = []
    monkeypatch.setattr(speciesid, 'config', {})
    monkeypatch.setattr(speciesid, 'load_config', lambda: None)
    monkeypatch.setattr(speciesid, 'ensure_schema', lambda path: None)
    monkeypatch.setattr(speciesid, 'log_system_event', MagicMock())
    monkeypatch.setattr(speciesid, 'preflight', lambda config: ['setup'] if setup_required else [])
    def diagnostics():
        order.append('diagnostics')
        return runtime
    monkeypatch.setattr(speciesid, 'MqttRuntimeDiagnostics', diagnostics)
    def process(target, args):
        assert order[0] == 'diagnostics'
        order.append(target.__name__)
        child = MagicMock(pid=123)
        child.is_alive.return_value = False
        return child
    monkeypatch.setattr(speciesid.multiprocessing, 'Process', process)
    speciesid.main()
    expected = ['diagnostics', 'run_webui']
    if not setup_required:
        expected.append('run_mqtt_client')
    assert order == expected
