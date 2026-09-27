"""Process-safe filesystem persistence for WAMF YAML state.

Each target uses a persistent sibling ``<config>.lock`` file. Its descriptor is
held for the complete backup/write/replace transaction and released when the
transaction context exits.
"""

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
import fcntl
import glob
import logging
import os
from pathlib import Path
import tempfile
import time

import yaml


BACKUP_TIMESTAMP_FORMAT = "%Y%m%d-%H%M%S-%f"
DEFAULT_LOCK_TIMEOUT_SECONDS = 5.0
FILE_MODE = 0o600
logger = logging.getLogger(__name__)


class ConfigPersistenceError(Exception):
    """Base class for controlled configuration persistence failures."""


class ConfigSerializationError(ConfigPersistenceError):
    """Configuration could not be safely serialized."""


class ConfigLockError(ConfigPersistenceError):
    """The process-safe configuration lock could not be used."""


class ConfigLockTimeoutError(ConfigLockError):
    """The configuration lock was not acquired before its deadline."""


class ConfigBackupError(ConfigPersistenceError):
    """The previous active configuration could not be backed up."""


class ConfigReadError(ConfigPersistenceError):
    """The active configuration could not be read inside a transaction."""


class ConfigWriteError(ConfigPersistenceError):
    """The candidate configuration could not atomically replace the active file."""


@dataclass(frozen=True)
class ConfigPersistenceResult:
    config_path: Path
    backup_path: Path | None


def ensure_private_file(path):
    """Set an existing persistence artifact to 0600 only when necessary."""

    path = Path(path)
    if path.exists() and (path.stat().st_mode & 0o777) != FILE_MODE:
        path.chmod(FILE_MODE)


def get_config_backup_paths(config_path):
    """Return existing backups newest-first, preserving the established pattern."""

    return sorted(
        glob.glob(f"{config_path}.*.bak"),
        key=os.path.getmtime,
        reverse=True,
    )


def prune_config_backups(config_path, max_files):
    """Remove backups beyond the configured newest-first retention count."""

    backups = get_config_backup_paths(config_path)
    expired = backups[max_files:]
    for backup_path in expired:
        os.remove(backup_path)
    return len(expired)


def _serialize_config(config, rendered_content=None):
    if not isinstance(config, Mapping):
        raise ConfigSerializationError("Configuration must be a mapping.")
    try:
        snapshot = deepcopy(dict(config))
        if rendered_content is None:
            return yaml.safe_dump(snapshot, sort_keys=False)
        loaded = yaml.safe_load(rendered_content) or {}
        if loaded != snapshot:
            raise ConfigSerializationError(
                "Prepared configuration and rendered YAML do not match."
            )
        return rendered_content
    except ConfigSerializationError:
        raise
    except Exception as exc:
        raise ConfigSerializationError(
            "Configuration could not be safely serialized."
        ) from exc


def _create_backup(config_path):
    backup_path = Path(
        f"{config_path}.{datetime.now().strftime(BACKUP_TIMESTAMP_FORMAT)}.bak"
    )
    descriptor = None
    try:
        previous_content = config_path.read_bytes()
        descriptor = os.open(
            backup_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            FILE_MODE,
        )
        os.fchmod(descriptor, FILE_MODE)
        backup_file = os.fdopen(descriptor, "wb")
        descriptor = None
        with backup_file:
            backup_file.write(previous_content)
            backup_file.flush()
            os.fsync(backup_file.fileno())
        return backup_path
    except OSError as exc:
        if descriptor is not None:
            os.close(descriptor)
        try:
            backup_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise ConfigBackupError(
            "The previous configuration could not be backed up."
        ) from exc


def _write_temporary_file(config_path, content):
    descriptor = None
    temporary_path = None
    try:
        descriptor, name = tempfile.mkstemp(
            dir=config_path.parent,
            prefix=f".{config_path.name}.",
            suffix=".tmp",
        )
        temporary_path = Path(name)
        os.fchmod(descriptor, FILE_MODE)
        with os.fdopen(descriptor, "w", encoding="utf-8") as temporary_file:
            descriptor = None
            temporary_file.write(content)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        return temporary_path
    except (OSError, UnicodeError) as exc:
        if descriptor is not None:
            os.close(descriptor)
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass
        raise ConfigWriteError(
            "The candidate configuration could not be written."
        ) from exc


def _fsync_directory(directory):
    try:
        descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError:
        # The replacement is already atomic and active. Some filesystems do not
        # support directory fsync, so durability of the directory entry is best effort.
        logger.warning("Unable to fsync configuration directory after replacement")


class ConfigPersistenceTransaction:
    """A bounded, process-safe transaction for one explicit config target."""

    def __init__(self, config_path, lock_timeout=DEFAULT_LOCK_TIMEOUT_SECONDS):
        self.config_path = Path(config_path)
        self.lock_path = Path(f"{self.config_path}.lock")
        self.lock_timeout = lock_timeout
        self._lock_descriptor = None

    def __enter__(self):
        if self.lock_timeout < 0:
            raise ValueError("lock_timeout must not be negative")
        try:
            descriptor = os.open(
                self.lock_path,
                os.O_RDWR | os.O_CREAT,
                FILE_MODE,
            )
            os.fchmod(descriptor, FILE_MODE)
        except OSError as exc:
            raise ConfigLockError(
                "The configuration persistence lock could not be opened."
            ) from exc

        deadline = time.monotonic() + self.lock_timeout
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self._lock_descriptor = descriptor
                return self
            except BlockingIOError as exc:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    os.close(descriptor)
                    raise ConfigLockTimeoutError(
                        "Timed out waiting for the configuration persistence lock."
                    ) from exc
                time.sleep(min(0.05, remaining))
            except OSError as exc:
                os.close(descriptor)
                raise ConfigLockError(
                    "The configuration persistence lock could not be acquired."
                ) from exc

    def __exit__(self, exc_type, exc_value, traceback):
        if self._lock_descriptor is not None:
            try:
                fcntl.flock(self._lock_descriptor, fcntl.LOCK_UN)
            finally:
                os.close(self._lock_descriptor)
                self._lock_descriptor = None

    def read_text(self):
        """Read the active configuration while retaining the transaction lock."""

        if self._lock_descriptor is None:
            raise ConfigLockError("Configuration transaction lock is not held.")
        try:
            return self.config_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise ConfigReadError(
                "The active configuration could not be read."
            ) from exc

    def write(
        self,
        config,
        *,
        create_backup=True,
        backup_limit=10,
        rendered_content=None,
    ):
        """Serialize and atomically replace the target while holding this lock."""

        if self._lock_descriptor is None:
            raise ConfigLockError("Configuration transaction lock is not held.")

        content = _serialize_config(config, rendered_content)
        backup_path = None
        if create_backup and self.config_path.exists():
            backup_path = _create_backup(self.config_path)
            try:
                prune_config_backups(self.config_path, max(backup_limit, 0))
            except OSError as exc:
                raise ConfigBackupError(
                    "Expired configuration backups could not be pruned."
                ) from exc
            if not backup_path.exists():
                backup_path = None

        temporary_path = _write_temporary_file(self.config_path, content)
        try:
            os.replace(temporary_path, self.config_path)
        except OSError as exc:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise ConfigWriteError(
                "The active configuration could not be atomically replaced."
            ) from exc

        _fsync_directory(self.config_path.parent)

        return ConfigPersistenceResult(self.config_path, backup_path)


def persist_config(
    config,
    config_path,
    *,
    create_backup=True,
    backup_limit=10,
    lock_timeout=DEFAULT_LOCK_TIMEOUT_SECONDS,
    rendered_content=None,
):
    """Persist a prepared mapping through one complete locked transaction."""

    with ConfigPersistenceTransaction(config_path, lock_timeout) as transaction:
        return transaction.write(
            config,
            create_backup=create_backup,
            backup_limit=backup_limit,
            rendered_content=rendered_content,
        )
