from contextlib import contextmanager
from datetime import datetime, timezone
import json
import sqlite3

import pytest

from app.db import RETENTION_STATUS_COLUMNS, ensure_schema
from app.retention_service import RetentionPhaseResult, RetentionRunResult
from app.retention_state import (
    MAX_DIAGNOSTICS,
    MAX_DIAGNOSTIC_CHARS,
    MAX_DIAGNOSTICS_JSON_BYTES,
    RetentionStateRepository,
    deserialize_diagnostics,
    serialize_diagnostics,
)


def _legacy_database(path, rows=()):
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE retention_status (
            last_run TEXT,
            rows_scanned INTEGER,
            orphan_count INTEGER,
            missing_count INTEGER
        )
        """
    )
    conn.executemany(
        "INSERT INTO retention_status VALUES (?, ?, ?, ?)", rows
    )
    conn.commit()
    conn.close()


def _result(outcome="success", completed="2026-01-02T03:04:06+00:00"):
    has_error = outcome != "success"
    return RetentionRunResult(
        trigger="test-trigger",
        outcome=outcome,
        phases={
            "expired_media": RetentionPhaseResult(
                "expired_media", outcome="success", action="delete"
            ),
            "orphan_scan": RetentionPhaseResult(
                "orphan_scan",
                outcome="partial" if has_error else "success",
                action="report_only",
            ),
            "system_events": RetentionPhaseResult(
                "system_events", outcome="skipped"
            ),
        },
        rows_scanned=11,
        expired_snapshot_count=1,
        expired_clip_count=2,
        deleted_snapshot_count=3,
        deleted_clip_count=4,
        orphan_count=5,
        orphan_deletion_count=6,
        missing_reference_count=7,
        system_events_pruned=8,
        error_summaries=(
            ["orphan_scan: one controlled error (PermissionError)"]
            if has_error else []
        ),
        started_at=datetime.fromisoformat("2026-01-02T03:04:05+00:00"),
        completed_at=datetime.fromisoformat(completed),
        duration_ms=1234,
    )


def test_fresh_a2_schema_is_empty_constrained_and_marked(tmp_path):
    db_path = tmp_path / "fresh.db"

    ensure_schema(db_path)

    conn = sqlite3.connect(db_path)
    info = conn.execute("PRAGMA table_info(retention_status)").fetchall()
    marker = conn.execute(
        "SELECT 1 FROM schema_migrations WHERE version = '005_retention_status_v2'"
    ).fetchone()
    assert {row[1] for row in info} == RETENTION_STATUS_COLUMNS
    assert next(row for row in info if row[1] == "id")[5] == 1
    assert conn.execute("SELECT COUNT(*) FROM retention_status").fetchone()[0] == 0
    assert marker == (1,)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO retention_status (id, record_version) VALUES (2, 1)"
        )
    conn.close()


def test_empty_legacy_migration_and_repeat_are_idempotent(tmp_path):
    db_path = tmp_path / "legacy.db"
    _legacy_database(db_path)

    ensure_schema(db_path)
    ensure_schema(db_path)

    conn = sqlite3.connect(db_path)
    assert conn.execute("SELECT COUNT(*) FROM retention_status").fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM schema_migrations "
        "WHERE version = '005_retention_status_v2'"
    ).fetchone()[0] == 1
    conn.close()


def test_populated_legacy_migration_preserves_only_observed_fields(tmp_path):
    db_path = tmp_path / "legacy.db"
    _legacy_database(db_path, [("2025-05-06T07:08:09", 10, 3, 2)])

    ensure_schema(db_path)

    state = RetentionStateRepository(db_path).read()
    assert state["legacy_orphan_scan_at"] == "2025-05-06T07:08:09"
    assert state["rows_scanned"] == 10
    assert state["orphan_count"] == 3
    assert state["missing_reference_count"] == 2
    assert state["last_run"] == "2025-05-06T07:08:09"
    assert state["last_attempt_outcome"] is None
    assert state["last_success_completed_at"] is None
    assert state["expired_snapshot_count"] is None
    assert state["error_count"] is None


def test_multiple_legacy_rows_prefer_greatest_valid_iso_timestamp(tmp_path):
    db_path = tmp_path / "legacy.db"
    _legacy_database(
        db_path,
        [
            ("not-a-date", 99, 99, 99),
            (None, 88, 88, 88),
            ("2025-01-01T00:00:00+00:00", 1, 1, 1),
            ("2025-06-01T00:00:00Z", 6, 6, 6),
        ],
    )

    ensure_schema(db_path)

    state = RetentionStateRepository(db_path).read()
    assert state["legacy_orphan_scan_at"] == "2025-06-01T00:00:00Z"
    assert state["rows_scanned"] == 6


def test_only_malformed_or_null_legacy_rows_select_latest_rowid(tmp_path):
    db_path = tmp_path / "legacy.db"
    _legacy_database(
        db_path,
        [("malformed", 1, 1, 1), (None, 2, 2, 2)],
    )

    ensure_schema(db_path)

    state = RetentionStateRepository(db_path).read()
    assert state["legacy_orphan_scan_at"] is None
    assert state["rows_scanned"] == 2


def test_migration_failure_rolls_back_and_does_not_record_marker(
    tmp_path, monkeypatch
):
    from app import db

    db_path = tmp_path / "legacy.db"
    _legacy_database(db_path, [("2025-01-01T00:00:00", 1, 2, 3)])
    real_connect = db.connect_db

    class FailingConnection:
        def __init__(self, connection):
            self.connection = connection

        def __getattr__(self, name):
            return getattr(self.connection, name)

        def execute(self, sql, parameters=()):
            if sql == "DROP TABLE retention_status":
                raise sqlite3.OperationalError("controlled migration failure")
            return self.connection.execute(sql, parameters)

    monkeypatch.setattr(
        db,
        "connect_db",
        lambda *args, **kwargs: FailingConnection(
            real_connect(*args, **kwargs)
        ),
    )

    with pytest.raises(sqlite3.OperationalError, match="controlled"):
        ensure_schema(db_path)

    conn = sqlite3.connect(db_path)
    assert {row[1] for row in conn.execute("PRAGMA table_info(retention_status)")} == {
        "last_run", "rows_scanned", "orphan_count", "missing_count"
    }
    assert conn.execute("SELECT * FROM retention_status").fetchone() == (
        "2025-01-01T00:00:00", 1, 2, 3
    )
    assert conn.execute(
        "SELECT COUNT(*) FROM schema_migrations "
        "WHERE version = '005_retention_status_v2'"
    ).fetchone()[0] == 0
    conn.close()


def test_migration_failure_after_table_replacement_rolls_back_destructive_ddl(
    tmp_path, monkeypatch
):
    from app import db

    db_path = tmp_path / "legacy.db"
    original = ("2025-01-01T00:00:00", 11, 4, 3)
    _legacy_database(db_path, [original])
    real_connect = db.connect_db

    class FailAfterRenameConnection:
        def __init__(self, connection):
            self.connection = connection

        def __getattr__(self, name):
            return getattr(self.connection, name)

        def execute(self, sql, parameters=()):
            result = self.connection.execute(sql, parameters)
            if sql == "ALTER TABLE retention_status_a2 RENAME TO retention_status":
                raise sqlite3.OperationalError("failure after destructive replacement")
            return result

    monkeypatch.setattr(
        db,
        "connect_db",
        lambda *args, **kwargs: FailAfterRenameConnection(
            real_connect(*args, **kwargs)
        ),
    )

    with pytest.raises(sqlite3.OperationalError, match="after destructive"):
        ensure_schema(db_path)

    conn = sqlite3.connect(db_path)
    assert {row[1] for row in conn.execute("PRAGMA table_info(retention_status)")} == {
        "last_run", "rows_scanned", "orphan_count", "missing_count"
    }
    assert conn.execute("SELECT * FROM retention_status").fetchall() == [original]
    assert conn.execute(
        "SELECT COUNT(*) FROM schema_migrations "
        "WHERE version = '005_retention_status_v2'"
    ).fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM sqlite_master "
        "WHERE type = 'table' AND name = 'retention_status_a2'"
    ).fetchone()[0] == 0
    conn.close()


def test_repository_persists_full_snapshot_and_updates_last_success_atomically(tmp_path):
    db_path = tmp_path / "state.db"
    ensure_schema(db_path)
    repository = RetentionStateRepository(db_path)

    success = _result("success")
    repository.persist(success)
    first = repository.read()
    assert first["last_attempt_trigger"] == "test-trigger"
    assert first["last_attempt_duration_ms"] == 1234
    assert first["last_attempt_started_at"].endswith("+00:00")
    assert first["last_attempt_completed_at"].endswith("+00:00")
    assert first["last_success_completed_at"] == first["last_attempt_completed_at"]
    assert [first[name] for name in (
        "rows_scanned", "expired_snapshot_count", "expired_clip_count",
        "deleted_snapshot_count", "deleted_clip_count", "orphan_count",
        "orphan_deletion_count", "missing_reference_count",
        "system_events_pruned",
    )] == [11, 1, 2, 3, 4, 5, 6, 7, 8]
    assert first["expired_media_action"] == "delete"
    assert first["orphan_media_action"] == "report_only"
    assert first["expired_media_outcome"] == "success"
    assert first["orphan_scan_outcome"] == "success"
    assert first["system_events_outcome"] == "skipped"
    assert first["error_count"] == 0
    assert first["error_summaries"] == []

    for outcome, completed in (
        ("partial", "2026-01-03T03:04:06+00:00"),
        ("failed", "2026-01-04T03:04:06+00:00"),
    ):
        repository.persist(_result(outcome, completed))
        state = repository.read()
        assert state["last_attempt_outcome"] == outcome
        assert state["last_attempt_completed_at"] == completed
        assert state["last_success_completed_at"] == first["last_success_completed_at"]
        assert state["error_count"] == 1
        assert state["error_summaries"] == [
            "orphan_scan: one controlled error (PermissionError)"
        ]


def test_repository_rollback_leaves_previous_snapshot_unchanged(tmp_path, monkeypatch):
    from app import retention_state

    db_path = tmp_path / "state.db"
    ensure_schema(db_path)
    repository = RetentionStateRepository(db_path)
    repository.persist(_result("success"))
    before = repository.read()
    real_connect = retention_state.connect_db

    class FailCommit:
        def __init__(self, connection):
            self.connection = connection

        def __getattr__(self, name):
            return getattr(self.connection, name)

        def commit(self):
            raise sqlite3.OperationalError("controlled commit failure")

    monkeypatch.setattr(
        retention_state,
        "connect_db",
        lambda path: FailCommit(real_connect(path)),
    )

    with pytest.raises(sqlite3.OperationalError):
        repository.persist(_result("failed", "2026-02-01T00:00:00+00:00"))
    monkeypatch.setattr(retention_state, "connect_db", real_connect)
    assert repository.read() == before


def test_ensure_schema_preserves_existing_native_a2_row_exactly(tmp_path):
    db_path = tmp_path / "state.db"
    ensure_schema(db_path)
    RetentionStateRepository(db_path).persist(_result("success"))
    conn = sqlite3.connect(db_path)
    before = conn.execute(
        "SELECT * FROM retention_status WHERE id = 1"
    ).fetchone()
    conn.close()

    ensure_schema(db_path)

    conn = sqlite3.connect(db_path)
    after = conn.execute(
        "SELECT * FROM retention_status WHERE id = 1"
    ).fetchone()
    conn.close()
    assert after == before


@pytest.mark.parametrize(
    ("persisted", "expected"),
    [
        (None, []),
        ("not-json", []),
        ('{"message":"wrong top-level type"}', []),
        ('["first", 7, null, {"bad": true}, "second"]', ["first", "second"]),
        ('["\\ud800", "safe"]', ["?", "safe"]),
    ],
)
def test_repository_read_tolerates_corrupt_diagnostic_json_and_preserves_count(
    tmp_path, persisted, expected
):
    db_path = tmp_path / "state.db"
    ensure_schema(db_path)
    RetentionStateRepository(db_path).persist(_result("success"))
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        UPDATE retention_status
        SET error_count = 7, error_summaries_json = ?
        WHERE id = 1
        """,
        (persisted,),
    )
    conn.commit()
    conn.close()

    state = RetentionStateRepository(db_path).read()

    assert state["error_count"] == 7
    assert state["error_summaries"] == expected


@pytest.mark.parametrize(
    ("summary", "sensitive_values"),
    [
        (
            "request failed at https://user:pass@example.invalid/private?q=token",
            ("https://", "user:pass", "example.invalid", "q=token"),
        ),
        (
            "broker amqp://guest:guest@broker.invalid/private-vhost failed",
            ("amqp://", "guest:guest", "broker.invalid", "private-vhost"),
        ),
        (
            "Authorization: Bearer durable-token-value",
            ("Bearer", "durable-token-value"),
        ),
        (
            "client_secret='credential-value' password=hunter2 api_key=key-value",
            ("credential-value", "hunter2", "key-value"),
        ),
        (
            "failed to read /private/archive/bird.jpg",
            ("/private/archive/bird.jpg", "bird.jpg"),
        ),
        (
            r"failed to read C:\Users\BirdLab\private.txt",
            (r"C:\Users\BirdLab\private.txt", "private.txt"),
        ),
        (
            r"failed to read \\server\share\private.txt",
            (r"\\server\share\private.txt", "private.txt"),
        ),
    ],
)
def test_durable_diagnostics_redact_sensitive_forms(summary, sensitive_values):
    serialized = serialize_diagnostics([summary])

    for sensitive in sensitive_values:
        assert sensitive not in serialized


def test_durable_diagnostic_count_cap_keeps_exactly_ten_short_entries():
    raw = [f"controlled diagnostic {index}" for index in range(15)]

    diagnostics = json.loads(serialize_diagnostics(raw))

    assert diagnostics == raw[:MAX_DIAGNOSTICS]


def test_durable_diagnostic_multibyte_content_respects_final_utf8_byte_limit():
    serialized = serialize_diagnostics(["鳥" * 500 for _ in range(20)])
    diagnostics = json.loads(serialized)

    assert diagnostics
    assert len(serialized.encode("utf-8")) <= MAX_DIAGNOSTICS_JSON_BYTES
    assert all(len(item) <= MAX_DIAGNOSTIC_CHARS for item in diagnostics)


def test_durable_diagnostic_redaction_and_truncation_stay_within_final_byte_limit():
    raw = [
        "Authorization: Bearer " + ("秘密" * 300) + " /private/" + ("鳥" * 300)
        for _ in range(20)
    ]

    serialized = serialize_diagnostics(raw)
    diagnostics = deserialize_diagnostics(serialized)

    assert len(serialized.encode("utf-8")) <= MAX_DIAGNOSTICS_JSON_BYTES
    assert len(json.dumps(
        diagnostics, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")) <= MAX_DIAGNOSTICS_JSON_BYTES
    assert all(len(item) <= MAX_DIAGNOSTIC_CHARS for item in diagnostics)


def test_query_reader_keeps_compatibility_keys_and_exposes_native_state(
    tmp_path, monkeypatch
):
    from app import queries

    db_path = tmp_path / "state.db"
    ensure_schema(db_path)
    RetentionStateRepository(db_path).persist(_result("success"))
    monkeypatch.setattr(queries, "DBPATH", db_path)

    state = queries.get_retention_status()

    assert state["last_run"] == "02 Jan 2026 03:04"
    assert state["orphan_count"] == 5
    assert state["missing_count"] == 7
    assert state["last_attempt_outcome"] == "success"
    assert state["legacy_orphan_scan_at"] is None


def test_run_persists_actions_utc_timing_duration_and_all_skipped(tmp_path, monkeypatch):
    from app import retention_service
    from tests.test_retention_service import retention_config

    config = retention_config(
        tmp_path,
        enabled=False,
        orphan_scan_enabled=False,
    )
    wall = iter((
        datetime(2026, 2, 3, 4, 5, 6, tzinfo=timezone.utc),
        datetime(2026, 2, 3, 4, 5, 7, tzinfo=timezone.utc),
        datetime(2026, 2, 3, 4, 5, 9, tzinfo=timezone.utc),
    ))
    monotonic = iter((10.0, 20.0, 20.456))
    monkeypatch.setattr(retention_service, "_utc_now", lambda: next(wall))
    monkeypatch.setattr(retention_service, "_monotonic", lambda: next(monotonic))

    result = retention_service.run_retention("manual", config=config)

    state = RetentionStateRepository(config["storage"]["database_path"]).read()
    assert result.outcome == "success"
    assert {phase.outcome for phase in result.phases.values()} == {"skipped"}
    assert result.duration_ms == 455
    assert state["last_attempt_trigger"] == "manual"
    assert state["last_attempt_started_at"] == "2026-02-03T04:05:07+00:00"
    assert state["last_attempt_completed_at"] == "2026-02-03T04:05:09+00:00"
    assert state["last_attempt_duration_ms"] == 455
    assert state["expired_media_action"] == "disabled"
    assert state["orphan_media_action"] == "disabled"


def test_already_running_does_not_replace_state(tmp_path, monkeypatch):
    from app import retention_service
    from tests.test_retention_service import retention_config

    config = retention_config(tmp_path, enabled=False, orphan_scan_enabled=False)
    retention_service.run_retention("first", config=config)
    repository = RetentionStateRepository(config["storage"]["database_path"])
    before = repository.read()

    @contextmanager
    def rejected(_database_path):
        yield False

    monkeypatch.setattr(retention_service, "retention_execution_guard", rejected)
    result = retention_service.run_retention("second", config=config)

    assert result.outcome == "already_running"
    assert repository.read() == before


def test_final_persistence_failure_changes_only_whole_run_outcome(
    tmp_path, monkeypatch
):
    from app import retention_service
    from tests.test_retention_service import retention_config

    config = retention_config(tmp_path, enabled=False, orphan_scan_enabled=False)
    retention_service.run_retention("first", config=config)
    repository = RetentionStateRepository(config["storage"]["database_path"])
    before = repository.read()

    def fail_persist(self, result):
        raise sqlite3.OperationalError("controlled persistence failure")

    monkeypatch.setattr(
        retention_service.RetentionStateRepository, "persist", fail_persist
    )
    result = retention_service.run_retention("second", config=config)

    assert result.outcome == "partial"
    assert {phase.outcome for phase in result.phases.values()} == {"skipped"}
    assert result.error_count == 1
    assert "operational state" in result.error_summaries[0]
    assert repository.read() == before


def test_known_database_policy_failure_is_persisted_but_unknown_target_is_not(
    tmp_path, monkeypatch
):
    from app import retention_service
    from tests.test_retention_service import retention_config

    config = retention_config(tmp_path)
    config["retention"]["snapshots_days"] = None
    known = retention_service.run_retention("bad-config", config=config)
    state = RetentionStateRepository(config["storage"]["database_path"]).read()
    assert known.outcome == "failed"
    assert state["last_attempt_outcome"] == "failed"
    assert state["last_attempt_trigger"] == "bad-config"
    assert {state[name] for name in (
        "expired_media_outcome", "orphan_scan_outcome", "system_events_outcome"
    )} == {"skipped"}
    assert state["expired_media_action"] is None

    calls = []
    monkeypatch.setattr(
        retention_service.RetentionStateRepository,
        "persist",
        lambda self, result: calls.append(self.database_path),
    )
    unknown = retention_service.run_retention("no-target", config=[])
    assert unknown.outcome == "failed"
    assert calls == []
