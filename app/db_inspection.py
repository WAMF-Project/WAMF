"""Read-only compatibility checks for diagnostics and staged database files.

This module never resolves configuration defaults and never calls ensure_schema.
A supported upgrade path is structural eligibility, not a promise that a later
write will succeed. Callers must stage/exclude runtime activity before migration.
"""
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path

from app.db import (
    BASELINE_MIGRATION,
    MIGRATION_LOCK_TIMEOUT_MS,
    TABLE_SCHEMAS,
    DatabaseNewerError,
    MigrationHistoryError,
    SchemaMigrationError,
    UnknownMigrationError,
    _history_rows,
    _ordered_migrations,
    _read_history,
    _schema_objects,
    _validate_schema,
)


class DatabaseCompatibility(StrEnum):
    CURRENT = 'current'
    UPGRADEABLE = 'upgradeable'
    LEGACY = 'legacy'
    NEWER = 'newer'
    INVALID = 'invalid'
    UNKNOWN = 'unknown'


class InspectionError(StrEnum):
    EMPTY_DATABASE = 'empty_database'
    NOT_WAMF = 'not_wamf'
    UNKNOWN_MIGRATION = 'unknown_migration'
    INVALID_HISTORY = 'invalid_history'
    INVALID_SCHEMA = 'invalid_schema'
    CORRUPT_DATABASE = 'corrupt_database'
    INTEGRITY_CHECK_FAILED = 'integrity_check_failed'
    MISSING_FILE = 'missing_file'
    INVALID_PATH = 'invalid_path'
    UNREADABLE_DATABASE = 'unreadable_database'
    LOCKED_DATABASE = 'locked_database'
    UNSUPPORTED_JOURNAL_MODE = 'unsupported_journal_mode'
    NEWER_SCHEMA = 'newer_schema'


@dataclass(frozen=True)
class MigrationHistoryEntry:
    # Non-text values in a damaged ledger are represented as None, not coerced.
    migration_id: str | None
    applied_at: str | None


@dataclass(frozen=True)
class DatabaseInspection:
    path: Path | None
    compatibility: DatabaseCompatibility
    latest_sequence: int
    latest_migration_id: str
    history: tuple[MigrationHistoryEntry, ...] = ()
    recognized_sequence: int | None = None
    recognized_migration_id: str | None = None
    migration_required: bool | None = None
    automatic_upgrade_supported: bool = False
    legacy_recognized: bool = False
    unversioned: bool | None = None
    error_category: InspectionError | None = None
    integrity_checked: bool = False
    sqlite_errorcode: int | None = None

    @property
    def migration_ids(self):
        return tuple(entry.migration_id for entry in self.history)

    @property
    def newer_than_build(self):
        return self.compatibility == DatabaseCompatibility.NEWER


def _inspect_connection(conn, result, migrations, check_integrity):
    if check_integrity:
        checks = conn.execute('PRAGMA quick_check').fetchall()
        result = replace(result, integrity_checked=True)
        if checks != [('ok',)]:
            return replace(result, compatibility=DatabaseCompatibility.INVALID,
                           error_category=InspectionError.INTEGRITY_CHECK_FAILED)
    objects = _schema_objects(conn)
    if not objects:
        return replace(result, unversioned=True, error_category=InspectionError.EMPTY_DATABASE)
    try:
        rows = _history_rows(conn)
        history = tuple(sorted((MigrationHistoryEntry(
            version if isinstance(version, str) else None,
            applied_at if isinstance(applied_at, str) else None,
        ) for version, applied_at in rows), key=lambda entry: (
            entry.migration_id or '', entry.applied_at or '',
        )))
        result = replace(result, history=history, unversioned=not rows)
        validated = _read_history(conn, migrations, rows=rows)
    except DatabaseNewerError:
        return replace(result, compatibility=DatabaseCompatibility.NEWER,
                       error_category=InspectionError.NEWER_SCHEMA)
    except UnknownMigrationError:
        return replace(result, error_category=InspectionError.UNKNOWN_MIGRATION)
    except MigrationHistoryError:
        return replace(result, compatibility=DatabaseCompatibility.INVALID,
                       error_category=InspectionError.INVALID_HISTORY)

    application_tables = set(TABLE_SCHEMAS) - {'schema_migrations'}
    if not application_tables.intersection(objects):
        return replace(result, error_category=InspectionError.NOT_WAMF)
    legacy = BASELINE_MIGRATION not in validated
    try:
        _validate_schema(conn, legacy=legacy)
    except SchemaMigrationError:
        return replace(result, compatibility=DatabaseCompatibility.INVALID,
                       error_category=InspectionError.INVALID_SCHEMA)

    if not legacy:
        sequence = len(validated)
        migration = migrations[sequence - 1]
        current = sequence == migrations[-1].sequence
        return replace(result, compatibility=(DatabaseCompatibility.CURRENT if current
                       else DatabaseCompatibility.UPGRADEABLE), recognized_sequence=sequence,
                       recognized_migration_id=migration.migration_id, migration_required=not current,
                       automatic_upgrade_supported=not current)

    # Pre-006 labels were observational. Only exact schema AND complete 001-005
    # history establish the audited pre-adoption version; never infer MAX(id).
    historical = tuple(migration for migration in migrations if migration.sequence < 6)
    if validated == {migration.migration_id for migration in historical}:
        try:
            _validate_schema(conn, legacy=False)
        except SchemaMigrationError:
            pass  # Known omissions remain recognizable legacy, not version 5.
        else:
            migration = historical[-1]
            return replace(result, compatibility=DatabaseCompatibility.UPGRADEABLE,
                           recognized_sequence=migration.sequence,
                           recognized_migration_id=migration.migration_id,
                           migration_required=True, automatic_upgrade_supported=True,
                           legacy_recognized=True)
    return replace(result, compatibility=DatabaseCompatibility.LEGACY,
                   migration_required=True, automatic_upgrade_supported=True,
                   legacy_recognized=True)


@contextmanager
def sqlite_read_checkpoint(conn, checkpoint=None):
    """Cooperative interruption without SQLite swallowing callback exceptions."""
    if checkpoint is None:
        yield
        return
    interrupted = []

    def progress():
        try:
            checkpoint()
        except BaseException as exc:
            interrupted.append(exc)
            raise  # SQLite interrupts; the context re-raises the original below.
        return 0

    conn.set_progress_handler(progress, 1000)
    try:
        checkpoint()
        yield
        checkpoint()
    except sqlite3.Error:
        if interrupted:
            raise interrupted[0]
        raise
    finally:
        conn.set_progress_handler(None, 0)


def inspect_database(db_path, *, check_integrity=False, checkpoint=None):
    """Inspect an existing filesystem path using SQLite mode=ro and query_only.

    Returns structured incompatibility/read errors without raw exception values.
    Invalid build registry configuration remains a programmer error and raises
    SchemaMigrationError. Empty files are UNKNOWN, not eligible restore inputs;
    ordinary first-run initialization still explicitly permits a fresh database.
    The optional quick_check reads data/index pages as well as schema metadata.
    Read-only inspection says nothing about subsequent write permissions.
    WAL-format files return UNKNOWN before opening: SQLite can otherwise create
    sidecars even with mode=ro. Callers must exclude journal-mode changes during
    inspection; staged files should be stable and self-contained.
    An optional cooperative checkpoint may raise to cancel a maintenance job.
    Such exceptions propagate unchanged; lock waits are shortened for that job.
    """
    migrations = _ordered_migrations()
    result = DatabaseInspection(None, DatabaseCompatibility.UNKNOWN,
                                migrations[-1].sequence, migrations[-1].migration_id)
    try:
        path = Path(db_path).expanduser().resolve(strict=True)
    except FileNotFoundError:
        return replace(result, error_category=InspectionError.MISSING_FILE)
    except (ValueError, TypeError, RuntimeError):
        return replace(result, error_category=InspectionError.INVALID_PATH)
    except OSError:
        return replace(result, error_category=InspectionError.UNREADABLE_DATABASE)
    result = replace(result, path=path)
    conn = None
    try:
        if not path.is_file():
            return replace(result, error_category=InspectionError.INVALID_PATH)
        # Header offsets 18/19 are SQLite's write/read format versions. WAL (2)
        # can create or update auxiliary files through a nominally read-only
        # connection. Do not bypass locking with immutable=1 on diagnostics DBs.
        with path.open('rb') as file:
            header = file.read(20)
        if header.startswith(b'SQLite format 3\x00') and 2 in header[18:20]:
            return replace(result, error_category=InspectionError.UNSUPPORTED_JOURNAL_MODE)
        # Do not use connect_db: it creates missing parents/files and uses runtime
        # configuration defaults. as_uri safely quotes '?'/'#'/spaces in paths.
        conn = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True,
                               timeout=MIGRATION_LOCK_TIMEOUT_MS / 1000)
        if checkpoint is not None:
            conn.execute('PRAGMA busy_timeout = 100')
        with sqlite_read_checkpoint(conn, checkpoint):
            conn.execute('PRAGMA query_only = ON')
            conn.execute('BEGIN')
            return _inspect_connection(conn, result, migrations, check_integrity)
    except FileNotFoundError:
        return replace(result, error_category=InspectionError.MISSING_FILE)
    except OSError:
        return replace(result, error_category=InspectionError.UNREADABLE_DATABASE)
    except sqlite3.Error as exc:
        code = getattr(exc, 'sqlite_errorcode', None)
        primary_code = code & 0xff if isinstance(code, int) else None
        if primary_code in (sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB):
            status, error = DatabaseCompatibility.INVALID, InspectionError.CORRUPT_DATABASE
        elif primary_code in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
            status, error = DatabaseCompatibility.UNKNOWN, InspectionError.LOCKED_DATABASE
        else:
            status, error = DatabaseCompatibility.UNKNOWN, InspectionError.UNREADABLE_DATABASE
        return replace(result, compatibility=status, error_category=error, sqlite_errorcode=code)
    finally:
        if conn is not None:
            conn.close()  # Ends the read transaction; no commit or write occurs.
