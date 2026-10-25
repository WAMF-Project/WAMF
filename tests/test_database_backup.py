"""Package primitives use synthetic DBs and a completely isolated data mount."""

import hashlib
import io
import json
import os
import sqlite3
import stat
import struct
import warnings
import zipfile
from dataclasses import asdict

import pytest

import wamf_paths
from app import database_backup as backup
from app import database_backup_storage as storage
from app import db
from app.db_inspection import DatabaseCompatibility, inspect_database
from tests.test_db_migrations import add_history, current_prebaseline, legacy_database
from version import VERSION


@pytest.fixture(autouse=True)
def isolated_storage(monkeypatch, tmp_path):
    monkeypatch.setattr(wamf_paths, 'REPO_ROOT', tmp_path)


@pytest.fixture
def current(tmp_path):
    path = tmp_path / 'synthetic.db'
    db.ensure_schema(path)
    return path


def manifest_for(path):
    payload = path.read_bytes()
    return backup.BackupManifest.from_inspection(
        inspect_database(path, check_integrity=True),
        backup.ArtifactFacts(len(payload), hashlib.sha256(payload).hexdigest()),
        started_at='2026-10-24T12:00:00.000001Z',
        completed_at='2026-10-24T12:00:01Z', source_database='speciesid.db')


def package(payload, manifest, *, members=None, compression=zipfile.ZIP_STORED, staged=True):
    if isinstance(manifest, backup.BackupManifest):
        manifest = manifest.to_bytes()
    elif isinstance(manifest, dict):
        manifest = json.dumps(manifest).encode()
    with (storage.reserve_backup(staged=staged) as (identity, file),
          zipfile.ZipFile(file, 'w', compression=compression) as archive):
        archive.writestr('database.db', payload)
        archive.writestr('manifest.json', manifest)
        for name, data in members or []:
            archive.writestr(name, data)
    return identity


def validate(identity):
    return backup.validate_package(identity, staged=True)


def artifact_path(identity, *, staged=True):
    root = (wamf_paths.get_database_backup_staging_root() if staged
            else wamf_paths.get_database_backup_root())
    return root / identity.filename


@pytest.mark.parametrize('kind,compatibility', [
    ('current', DatabaseCompatibility.CURRENT),
    ('upgradeable', DatabaseCompatibility.UPGRADEABLE),
    ('legacy', DatabaseCompatibility.LEGACY),
])
def test_valid_package_and_non_mutating_repeat_validation(tmp_path, kind, compatibility, monkeypatch):
    path = tmp_path / 'input.db'
    {'current': db.ensure_schema, 'upgradeable': current_prebaseline,
     'legacy': legacy_database}[kind](path)
    original_db = path.read_bytes(), path.stat().st_mtime_ns
    manifest = manifest_for(path)
    identity = package(path.read_bytes(), manifest)
    stored = artifact_path(identity)
    before = stored.read_bytes(), stored.stat().st_mtime_ns

    def forbidden(*args, **kwargs):
        pytest.fail('Validation must never migrate or use runtime DB defaults')

    monkeypatch.setattr(db, 'ensure_schema', forbidden)
    monkeypatch.setattr(db, 'connect_db', forbidden)
    monkeypatch.setattr(wamf_paths, 'get_database_path', forbidden)
    result = validate(identity)
    assert result.valid and result.code is backup.ValidationCode.VALID
    assert result.compatibility is compatibility
    assert result.inspection.integrity_checked and result.full_integrity_checked
    assert result.inspection.path is None
    assert result.manifest == manifest
    assert result.facts.database_size == len(path.read_bytes())
    assert result.facts.database_sha256 == hashlib.sha256(path.read_bytes()).hexdigest()
    assert validate(identity) == result
    assert (stored.read_bytes(), stored.stat().st_mtime_ns) == before
    assert (path.read_bytes(), path.stat().st_mtime_ns) == original_db
    assert list(wamf_paths.get_database_backup_staging_root().iterdir()) == [stored]
    assert manifest.wamf_version == VERSION
    assert manifest.sqlite_version == sqlite3.sqlite_version
    if kind == 'legacy':
        assert manifest.recognized_sequence is manifest.recognized_migration_id is None


def test_validate_managed_package(current):
    identity = package(current.read_bytes(), manifest_for(current), staged=False)
    assert backup.validate_package(identity).valid


@pytest.mark.parametrize('kind,compatibility,code', [
    ('newer', DatabaseCompatibility.NEWER, backup.ValidationCode.NEWER_DATABASE),
    ('invalid', DatabaseCompatibility.INVALID, backup.ValidationCode.INVALID_DATABASE),
    ('corrupt', DatabaseCompatibility.INVALID, backup.ValidationCode.INVALID_DATABASE),
    ('foreign', DatabaseCompatibility.UNKNOWN, backup.ValidationCode.UNKNOWN_DATABASE),
    ('unknown', DatabaseCompatibility.UNKNOWN, backup.ValidationCode.UNKNOWN_DATABASE),
    ('empty', DatabaseCompatibility.UNKNOWN, backup.ValidationCode.UNKNOWN_DATABASE),
    ('wal', DatabaseCompatibility.UNKNOWN, backup.ValidationCode.WAL_UNSUPPORTED),
])
def test_actual_database_is_authoritative(current, kind, compatibility, code):
    manifest = asdict(manifest_for(current))
    if kind == 'newer':
        add_history(current, ['007_future_schema'])
    elif kind == 'invalid':
        with sqlite3.connect(current) as conn:
            conn.execute("DELETE FROM schema_migrations WHERE version = '002_detection_media_columns'")
    elif kind == 'foreign':
        current.unlink()
        with sqlite3.connect(current) as conn:
            conn.execute('CREATE TABLE foreign_data (value TEXT)')
    elif kind == 'unknown':
        current.unlink()
        legacy_database(current)
        add_history(current, ['003_unrecognized_change'])
    payload = current.read_bytes()
    if kind == 'corrupt':
        payload = b'this is not sqlite'
    elif kind == 'empty':
        payload = b''
    elif kind == 'wal':
        payload = payload[:18] + bytes([2, 2]) + payload[20:]
    manifest.update(database_size=len(payload), database_sha256=hashlib.sha256(payload).hexdigest())
    identity = package(payload, manifest)
    result = validate(identity)
    assert not result.valid and result.code is code
    assert result.compatibility is compatibility
    assert result.inspection.path is None
    assert list(wamf_paths.get_database_backup_staging_root().iterdir()) == [artifact_path(identity)]


@pytest.mark.parametrize('field,value,code', [
    ('database_size', 1, backup.ValidationCode.SIZE_MISMATCH),
    ('database_sha256', '0' * 64, backup.ValidationCode.CHECKSUM_MISMATCH),
    ('recognized_sequence', None, backup.ValidationCode.INVALID_MANIFEST),
])
def test_manifest_database_disagreement(current, field, value, code):
    manifest = asdict(manifest_for(current))
    manifest[field] = value
    assert validate(package(current.read_bytes(), manifest)).code is code


@pytest.mark.parametrize('change', ['version', 'history'])
def test_inspection_metadata_must_match(current, change):
    manifest = asdict(manifest_for(current))
    if change == 'version':
        manifest['recognized_sequence'] = 5
        # Use the registry's actual ID, without copying compatibility rules.
        manifest['recognized_migration_id'] = db.MIGRATIONS[4].migration_id
    else:
        manifest['history'][0]['applied_at'] = 'a false timestamp'
    assert validate(package(current.read_bytes(), manifest)).code is backup.ValidationCode.METADATA_MISMATCH


@pytest.mark.parametrize('field,value', [
    ('backup_format_version', True), ('application', 'foreign'), ('scope', 'everything'),
    ('wamf_version', 1), ('sqlite_version', ''), ('source_database', '/absolute/db'),
    ('source_database', '../db'), ('source_database', r'C:\db'), ('source_database', '..'),
    ('source_database', 'secret\x00.db'), ('database_member', '../database.db'),
    ('started_at', '2026-10-24'), ('started_at', '2026-10-24T12:00:00+01:00'),
    ('started_at', '2026-02-30T12:00:00Z'), ('completed_at', '2020-01-01T00:00:00Z'),
    ('database_size', True), ('database_size', -1), ('database_size', 1024 ** 3 + 1),
    ('database_sha256', 'z' * 64), ('database_sha256', 'a' * 63),
    ('recognized_sequence', True), ('recognized_sequence', 0),
    ('recognized_migration_id', '007_unrelated'), ('history', {}),
    ('history', [{'migration_id': 'bad', 'applied_at': 'time'}]),
    ('history', [{'migration_id': '001_initial_tables', 'applied_at': None}]),
])
def test_strict_manifest_fields(current, field, value):
    manifest = asdict(manifest_for(current))
    manifest[field] = value
    with pytest.raises(backup.PackageValidationError) as error:
        backup.BackupManifest.from_bytes(json.dumps(manifest).encode())
    assert error.value.code is backup.ValidationCode.INVALID_MANIFEST


@pytest.mark.parametrize('version', [0, 2, 999])
def test_unsupported_format(current, version):
    manifest = asdict(manifest_for(current))
    manifest['backup_format_version'] = version
    assert validate(package(current.read_bytes(), manifest)).code is backup.ValidationCode.UNSUPPORTED_FORMAT


@pytest.mark.parametrize('payload', [b'{', b'[]', b'null', b'\xff', b'{"a":1,"a":2}',
                                     b'{"a":NaN}', b'[' * 2000 + b']' * 2000])
def test_malformed_json(current, payload):
    assert validate(package(current.read_bytes(), payload)).code is backup.ValidationCode.INVALID_MANIFEST


@pytest.mark.parametrize('change', ['missing', 'extra', 'duplicate_history', 'extra_history_field'])
def test_exact_manifest_shape(current, change):
    manifest = asdict(manifest_for(current))
    if change == 'missing':
        del manifest['scope']
    elif change == 'extra':
        manifest['absolute_source_path'] = '/untrusted'
    elif change == 'duplicate_history':
        manifest['history'] += (manifest['history'][0],)
    else:
        manifest['history'][0]['extra'] = True
    assert validate(package(current.read_bytes(), manifest)).code is backup.ValidationCode.INVALID_MANIFEST


@pytest.mark.parametrize('name', ['/database.db', '../database.db', 'dir/../database.db',
                                  r'..\database.db', 'C:/database.db', 'database.db/',
                                  './database.db', 'DATABASE.DB'])
def test_member_name_tricks(current, name):
    with (storage.reserve_backup() as (identity, file), zipfile.ZipFile(file, 'w') as archive):
        archive.writestr(name, current.read_bytes())
        archive.writestr('manifest.json', manifest_for(current).to_bytes())
    assert validate(identity).code is backup.ValidationCode.INVALID_MEMBERS


@pytest.mark.parametrize('members', [[('extra', b'')], [('database.db', b'')], [('manifest.json', b'')]])
def test_extra_and_duplicate_members(current, members):
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter('always')
        identity = package(current.read_bytes(), manifest_for(current), members=members)
    if members[0][0] != 'extra':
        assert any('Duplicate name' in str(w.message) for w in caught)
    assert validate(identity).code is backup.ValidationCode.INVALID_MEMBERS


def test_two_duplicate_members(current):
    with (storage.reserve_backup() as (identity, file), zipfile.ZipFile(file, 'w') as archive):
        archive.writestr('database.db', b'a')
        with pytest.warns(UserWarning, match='Duplicate name'):
            archive.writestr('database.db', b'b')
    assert validate(identity).code is backup.ValidationCode.INVALID_MEMBERS


@pytest.mark.parametrize('compression', [zipfile.ZIP_DEFLATED, zipfile.ZIP_BZIP2, zipfile.ZIP_LZMA])
def test_compression_rejected(current, compression):
    identity = package(current.read_bytes(), manifest_for(current), compression=compression)
    assert validate(identity).code is backup.ValidationCode.UNSUPPORTED_ZIP


@pytest.mark.parametrize('kind', ['encrypted', 'local_encrypted', 'symlink', 'fifo', 'dos_directory', 'nul_name',
                                  'prepended', 'trailing', 'truncated', 'false_size', 'crc'])
def test_malicious_zip_headers(current, kind):
    identity = package(current.read_bytes(), manifest_for(current))
    path = artifact_path(identity)
    data = bytearray(path.read_bytes())
    central = data.index(b'PK\x01\x02')
    if kind in ('encrypted', 'local_encrypted'):
        struct.pack_into('<H', data, 6, 1)
        if kind == 'encrypted':
            struct.pack_into('<H', data, central + 8, 1)
    elif kind in ('symlink', 'fifo'):
        mode = stat.S_IFLNK if kind == 'symlink' else stat.S_IFIFO
        struct.pack_into('<L', data, central + 38, (mode | 0o600) << 16)
    elif kind == 'dos_directory':
        struct.pack_into('<L', data, central + 38, 0x10)
    elif kind == 'nul_name':
        data[central + 46 + 8] = 0
    elif kind == 'prepended':
        data = b'not a normal ZIP' + data
    elif kind == 'trailing':
        data += b'untrusted trailing payload'
    elif kind == 'truncated':
        del data[-2:]
    elif kind == 'false_size':
        struct.pack_into('<L', data, central + 24, 1)
    else:
        data[50] ^= 1
    path.write_bytes(data)
    assert not validate(identity).valid


def test_whole_package_limit(current, monkeypatch):
    identity = package(current.read_bytes(), manifest_for(current))
    monkeypatch.setattr(backup, 'MAX_PACKAGE_BYTES', 20)
    assert validate(identity).code is backup.ValidationCode.PACKAGE_TOO_LARGE


def test_manifest_limit(current):
    identity = package(current.read_bytes(), b' ' * (backup.MAX_MANIFEST_BYTES + 1))
    assert validate(identity).code is backup.ValidationCode.MANIFEST_TOO_LARGE


def test_database_limit(current, monkeypatch):
    identity = package(current.read_bytes(), manifest_for(current))
    monkeypatch.setattr(backup, 'MAX_DATABASE_BYTES', len(current.read_bytes()) - 1)
    assert validate(identity).code is backup.ValidationCode.DATABASE_TOO_LARGE


@pytest.mark.parametrize('destination', [False, True])
def test_actual_stream_limit_independent_of_headers(destination):
    output = io.BytesIO() if destination else None
    with pytest.raises(backup.PackageValidationError) as error:
        backup._read_limited(io.BytesIO(b'x' * 1025), 1024,
                             backup.ValidationCode.DATABASE_TOO_LARGE, output)
    assert error.value.code is backup.ValidationCode.DATABASE_TOO_LARGE
    if output:
        assert len(output.getvalue()) <= 1024


def test_full_integrity_failure_cleans_workspace(current, monkeypatch):
    identity = package(current.read_bytes(), manifest_for(current))
    monkeypatch.setattr(backup, 'check_staged_integrity', lambda path: False)
    result = validate(identity)
    assert result.code is backup.ValidationCode.INTEGRITY_FAILED
    assert result.inspection.integrity_checked and not result.full_integrity_checked
    assert list(wamf_paths.get_database_backup_staging_root().iterdir()) == [artifact_path(identity)]


def test_extraction_and_directories_are_private(current, monkeypatch):
    identity = package(current.read_bytes(), manifest_for(current))
    original = backup.inspect_database

    def inspect(path, **kwargs):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
        return original(path, **kwargs)

    monkeypatch.setattr(backup, 'inspect_database', inspect)
    assert validate(identity).valid
    root = wamf_paths.get_database_backup_root()
    for directory in (root.parent, root, root / '.staging'):
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert stat.S_IMODE(artifact_path(identity).stat().st_mode) == 0o600


def test_paths_are_fixed_and_inspection_has_no_side_effects(tmp_path):
    assert wamf_paths.get_database_backup_root() == tmp_path / 'data/backups/database'
    assert wamf_paths.get_database_backup_staging_root() == tmp_path / 'data/backups/database/.staging'
    assert wamf_paths.get_database_restore_root() == tmp_path / 'data/backups/database/.restore'
    assert not (tmp_path / 'data').exists()


@pytest.mark.parametrize('component', ['backups', 'backups/database', 'backups/database/.staging'])
def test_symlinked_storage_descendants_rejected(tmp_path, component):
    target = tmp_path / 'outside'
    target.mkdir()
    path = tmp_path / 'data' / component
    path.parent.mkdir(parents=True)
    path.symlink_to(target, target_is_directory=True)
    with pytest.raises(wamf_paths.DatabaseBackupPathError):
        wamf_paths.get_database_backup_staging_root()
    with pytest.raises(storage.BackupStorageError), storage.reserve_backup():
        pytest.fail('Must not reserve through a symlink')
    assert list(target.iterdir()) == []


@pytest.mark.parametrize('kind', ['symlink', 'hardlink', 'fifo', 'directory'])
def test_artifact_aliases_and_special_files_rejected(current, tmp_path, kind):
    identity = package(current.read_bytes(), manifest_for(current))
    path = artifact_path(identity)
    original = path.read_bytes()
    path.unlink()
    outside = tmp_path / 'outside.zip'
    outside.write_bytes(original)
    if kind == 'symlink':
        path.symlink_to(outside)
    elif kind == 'hardlink':
        os.link(outside, path)
    elif kind == 'fifo':
        os.mkfifo(path, 0o600)
    else:
        path.mkdir()
    assert validate(identity).code is backup.ValidationCode.UNSAFE_STORAGE
    assert outside.read_bytes() == original


@pytest.mark.parametrize('value', ['../escape', '/tmp/path', 'normal.zip', 'wamf-db-x',
                                   'wamf-db-20260230T120000000000Z-' + 'a' * 32, None, 42])
def test_unsafe_identifiers_rejected(value):
    with pytest.raises(storage.BackupStorageError):
        storage.BackupIdentity(value)


def test_collision_retries_without_overwrite(monkeypatch):
    first, second = storage.BackupIdentity.generate(), storage.BackupIdentity.generate()
    identities = iter([first, first, second])
    monkeypatch.setattr(storage.BackupIdentity, 'generate', lambda: next(identities))
    with storage.reserve_backup() as (reserved, file):
        assert reserved == first
        file.write(b'preserve')
    with storage.reserve_backup() as (reserved, file):
        assert reserved == second
        file.write(b'second')
    assert artifact_path(first).read_bytes() == b'preserve'
    monkeypatch.setattr(storage.BackupIdentity, 'generate', lambda: first)
    with pytest.raises(storage.BackupStorageError, match='collision'), storage.reserve_backup():
        pytest.fail('Must not overwrite')
    assert artifact_path(first).read_bytes() == b'preserve'


def test_reservation_failure_removes_only_its_file():
    with (pytest.raises(RuntimeError, match='failed'),
          storage.reserve_backup() as (identity, file)):
        file.write(b'incomplete')
        raise RuntimeError('failed')
    assert not artifact_path(identity).exists()


def test_missing_storage_validation_does_not_create_directories(tmp_path):
    result = validate(storage.BackupIdentity.generate())
    assert result.code is backup.ValidationCode.UNSAFE_STORAGE
    assert not (tmp_path / 'data').exists()


def test_invalid_registry_is_not_disguised_as_bad_upload(current, monkeypatch):
    identity = package(current.read_bytes(), manifest_for(current))

    def invalid_registry(*args, **kwargs):
        raise db.SchemaMigrationError('Invalid build registry')

    monkeypatch.setattr(backup, 'inspect_database', invalid_registry)
    with pytest.raises(db.SchemaMigrationError, match='registry'):
        validate(identity)
    assert list(wamf_paths.get_database_backup_staging_root().iterdir()) == [artifact_path(identity)]


@pytest.mark.parametrize('kind', ['file_permissions', 'directory_permissions'])
def test_nonprivate_storage_rejected(current, kind):
    identity = package(current.read_bytes(), manifest_for(current))
    path = artifact_path(identity)
    (path if kind == 'file_permissions' else path.parent).chmod(0o755)
    assert validate(identity).code is backup.ValidationCode.UNSAFE_STORAGE


def test_failure_cleanup_does_not_unlink_replacement():
    with (pytest.raises(RuntimeError, match='failed'),
          storage.reserve_backup() as (identity, _file)):
        path = artifact_path(identity)
        path.unlink()
        path.write_bytes(b'replacement belongs to somebody else')
        raise RuntimeError('failed')
    assert path.read_bytes() == b'replacement belongs to somebody else'
