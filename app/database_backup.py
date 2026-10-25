"""Database package v1 contract and read-only validation.

Schema compatibility belongs exclusively to db_inspection. Validation extracts
only into owned private storage and never migrates or opens the active database.
"""

import hashlib
import json
import os
import re
import sqlite3
import struct
import zipfile
from contextlib import closing, nullcontext
from dataclasses import asdict, dataclass, fields, replace
from datetime import datetime
from enum import StrEnum

from app.database_backup_storage import (
    BackupIdentity,
    BackupStorageError,
    _check_file,
    open_backup,
    validation_workspace,
)
from app.db_inspection import (
    DatabaseCompatibility,
    DatabaseInspection,
    InspectionError,
    MigrationHistoryEntry,
    inspect_database,
    sqlite_read_checkpoint,
)
from version import VERSION
from wamf_paths import DatabaseBackupPathError

DATABASE_MEMBER = 'database.db'
MANIFEST_MEMBER = 'manifest.json'
APPLICATION_ID = 'WAMF'
BACKUP_FORMAT_VERSION = 1
MAX_DATABASE_BYTES = 1024 ** 3
MAX_MANIFEST_BYTES = 64 * 1024
# Includes both payloads and bounded ZIP headers/central directory. ZIP64,
# comments, prepended/trailing data, and split archives are outside v1.
MAX_ZIP_METADATA_BYTES = 4096
MAX_PACKAGE_BYTES = MAX_DATABASE_BYTES + MAX_MANIFEST_BYTES + MAX_ZIP_METADATA_BYTES
CHUNK_BYTES = 64 * 1024


class ValidationCode(StrEnum):
    VALID = 'valid'
    UNSAFE_STORAGE = 'unsafe_storage'
    INVALID_PACKAGE = 'invalid_package'
    PACKAGE_TOO_LARGE = 'package_too_large'
    INVALID_MEMBERS = 'invalid_members'
    UNSUPPORTED_ZIP = 'unsupported_zip'
    MANIFEST_TOO_LARGE = 'manifest_too_large'
    DATABASE_TOO_LARGE = 'database_too_large'
    INVALID_MANIFEST = 'invalid_manifest'
    UNSUPPORTED_FORMAT = 'unsupported_format'
    SIZE_MISMATCH = 'size_mismatch'
    CHECKSUM_MISMATCH = 'checksum_mismatch'
    METADATA_MISMATCH = 'metadata_mismatch'
    NEWER_DATABASE = 'newer_database'
    INVALID_DATABASE = 'invalid_database'
    UNKNOWN_DATABASE = 'unknown_database'
    WAL_UNSUPPORTED = 'wal_unsupported'
    INTEGRITY_FAILED = 'integrity_failed'


class PackageValidationError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code.value)


def _require(condition, code=ValidationCode.INVALID_MANIFEST):
    if not condition:
        raise PackageValidationError(code)


def _text(value, limit=128):
    return (isinstance(value, str) and 0 < len(value) <= limit
            and all(ord(character) >= 32 and ord(character) != 127 for character in value))


def _migration_id(value):
    return isinstance(value, str) and re.fullmatch(r'[0-9]{3,9}_[a-z][a-z0-9_]{0,100}', value)


def _timestamp(value):
    _require(isinstance(value, str) and re.fullmatch(
        r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?Z', value))
    try:
        return datetime.fromisoformat(value)
    except ValueError as exc:
        raise PackageValidationError(ValidationCode.INVALID_MANIFEST) from exc


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        _require(key not in result)
        result[key] = value
    return result


@dataclass(frozen=True)
class BackupManifest:
    backup_format_version: int
    application: str
    wamf_version: str
    sqlite_version: str
    started_at: str
    completed_at: str
    source_database: str
    database_member: str
    database_size: int
    database_sha256: str
    recognized_migration_id: str | None
    recognized_sequence: int | None
    history: tuple[MigrationHistoryEntry, ...]
    scope: str

    def __post_init__(self):
        _require(type(self.backup_format_version) is int)
        _require(self.backup_format_version == BACKUP_FORMAT_VERSION,
                 ValidationCode.UNSUPPORTED_FORMAT)
        _require(self.application == APPLICATION_ID and self.scope == 'database-only')
        _require(self.database_member == DATABASE_MEMBER)
        _require(_text(self.wamf_version) and _text(self.sqlite_version))
        started, completed = _timestamp(self.started_at), _timestamp(self.completed_at)
        _require(completed >= started)
        _require(_text(self.source_database, 255)
                 and not any(c in self.source_database for c in '/\\:')
                 and self.source_database not in ('.', '..'))
        _require(type(self.database_size) is int and 0 <= self.database_size <= MAX_DATABASE_BYTES)
        _require(isinstance(self.database_sha256, str)
                 and re.fullmatch(r'[0-9a-f]{64}', self.database_sha256))
        if self.recognized_sequence is None:
            _require(self.recognized_migration_id is None)
        else:
            _require(type(self.recognized_sequence) is int
                     and 1 <= self.recognized_sequence <= 999999999
                     and _migration_id(self.recognized_migration_id))
            _require(int(self.recognized_migration_id.split('_', 1)[0]) == self.recognized_sequence)
        _require(type(self.history) is tuple)
        seen = set()
        for entry in self.history:
            _require(isinstance(entry, MigrationHistoryEntry)
                     and _migration_id(entry.migration_id) and _text(entry.applied_at))
            _require(entry.migration_id not in seen)
            seen.add(entry.migration_id)
        if self.recognized_migration_id is not None:
            _require(self.recognized_migration_id in seen)

    @classmethod
    def from_bytes(cls, payload):
        _require(len(payload) <= MAX_MANIFEST_BYTES, ValidationCode.MANIFEST_TOO_LARGE)
        try:
            data = json.loads(payload.decode('utf-8'), object_pairs_hook=_unique_object,
                              parse_constant=lambda _: _require(False))
            _require(type(data) is dict and set(data) == {field.name for field in fields(cls)})
            _require(type(data['history']) is list)
            history = []
            for entry in data['history']:
                _require(type(entry) is dict and set(entry) == {'migration_id', 'applied_at'})
                history.append(MigrationHistoryEntry(**entry))
            data['history'] = tuple(history)
            return cls(**data)
        except (UnicodeError, ValueError, TypeError, RecursionError) as exc:
            if isinstance(exc, PackageValidationError):
                raise
            raise PackageValidationError(ValidationCode.INVALID_MANIFEST) from exc

    def to_bytes(self):
        payload = json.dumps(asdict(self), ensure_ascii=True, separators=(',', ':')).encode('utf-8')
        _require(len(payload) <= MAX_MANIFEST_BYTES, ValidationCode.MANIFEST_TOO_LARGE)
        return payload

    @classmethod
    def from_inspection(cls, inspection, facts, *, started_at, completed_at, source_database):
        """Capture observed metadata, never manufacture a version for legacy DBs."""
        _require(inspection.compatibility in {
            DatabaseCompatibility.CURRENT, DatabaseCompatibility.UPGRADEABLE,
            DatabaseCompatibility.LEGACY,
        })
        return cls(BACKUP_FORMAT_VERSION, APPLICATION_ID, VERSION, sqlite3.sqlite_version,
                   started_at, completed_at, source_database, DATABASE_MEMBER,
                   facts.database_size, facts.database_sha256,
                   inspection.recognized_migration_id, inspection.recognized_sequence,
                   inspection.history, 'database-only')


@dataclass(frozen=True)
class ArtifactFacts:
    database_size: int
    database_sha256: str


@dataclass(frozen=True)
class PackageValidationResult:
    code: ValidationCode
    identity: BackupIdentity
    manifest: BackupManifest | None = None
    facts: ArtifactFacts | None = None
    inspection: DatabaseInspection | None = None
    full_integrity_checked: bool = False

    @property
    def valid(self):
        return self.code == ValidationCode.VALID

    @property
    def compatibility(self):
        return self.inspection.compatibility if self.inspection else None


def _read_limited(source, limit, code, destination=None, checkpoint=None):
    """Count actual streamed bytes, independently of ZIP and manifest metadata."""
    total, chunks, digest = 0, [], hashlib.sha256()
    while True:
        if checkpoint is not None:
            checkpoint()
        chunk = source.read(min(CHUNK_BYTES, limit - total + 1))
        if not chunk:
            break
        total += len(chunk)
        _require(total <= limit, code)
        digest.update(chunk)
        if destination is None:
            chunks.append(chunk)
        else:
            destination.write(chunk)
    return (b''.join(chunks) if destination is None
            else ArtifactFacts(total, digest.hexdigest()))


def _zip_preflight(file):
    """Bound the central directory before ZipFile allocates member objects."""
    size = os.fstat(file.fileno()).st_size
    _require(size <= MAX_PACKAGE_BYTES, ValidationCode.PACKAGE_TOO_LARGE)
    _require(size >= 22, ValidationCode.INVALID_PACKAGE)
    file.seek(-22, os.SEEK_END)
    signature, disk, central_disk, disk_count, count, central_size, offset, comment = struct.unpack(
        '<4s4H2LH', file.read(22))
    _require(signature == b'PK\x05\x06' and not disk and not central_disk and not comment,
             ValidationCode.INVALID_PACKAGE)
    _require(count == disk_count == 2, ValidationCode.INVALID_MEMBERS)
    _require(central_size <= MAX_ZIP_METADATA_BYTES and offset + central_size == size - 22,
             ValidationCode.INVALID_PACKAGE)
    file.seek(0)
    _require(file.read(4) == b'PK\x03\x04', ValidationCode.INVALID_PACKAGE)
    file.seek(0)
    return offset


def _zip_members(archive, file, central_offset):
    members = archive.infolist()
    _require(len(members) == 2 and {m.orig_filename for m in members} == {
        DATABASE_MEMBER, MANIFEST_MEMBER}, ValidationCode.INVALID_MEMBERS)
    previous_end = 0
    for member in sorted(members, key=lambda entry: entry.header_offset):
        _require(member.filename == member.orig_filename and not member.is_dir(),
                 ValidationCode.INVALID_MEMBERS)
        kind = (member.external_attr >> 16) & 0o170000
        _require(kind in (0, 0o100000) and not member.external_attr & 0x10,
                 ValidationCode.INVALID_MEMBERS)
        _require(member.compress_type == zipfile.ZIP_STORED and not member.flag_bits & ~0x800
                 and not member.extra and not member.comment,
                 ValidationCode.UNSUPPORTED_ZIP)
        _require(member.compress_size == member.file_size, ValidationCode.INVALID_PACKAGE)
        limit, code = ((MAX_MANIFEST_BYTES, ValidationCode.MANIFEST_TOO_LARGE)
                       if member.filename == MANIFEST_MEMBER else
                       (MAX_DATABASE_BYTES, ValidationCode.DATABASE_TOO_LARGE))
        _require(member.file_size <= limit, code)
        # Require ordinary contiguous local entries, agreeing with central
        # metadata. No hidden prefix, payload, data descriptor, or ZIP64 fields.
        _require(member.header_offset == previous_end, ValidationCode.INVALID_PACKAGE)
        file.seek(member.header_offset)
        header = file.read(30)
        _require(len(header) == 30, ValidationCode.INVALID_PACKAGE)
        local = struct.unpack('<4s5H3L2H', header)
        _require(local[0] == b'PK\x03\x04' and local[2] == member.flag_bits
                 and local[3] == member.compress_type and local[6] == member.CRC
                 and local[7] == member.compress_size and local[8] == member.file_size
                 and local[9] == len(member.filename) and local[10] == 0,
                 ValidationCode.INVALID_PACKAGE)
        _require(file.read(local[9]) == member.filename.encode('ascii'),
                 ValidationCode.INVALID_MEMBERS)
        previous_end = member.header_offset + 30 + local[9] + member.compress_size
    _require(previous_end == central_offset, ValidationCode.INVALID_PACKAGE)


def check_staged_integrity(path, *, checkpoint=None):
    """Full check of an already inspected, private, stable rollback-format DB.

    Internal staging helper, not an alternative to #52 or an arbitrary-path
    validation entry point. No extensions, user SQL, or writable connection.
    """
    with (closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=0.1)) as conn,
          sqlite_read_checkpoint(conn, checkpoint)):
        conn.execute('PRAGMA query_only = ON')
        conn.execute('PRAGMA trusted_schema = OFF')
        return conn.execute('PRAGMA integrity_check').fetchmany(2) == [('ok',)]


def validate_package(identity, *, staged=False, checkpoint=None):
    """Validate a managed package by ID; delete extraction on success or failure.

    The returned inspection has its private temporary path removed. This is a
    synchronous primitive; upload/request workers must own cancellation
    and resource limits. No migration or active-database lookup occurs here.
    """
    return _validate_package(identity, open_backup(identity, staged=staged), checkpoint)


def validate_open_package(identity, file, *, checkpoint=None, workspace=None, candidate_file=None):
    """Internal pinned-handle variant; caller owns a managed, private file."""
    return _validate_package(identity, nullcontext(file), checkpoint, workspace, candidate_file)


def _validate_package(identity, opener, checkpoint, workspace=None, candidate_file=None):
    result = PackageValidationResult(ValidationCode.INVALID_PACKAGE, identity)
    try:
        with opener as package:
            if checkpoint is not None:
                checkpoint()
            before = _check_file(package.fileno())
            central_offset = _zip_preflight(package)
            with zipfile.ZipFile(package, 'r') as archive:
                _zip_members(archive, package, central_offset)
                with archive.open(MANIFEST_MEMBER) as source:
                    manifest = BackupManifest.from_bytes(_read_limited(
                        source, MAX_MANIFEST_BYTES, ValidationCode.MANIFEST_TOO_LARGE,
                        checkpoint=checkpoint))
                result = replace(result, manifest=manifest)
                with (workspace or validation_workspace)() as (directory, database_path):
                    descriptor = os.open(DATABASE_MEMBER, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                                         | os.O_NOFOLLOW, 0o600, dir_fd=directory)
                    with os.fdopen(descriptor, 'wb') as destination:
                        os.fchmod(destination.fileno(), 0o600)
                        with archive.open(DATABASE_MEMBER) as source:
                            facts = _read_limited(source, MAX_DATABASE_BYTES,
                                                  ValidationCode.DATABASE_TOO_LARGE, destination,
                                                  checkpoint=checkpoint)
                    result = replace(result, facts=facts)
                    _require(facts.database_size == manifest.database_size, ValidationCode.SIZE_MISMATCH)
                    _require(facts.database_sha256 == manifest.database_sha256,
                             ValidationCode.CHECKSUM_MISMATCH)
                    inspection = inspect_database(database_path, check_integrity=True,
                                                  checkpoint=checkpoint)
                    result = replace(result, inspection=replace(inspection, path=None))
                    if inspection.error_category == InspectionError.UNSUPPORTED_JOURNAL_MODE:
                        raise PackageValidationError(ValidationCode.WAL_UNSUPPORTED)
                    rejected = {
                        DatabaseCompatibility.NEWER: ValidationCode.NEWER_DATABASE,
                        DatabaseCompatibility.INVALID: ValidationCode.INVALID_DATABASE,
                        DatabaseCompatibility.UNKNOWN: ValidationCode.UNKNOWN_DATABASE,
                    }
                    if inspection.compatibility in rejected:
                        raise PackageValidationError(rejected[inspection.compatibility])
                    _require((manifest.recognized_sequence, manifest.recognized_migration_id,
                              manifest.history) ==
                             (inspection.recognized_sequence, inspection.recognized_migration_id,
                              inspection.history), ValidationCode.METADATA_MISMATCH)
                    integrity = (check_staged_integrity(database_path) if checkpoint is None else
                                 check_staged_integrity(database_path, checkpoint=checkpoint))
                    _require(integrity, ValidationCode.INTEGRITY_FAILED)
                    result = replace(result, full_integrity_checked=True)
                    if candidate_file is not None:
                        with database_path.open('rb') as source:
                            retained = _read_limited(source, MAX_DATABASE_BYTES,
                                                     ValidationCode.DATABASE_TOO_LARGE,
                                                     candidate_file, checkpoint)
                        _require(retained == facts, ValidationCode.CHECKSUM_MISMATCH)
            after = _check_file(package.fileno())
            _require((before.st_size, before.st_mtime_ns, before.st_ctime_ns) ==
                     (after.st_size, after.st_mtime_ns, after.st_ctime_ns), ValidationCode.INVALID_PACKAGE)
            if checkpoint is not None:
                checkpoint()
        return replace(result, code=ValidationCode.VALID)
    except PackageValidationError as exc:
        return replace(result, code=exc.code)
    except (BackupStorageError, DatabaseBackupPathError):
        return replace(result, code=ValidationCode.UNSAFE_STORAGE)
    except (zipfile.BadZipFile, zipfile.LargeZipFile, EOFError, NotImplementedError,
            UnicodeError, struct.error):
        return replace(result, code=ValidationCode.INVALID_PACKAGE)
    except sqlite3.Error:
        return replace(result, code=ValidationCode.INTEGRITY_FAILED)
    except OSError:
        return replace(result, code=ValidationCode.UNSAFE_STORAGE)
