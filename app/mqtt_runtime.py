"""Ephemeral production-consumer diagnostics owned by the native supervisor.

One manager-hosted store serializes small snapshot replacements. A killed MQTT
worker cannot hold its lock: the lock belongs to the manager's server thread.
The supervisor owns generation changes and withdrawal; workers can update only
their current generation. No settings, payloads, exceptions or broker addresses
cross this boundary. Readers receive frozen ordinary data, never a proxy.

Freshness is lifecycle based, not an event/heartbeat timeout. A quiet topic is
healthy. The existing supervisor watchdog withdraws an exited worker within its
normal 0.5-second check cycle, including SIGKILL. Manager failure reads unknown
and diagnostic write failures never interrupt MQTT processing. The manager is
closed after all workers are joined and before supervisor re-exec.

Create this parent-owned boundary before starting application workers/threads.
The native supervisor already does so: classifier initialization and its native
LiteRT/XNNPACK threads belong exclusively to the MQTT child. Tests invoking the
supervisor must release any interpreter they themselves created first. Keep
the native fork lifecycle: spawn would introduce a resource-tracker helper
which survives until exec and can become an unreaped child after re-exec.
"""

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from multiprocessing.managers import BaseManager
from threading import Lock
import uuid


def _now():
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class MqttRuntimeSnapshot:
    """UTC receipt/transition times, with lifecycle authority over freshness.

    ``last_connected_at`` records accepted CONNACK (transport acceptance).
    ``connection_state=connected`` requires the matching successful SUBACK.
    A rejected SUBACK leaves transport accepted but state subscribing/failed.
    Reconnecting clears subscription readiness until a fresh CONNACK/SUBACK.
    Stopped snapshots preserve that generation's history for inspection, but
    are never current/operational. Beginning a generation clears all history.
    """

    worker_generation: str | None = None
    worker_started_at: str | None = None
    connection_state: str = "unknown"
    subscription_state: str = "unknown"
    last_connected_at: str | None = None
    last_disconnected_at: str | None = None
    last_disconnect_reason: str | None = None
    last_disconnect_code: int | None = None
    reason_code: str | None = "worker_not_started"
    last_event_message_at: str | None = None
    updated_at: str | None = None
    is_current: bool = False

    @property
    def is_operational(self):
        return (
            self.is_current and self.connection_state == "connected"
            and self.subscription_state == "subscribed"
        )


def _wire_code(reason):
    # Paho VERSION2 ReasonCode.value is a protocol byte. Never stringify input.
    value = getattr(reason, "value", None)
    return value if type(value) is int and 0 <= value <= 255 else None


class _RuntimeStore:
    """Lives only in the explicitly owned manager, not in Flask or the worker."""

    def __init__(self):
        self._lock = Lock()
        self._snapshot = MqttRuntimeSnapshot(updated_at=_now())

    def read(self):
        with self._lock:
            return self._snapshot

    def begin(self):
        with self._lock:
            generation = uuid.uuid4().hex
            self._snapshot = MqttRuntimeSnapshot(
                worker_generation=generation, connection_state="starting",
                reason_code=None, updated_at=_now(), is_current=True,
            )
            return generation

    def stop(self, generation):
        with self._lock:
            if self._snapshot.worker_generation == generation:
                self._snapshot = replace(
                    self._snapshot, connection_state="stopped",
                    subscription_state="unknown", reason_code="worker_stopped",
                    is_current=False, updated_at=_now(),
                )

    def record(self, generation, action, wire_code):
        with self._lock:
            state = self._snapshot
            if state.worker_generation != generation or not state.is_current:
                return
            now = _now()
            changes = {"updated_at": now}
            if action == "started":
                changes["worker_started_at"] = now
            elif action == "connack_accepted":
                changes.update(
                    connection_state="subscribing", subscription_state="pending",
                    last_connected_at=now, reason_code=None,
                )
            elif action == "connack_rejected":
                changes.update(
                    connection_state="disconnected", subscription_state="unknown",
                    reason_code="auth_rejected" if wire_code in (134, 135) else "connection_rejected",
                )
            elif action == "subscribed":
                changes.update(
                    connection_state="connected", subscription_state="subscribed", reason_code=None,
                )
            elif action == "subscription_failed":
                changes.update(
                    connection_state="subscribing", subscription_state="failed",
                    reason_code="subscription_rejected",
                )
            elif action == "disconnect":
                expected = wire_code == 0
                reason = "expected_disconnect" if expected else "unexpected_disconnect"
                changes.update(
                    connection_state="disconnected" if expected else "reconnecting",
                    subscription_state="unknown", last_disconnected_at=now,
                    last_disconnect_code=wire_code, last_disconnect_reason=reason,
                    reason_code=reason,
                )
            elif action == "event_received":
                changes["last_event_message_at"] = now
            else:
                return
            self._snapshot = replace(state, **changes)


class _RuntimeManager(BaseManager):
    pass


_RuntimeManager.register("MqttRuntimeStore", _RuntimeStore)


class MqttRuntimeReader:
    def __init__(self, store):
        self._store = store


def get_mqtt_runtime_status(reader):
    """Return an immutable safe copy; no broker request or timestamp inference."""
    if reader is None:
        return MqttRuntimeSnapshot()
    try:
        return reader._store.read()
    except Exception:
        return MqttRuntimeSnapshot(reason_code="diagnostics_unavailable")


class MqttRuntimeWriter(MqttRuntimeReader):
    def __init__(self, store, generation):
        super().__init__(store)
        self.generation = generation

    def record(self, action, reason=None):
        try:
            self._store.record(self.generation, action, _wire_code(reason))
        except Exception:
            pass  # Observations must never change production processing.


class MqttRuntimeDiagnostics:
    """Parent-only owner. Pass reader/writer handles explicitly to children."""

    def __init__(self):
        self._manager = _RuntimeManager(shutdown_timeout=1.0)
        self._manager.start()
        self._store = self._manager.MqttRuntimeStore()
        self.reader = MqttRuntimeReader(self._store)

    def begin_worker(self):
        return MqttRuntimeWriter(self._store, self._store.begin())

    def stop_worker(self, writer):
        try:
            self._store.stop(writer.generation)
        except Exception:
            pass

    def close(self):
        self._manager.shutdown()
