"""Request-owned catalogue installation and ephemeral progress, without activation."""

from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
import errno
import fcntl
import json
import logging
import math
import os
from pathlib import Path
import selectors
import stat
import subprocess
import sys
import threading
import time
from contextlib import contextmanager

from app.config_loader import load_runtime_config
from app.config_migration import ConfigMigrationError
from app.model_catalogue import ModelCatalogueError, load_catalogue, parse_catalogue, safe_identity
from app.model_registry import ModelSelectionError, resolve_classifier_model
from app.model_validation import MESSAGES as VALIDATION_MESSAGES
from wamf_paths import ModelPathError, get_models_path


CONNECT_TIMEOUT_SECONDS = 5.0
READ_TIMEOUT_SECONDS = 15.0
INSTALL_TIMEOUT_SECONDS = 600.0
MAX_ARTIFACT_BYTES = 256 * 1024 * 1024
MAX_REDIRECTS = 5
CHUNK_BYTES = 64 * 1024
logger = logging.getLogger(__name__)

MESSAGES = {
    "installed": "Model installed and validated. Selection has not changed.",
    "already_installed": "This model version is already installed and validated.",
    "already_available_bundled": "This model is already available in the WAMF installation.",
    "install_in_progress": "Another model installation is in progress.",
    "unknown_model": "The model is not in the supported catalogue.",
    "unknown_version": "The model version is not in the supported catalogue.",
    "invalid_catalogue": "The supported model catalogue could not be used.",
    "invalid_catalogue_source": "The catalogue does not specify an approved artifact source.",
    "connection_error": "The artifact server could not be reached.",
    "tls_error": "A verified TLS connection to the artifact server could not be established.",
    "redirect_rejected": "The artifact server redirected to an unapproved destination.",
    "too_many_redirects": "The artifact server exceeded the redirect limit.",
    "http_error": "The artifact server returned an unsuccessful HTTP status.",
    "unexpected_response": "The artifact server returned an unsupported response.",
    "timeout": "Model installation exceeded a network or operation time limit.",
    "oversized": "The artifact exceeds its allowed size.",
    "size_mismatch": "The artifact size does not match the catalogue.",
    "checksum_mismatch": "The artifact checksum does not match the catalogue.",
    "validation_failed": "The model did not pass classifier validation.",
    "filesystem_error": "Model storage could not be updated.",
    "permission_error": "WAMF does not have permission to update model storage.",
    "unsafe_path": "The artifact path is not safe for managed installation.",
    "repair_blocked": "Repair is deferred because this file may be selected or running.",
    "configuration_error": "Model storage or selection configuration could not be read safely.",
    "internal_error": "Model installation could not complete.",
}
SUCCESS_CODES = frozenset({"installed", "already_installed", "already_available_bundled"})
PHASES = frozenset({"preparing", "downloading", "verifying", "validating", "installing"})


@dataclass(frozen=True)
class ModelInstallResult:
    state: str
    code: str
    message: str
    validation_code: str | None = None
    durability_warning: bool = False

    @property
    def success(self):
        return self.code in SUCCESS_CODES


def install_result(code, *, validation_code=None, durability_warning=False):
    state = code if code in SUCCESS_CODES or code == "install_in_progress" else "failed"
    detail = validation_code if validation_code in VALIDATION_MESSAGES else None
    return ModelInstallResult(state, code, MESSAGES[code], detail, durability_warning)


@dataclass(frozen=True)
class InstallSnapshot:
    model_id: str
    model_version: str
    phase: str
    bytes_received: int
    bytes_total: int
    started_at: str
    updated_at: str
    terminal_result: ModelInstallResult | None = None

    @property
    def percent(self):
        if not self.bytes_total or (self.terminal_result and self.terminal_result.code != "installed"):
            return None
        return round(100 * self.bytes_received / self.bytes_total, 1)


class InstallError(Exception):
    def __init__(self, code, *, validation_code=None):
        super().__init__(code)
        self.code = code
        self.validation_code = validation_code


def artifact_from_payload(data):
    """Reuse M1's schema at the private worker boundary."""
    artifact = dict(data)
    model_id = artifact.pop("model_id")
    return parse_catalogue({
        "schema_version": 1,
        "default": {"model_id": model_id, "version": artifact["version"]},
        "models": [{"id": model_id, "name": "Install candidate", "description": "Release artifact",
                    "artifacts": [artifact]}],
    }).default_artifact


@contextmanager
def model_install_guard(root):
    """One non-waiting flock writer per model root, independent of retention.

    Pass the descriptor to the owned worker so the lock also survives until
    that child exits if its parent dies. Never unlink the persistent lock file.
    """
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        descriptor = os.open(root / ".install.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise InstallError("unsafe_path") from exc
        raise
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise InstallError("unsafe_path")
        os.fchmod(descriptor, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise InstallError("install_in_progress") from exc
        yield descriptor
    finally:
        # Closing the parent descriptor releases ownership only after its
        # worker's inherited descriptor has also closed. No explicit LOCK_UN.
        os.close(descriptor)


class ModelInstaller:
    """Synchronous service for an explicit request, with thread-safe snapshots.

    No thread, subprocess or filesystem work starts on construction/import.
    M3 may poll snapshot() from another request. Results are forgotten on exit.
    """

    def __init__(self):
        self._operation_lock = threading.Lock()
        self._snapshot_lock = threading.Lock()
        self._snapshot = None

    def snapshot(self):
        with self._snapshot_lock:
            return self._snapshot

    def _publish(self, *, phase, received=None, result=None):
        with self._snapshot_lock:
            previous = self._snapshot
            self._snapshot = replace(
                previous, phase=phase, updated_at=datetime.now(UTC).isoformat(),
                bytes_received=previous.bytes_received if received is None else received,
                terminal_result=result,
            )
        if phase != previous.phase:
            logger.info("Model install %s/%s phase=%s", previous.model_id, previous.model_version, phase)

    def install_model(self, model_id, model_version, *, config=None, catalogue=None,
                      running_paths=None, timeout_seconds=INSTALL_TIMEOUT_SECONDS):
        """Install exactly one supported identity; never save config or restart.

        running_paths=None means running identity is unknown: existing corrupt
        files cannot be replaced. An internal caller with authoritative startup
        knowledge may supply running paths (or () when no classifier runs).
        Configured selection is additionally protected. M3 activation must use
        the same install guard when changing selection, not race an installer.
        """
        if (not isinstance(timeout_seconds, (int, float)) or isinstance(timeout_seconds, bool)
                or not math.isfinite(timeout_seconds) or timeout_seconds <= 0):
            raise ValueError("Install timeout must be finite and positive")
        if not self._operation_lock.acquire(blocking=False):
            return install_result("install_in_progress")
        try:
            try:
                safe_identity(model_id, "model_id")
            except ModelCatalogueError:
                return install_result("unknown_model")
            try:
                safe_identity(model_version, "model_version")
            except ModelCatalogueError:
                return install_result("unknown_version")
            started = time.monotonic()
            stamp = datetime.now(UTC).isoformat()
            with self._snapshot_lock:
                self._snapshot = InstallSnapshot(model_id, model_version, "preparing", 0, 0, stamp, stamp)
            try:
                catalogue = catalogue if catalogue is not None else load_catalogue()
                if not any(model.id == model_id for model in catalogue.models):
                    raise InstallError("unknown_model")
                artifact = catalogue.get_artifact(model_id, model_version)
                if artifact is None:
                    raise InstallError("unknown_version")
                # Recheck domain objects supplied by internal dependency injection.
                data = {key: value for key, value in asdict(artifact).items() if value is not None}
                artifact = artifact_from_payload(data)
                with self._snapshot_lock:
                    self._snapshot = replace(self._snapshot, bytes_total=artifact.size_bytes)
                if artifact.bundled_path is None:
                    from app.model_download import validate_source

                    validate_source(artifact.download_url)
                    if artifact.size_bytes > MAX_ARTIFACT_BYTES:
                        raise InstallError("oversized")
                config = load_runtime_config() if config is None else config
                root = get_models_path(config).resolve()
                protected = None if running_paths is None else [str(Path(path).resolve()) for path in running_paths]
                try:
                    selected = resolve_classifier_model(config, catalogue).path.resolve()
                except ModelSelectionError:
                    # Unknown/incomplete selection must never authorize repairs.
                    protected = None
                else:
                    if protected is not None:
                        protected.append(str(selected))
                logger.info("Model install requested: %s/%s", model_id, model_version)
                with model_install_guard(root) as lock_fd:
                    payload = {"artifact": data, "root": str(root), "protected_paths": protected}
                    result = self._run_worker(payload, lock_fd, started, timeout_seconds)
            except InstallError as exc:
                result = install_result(exc.code, validation_code=exc.validation_code)
            except ModelCatalogueError:
                result = install_result("invalid_catalogue")
            except (ModelPathError, RuntimeError):
                result = install_result("unsafe_path")
            except PermissionError:
                result = install_result("permission_error")
            except OSError:
                result = install_result("filesystem_error")
            except (ConfigMigrationError, ValueError, TypeError, AttributeError):
                result = install_result("configuration_error")
            self._publish(phase="complete" if result.success else "failed", result=result)
            logger.info("Model install %s/%s result=%s", model_id, model_version, result.code)
            if result.durability_warning:
                logger.warning("Installed model %s/%s directory synchronization was incomplete", model_id, model_version)
            return result
        finally:
            self._operation_lock.release()

    def _run_worker(self, payload, lock_fd, started, timeout_seconds):
        """Read bounded JSON events; kill/reap on timeout, malformed output or exit."""
        process = None
        terminal = None
        try:
            encoded = json.dumps(payload).encode("utf-8")
            # Keep the single control write within PIPE_BUF even if a child
            # stalls before reading stdin. Metadata is deliberately small.
            if len(encoded) > 4096:
                return install_result("invalid_catalogue")
            if time.monotonic() - started >= timeout_seconds:
                return install_result("timeout")
            worker = Path(__file__).with_name("model_install_worker.py")
            process = subprocess.Popen(
                [sys.executable, str(worker), str(os.getpid())], stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, pass_fds=(lock_fd,),
            )
            process.stdin.write(encoded)
            process.stdin.close()
            buffer = b""
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                while True:
                    remaining = timeout_seconds - (time.monotonic() - started)
                    if remaining <= 0:
                        return install_result("timeout")
                    if not selector.select(remaining):
                        return install_result("timeout")
                    chunk = os.read(process.stdout.fileno(), 4096)
                    if not chunk:
                        break
                    buffer += chunk
                    if len(buffer) > 8192:
                        return install_result("internal_error")
                    while b"\n" in buffer:
                        line, buffer = buffer.split(b"\n", 1)
                        event = json.loads(line)
                        if terminal is not None:
                            return install_result("internal_error")
                        if "result" in event:
                            code = event["result"]
                            if code not in MESSAGES:
                                return install_result("internal_error")
                            terminal = install_result(
                                code, validation_code=event.get("validation_code"),
                                durability_warning=event.get("durability_warning") is True,
                            )
                        else:
                            phase, received = event["phase"], event["bytes_received"]
                            snapshot = self.snapshot()
                            if (phase not in PHASES or type(received) is not int
                                    or not snapshot.bytes_received <= received <= snapshot.bytes_total):
                                return install_result("internal_error")
                            self._publish(phase=phase, received=received)
            remaining = max(0, timeout_seconds - (time.monotonic() - started))
            process.wait(timeout=remaining)
            if process.returncode != 0 or buffer or terminal is None:
                return install_result("internal_error")
            return terminal
        except subprocess.TimeoutExpired:
            return install_result("timeout")
        except (OSError, ValueError, KeyError, TypeError):
            return install_result("internal_error")
        finally:
            if process is not None:
                if process.poll() is None:
                    process.kill()
                process.wait()
                process.stdout.close()
                process.stdin.close()


_installer = ModelInstaller()
install_model = _installer.install_model
get_install_status = _installer.snapshot
