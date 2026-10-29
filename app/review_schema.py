"""Frozen 007 DDL. No runtime services or media ownership are established here."""

GROUPING_MIGRATION = '007_review_grouping_model'

# Kept separate from the frozen 006 schema and the public detection SELECT.
DETECTION_GROUPING_COLUMNS = {
    'frigate_review_id': 'TEXT',
    'observation_end_time': 'TEXT',
    'support_count': 'INTEGER CHECK (support_count IS NULL OR support_count > 0)',
    'aggregation_rule_version': 'TEXT',
}

REVIEW_TABLE_SCHEMAS = {
    'frigate_reviews': """
        CREATE TABLE frigate_reviews (
            review_id TEXT PRIMARY KEY NOT NULL,
            camera_name TEXT NOT NULL,
            start_time REAL NOT NULL,
            end_time REAL CHECK (end_time IS NULL OR end_time >= start_time),
            state TEXT NOT NULL DEFAULT 'active'
                CHECK (state IN ('active', 'closing', 'finalized', 'deferred', 'incomplete')),
            severity TEXT,
            reconciliation_status TEXT NOT NULL DEFAULT 'pending'
                CHECK (reconciliation_status IN ('pending', 'synced', 'deferred', 'incomplete')),
            retry_count INTEGER NOT NULL DEFAULT 0 CHECK (retry_count >= 0),
            next_retry_at REAL,
            last_error TEXT,
            aggregation_rule_version TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            CHECK (state NOT IN ('closing', 'finalized') OR end_time IS NOT NULL),
            CHECK (state != 'finalized' OR aggregation_rule_version IS NOT NULL)
        )
    """,
    'frigate_review_events': """
        CREATE TABLE frigate_review_events (
            event_id TEXT PRIMARY KEY NOT NULL,
            review_id TEXT,
            camera_name TEXT NOT NULL,
            label TEXT,
            start_time REAL,
            end_time REAL CHECK (end_time IS NULL OR end_time >= start_time),
            classification_state TEXT NOT NULL DEFAULT 'pending'
                CHECK (classification_state IN ('pending', 'classified', 'rejected', 'failed')),
            classification_phase TEXT
                CHECK (classification_phase IN ('provisional', 'terminal')),
            scientific_name TEXT,
            common_name TEXT,
            category_name TEXT,
            detection_index INTEGER,
            score REAL CHECK (score IS NULL OR (score >= 0 AND score <= 1)),
            reason TEXT,
            snapshot_revision TEXT,
            snapshot_time REAL,
            classifier_model TEXT,
            observation_id INTEGER,
            retry_count INTEGER NOT NULL DEFAULT 0 CHECK (retry_count >= 0),
            next_retry_at REAL,
            last_error TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            CHECK (classification_state = 'pending' OR classification_phase IS NOT NULL),
            CHECK (classification_state != 'classified' OR
                (scientific_name IS NOT NULL AND score IS NOT NULL)),
            CHECK (classification_state NOT IN ('rejected', 'failed') OR reason IS NOT NULL)
        )
    """,
    'frigate_review_suppressions': """
        CREATE TABLE frigate_review_suppressions (
            review_id TEXT NOT NULL,
            scientific_name TEXT NOT NULL,
            suppressed_at TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (review_id, scientific_name)
        )
    """,
}

REVIEW_INDEX_SCHEMAS = {
    'idx_detections_review_species': """
        CREATE UNIQUE INDEX idx_detections_review_species
        ON detections(frigate_review_id, display_name)
        WHERE frigate_review_id IS NOT NULL
    """,
    'idx_frigate_reviews_pending': """
        CREATE INDEX idx_frigate_reviews_pending ON frigate_reviews(state, next_retry_at)
    """,
    'idx_frigate_review_events_review': """
        CREATE INDEX idx_frigate_review_events_review ON frigate_review_events(review_id)
    """,
    'idx_frigate_review_events_pending': """
        CREATE INDEX idx_frigate_review_events_pending
        ON frigate_review_events(classification_state, next_retry_at)
    """,
    'idx_frigate_review_events_observation': """
        CREATE INDEX idx_frigate_review_events_observation ON frigate_review_events(observation_id)
    """,
}


def upgrade_review_grouping(conn):
    for name, definition in DETECTION_GROUPING_COLUMNS.items():
        conn.execute(f'ALTER TABLE detections ADD COLUMN {name} {definition}')
    for sql in REVIEW_TABLE_SCHEMAS.values():
        conn.execute(sql)
    for sql in REVIEW_INDEX_SCHEMAS.values():
        conn.execute(sql)


def detection_schema_007(baseline_sql):
    """Derive 007 while preserving the independently validated 006 definition."""
    suffix = ''.join(f', {name} {definition}'
                     for name, definition in DETECTION_GROUPING_COLUMNS.items())
    end = baseline_sql.rfind(')')
    return baseline_sql[:end] + suffix + baseline_sql[end:]
