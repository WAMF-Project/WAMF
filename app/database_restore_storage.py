"""Private, authenticated durable restore records; no active DB file I/O."""

import fcntl
import hashlib
import hmac
import json
import os
import re
import secrets
import stat
import time
from contextlib import contextmanager
from pathlib import Path

from app.database_backup_storage import (
    BackupStorageError,
    _check_file,
    backup_directory,
)

MAX_RECORD_BYTES = 256 * 1024
ARTIFACTS = ('package.zip', 'candidate.db', 'database.db')


class RestoreStorageError(ValueError):
    pass


class RestoreBusyError(RestoreStorageError):
    pass


def operation_id(value):
    if not isinstance(value, str) or not re.fullmatch(r'restore-[0-9a-f]{32}', value):
        raise RestoreStorageError('Invalid restore identifier')
    return value


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True,
                      allow_nan=False).encode('ascii')


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise RestoreStorageError('Duplicate record field')
        result[key] = value
    return result


def _private(descriptor, *, directory=False):
    info = os.fstat(descriptor)
    expected = 0o700 if directory else 0o600
    if (info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != expected
            or (directory and not stat.S_ISDIR(info.st_mode))):
        raise RestoreStorageError('Restore storage must be private')
    if not directory:
        _check_file(descriptor)
    return info


@contextmanager
def private_file(directory, name, *, create=False, write=False):
    flags = (os.O_RDWR if write or create else os.O_RDONLY) | os.O_NOFOLLOW | os.O_NONBLOCK
    if create:
        flags |= os.O_CREAT | os.O_EXCL
    descriptor = os.open(name, flags, 0o600, dir_fd=directory)
    with os.fdopen(descriptor, 'w+b' if create else 'r+b' if write else 'rb') as file:
        if create:
            os.fchmod(file.fileno(), 0o600)
        _private(file.fileno())
        yield file


@contextmanager
def restore_root(*, create=False):
    with backup_directory(create=create) as parent:
        if create:
            try:
                os.mkdir('.restore', mode=0o700, dir_fd=parent)
            except FileExistsError:
                pass
        descriptor = os.open('.restore', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
        try:
            _private(descriptor, directory=True)
            yield descriptor
        finally:
            os.close(descriptor)


@contextmanager
def _lock(directory, name, *, wait=False):
    descriptor = os.open(name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                         0o600, dir_fd=directory)
    try:
        _private(descriptor)
        deadline = time.monotonic() + (2 if wait else 0)
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError as exc:
                if time.monotonic() >= deadline:
                    raise RestoreBusyError('Restore operation is busy') from exc
                time.sleep(0.01)
        yield
    finally:
        os.close(descriptor)


def load_key(root, *, create=False):
    if create:
        with _lock(root, '.catalog.lock', wait=True):
            try:
                with private_file(root, '.record.key', create=True) as file:
                    file.write(secrets.token_bytes(32))
                    file.flush()
                    os.fsync(file.fileno())
                os.fsync(root)
            except FileExistsError:
                pass
    with private_file(root, '.record.key') as file:
        key = file.read(33)
    if len(key) != 32:
        raise RestoreStorageError('Invalid restore record key')
    return key


class OperationStore:
    def __init__(self, directory, key, identifier):
        self.directory, self.key, self.identifier = directory, key, identifier

    def seal(self, value, *, purpose=b'record'):
        return hmac.new(self.key, purpose + b'\0' + canonical(value), hashlib.sha256).hexdigest()

    def read(self):
        with private_file(self.directory, 'operation.json') as file:
            raw = file.read(MAX_RECORD_BYTES + 1)
        if len(raw) > MAX_RECORD_BYTES:
            raise RestoreStorageError('Oversized restore record')
        try:
            envelope = json.loads(raw, object_pairs_hook=unique_object)
            if set(envelope) != {'record', 'mac'} or not isinstance(envelope['mac'], str):
                raise RestoreStorageError('Invalid restore record')
            record = envelope['record']
            if not hmac.compare_digest(self.seal(record), envelope['mac']):
                raise RestoreStorageError('Restore record authentication failed')
            if record['operation_id'] != self.identifier or record['record_version'] != 1:
                raise RestoreStorageError('Restore record identity mismatch')
            return record
        except (ValueError, TypeError, KeyError, RecursionError) as exc:
            raise RestoreStorageError('Invalid restore record') from exc

    def write(self, record):
        payload = canonical({'record': record, 'mac': self.seal(record)})
        if len(payload) > MAX_RECORD_BYTES:
            raise RestoreStorageError('Oversized restore record')
        name = f'.record-{secrets.token_hex(16)}'
        try:
            with private_file(self.directory, name, create=True) as file:
                file.write(payload)
                file.flush()
                os.fsync(file.fileno())
            # Refuse an unsafe existing record before replacing our own metadata.
            try:
                with private_file(self.directory, 'operation.json'):
                    pass
            except FileNotFoundError:
                pass
            os.replace(name, 'operation.json', src_dir_fd=self.directory, dst_dir_fd=self.directory)
            os.fsync(self.directory)
        finally:
            try:
                os.unlink(name, dir_fd=self.directory)
            except FileNotFoundError:
                pass

    def cleanup(self):
        # Fixed names only, never recursive traversal or caller-supplied paths.
        try:
            scratch = os.open('.recheck', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                              dir_fd=self.directory)
        except FileNotFoundError:
            pass
        else:
            try:
                _private(scratch, directory=True)
                self._cleanup_files(scratch)
                os.fsync(scratch)
            finally:
                os.close(scratch)
            os.rmdir('.recheck', dir_fd=self.directory)
        self._cleanup_files(self.directory)
        os.fsync(self.directory)

    @staticmethod
    def _cleanup_files(directory):
        for name in ARTIFACTS:
            try:
                info = os.stat(name, dir_fd=directory, follow_symlinks=False)
                if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                        or info.st_uid != os.geteuid()):
                    raise RestoreStorageError('Unsafe restore artifact')
                os.unlink(name, dir_fd=directory)
            except FileNotFoundError:
                pass


@contextmanager
def operation_store(identifier=None, *, create=False):
    if not create:
        operation_id(identifier)
    try:
        with restore_root(create=create) as root:
            key = load_key(root, create=create)
            if create:
                for _ in range(8):
                    identifier = f'restore-{secrets.token_hex(16)}'
                    try:
                        os.mkdir(identifier, mode=0o700, dir_fd=root)
                        break
                    except FileExistsError:
                        continue
                else:
                    raise RestoreStorageError('Restore identifier collision')
                os.fsync(root)
            descriptor = os.open(identifier, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root)
            try:
                _private(descriptor, directory=True)
                with _lock(descriptor, '.lock'):
                    yield OperationStore(descriptor, key, identifier)
            finally:
                os.close(descriptor)
    except BackupStorageError as exc:
        raise RestoreStorageError('Unsafe restore storage') from exc


@contextmanager
def target_store(path, *, create=False):
    """Authenticated per-target head/retention hold, independent of DB contents."""
    name = hashlib.sha256(str(Path(path).resolve()).encode()).hexdigest()
    with restore_root(create=create) as root:
        if create:
            try:
                os.mkdir('targets', mode=0o700, dir_fd=root)
            except FileExistsError:
                pass
        parent = os.open('targets', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root)
        try:
            _private(parent, directory=True)
            if create:
                try:
                    os.mkdir(name, mode=0o700, dir_fd=parent)
                except FileExistsError:
                    pass
                os.fsync(parent)
                os.fsync(root)
            descriptor = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            try:
                _private(descriptor, directory=True)
                try:
                    key = load_key(root, create=create)
                except FileNotFoundError as exc:
                    raise RestoreStorageError('Missing target journal key') from exc
                yield OperationStore(descriptor, key, f'target-{name}')
            finally:
                os.close(descriptor)
        finally:
            os.close(parent)


@contextmanager
def restore_request_store(*, create=False):
    """One authenticated pending request per managed installation/root."""
    with restore_root(create=create) as root:
        if create:
            try:
                os.mkdir('requests', mode=0o700, dir_fd=root)
            except FileExistsError:
                pass
            os.fsync(root)
        descriptor = os.open('requests', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root)
        try:
            _private(descriptor, directory=True)
            try:
                key = load_key(root, create=create)
            except FileNotFoundError as exc:
                raise RestoreStorageError('Missing restore request key') from exc
            with _lock(descriptor, '.lock', wait=True):
                yield OperationStore(descriptor, key, 'restore-request')
        finally:
            os.close(descriptor)
