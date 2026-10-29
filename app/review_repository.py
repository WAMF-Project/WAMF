"""Transaction-owned persistence for Frigate species occurrences.

No connection creation, commits, network calls, media writes or runtime hooks.
Mutations require an explicit caller transaction. Relationships are enforced here,
not by SQLite FKs: existing WAMF connections retain their current FK policy.
Raw SQL callers must maintain the same invariants. Deletion/revision workflows
must detach child observation links; suppression survives observation deletion.
"""

import math
from datetime import datetime
from enum import StrEnum

from app.terminal_evidence import terminal_complete


class ReviewState(StrEnum):
    ACTIVE = 'active'
    CLOSING = 'closing'
    FINALIZED = 'finalized'
    DEFERRED = 'deferred'
    INCOMPLETE = 'incomplete'


class ReconciliationStatus(StrEnum):
    PENDING = 'pending'
    SYNCED = 'synced'
    DEFERRED = 'deferred'
    INCOMPLETE = 'incomplete'


class ClassificationState(StrEnum):
    PENDING = 'pending'
    CLASSIFIED = 'classified'
    REJECTED = 'rejected'
    FAILED = 'failed'


class ClassificationPhase(StrEnum):
    PROVISIONAL = 'provisional'
    TERMINAL = 'terminal'


def _transaction(conn):
    if not conn.in_transaction:
        raise ValueError('Review mutations require a caller-owned transaction')


def _text(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError('Expected nonempty text')
    return value


def _time(value):
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise ValueError('Expected a finite nonnegative timestamp')
    return value


def _get(conn, table, key, identity):
    cursor = conn.execute(f'SELECT * FROM {table} WHERE {key} = ?', (identity,))
    row = cursor.fetchone()
    return dict(zip((item[0] for item in cursor.description), row)) if row is not None else None


def get_review(conn, review_id):
    return _get(conn, 'frigate_reviews', 'review_id', review_id)


def get_event(conn, event_id):
    return _get(conn, 'frigate_review_events', 'event_id', event_id)


def _required(value):
    if value is None:
        raise ValueError('Referenced review, event or observation does not exist')
    return value


def upsert_review(conn, review_id, *, camera_name, start_time, end_time=None, severity=None):
    """Merge lifecycle facts. An old open payload cannot clear an observed end."""
    _transaction(conn)
    for value in (review_id, camera_name):
        _text(value)
    _time(start_time)
    if end_time is not None and _time(end_time) < start_time:
        raise ValueError('Review ends before it starts')
    old = get_review(conn, review_id)
    if old and old['camera_name'] != camera_name:
        raise ValueError('Review camera mismatch')
    if old:
        merged_start = min(start_time, old['start_time'])
        merged_end = max(value for value in (end_time, old['end_time']) if value is not None) \
            if end_time is not None or old['end_time'] is not None else None
        state = old['state']
        if merged_end is not None and state == ReviewState.ACTIVE:
            state = ReviewState.CLOSING
        # Severity promotion is monotonic; older detection payloads cannot demote alerts.
        severity = 'alert' if 'alert' in (severity, old['severity']) else severity or old['severity']
        values = (merged_start, merged_end, state, severity)
        if values != tuple(old[key] for key in ('start_time', 'end_time', 'state', 'severity')):
            conn.execute('''UPDATE frigate_reviews SET start_time=?, end_time=?, state=?,
                severity=?, updated_at=datetime('now') WHERE review_id=?''', (*values, review_id))
    else:
        conn.execute('''INSERT INTO frigate_reviews
            (review_id, camera_name, start_time, end_time, state, severity)
            VALUES (?, ?, ?, ?, ?, ?)''',
                     (review_id, camera_name, start_time, end_time,
                      ReviewState.CLOSING if end_time is not None else ReviewState.ACTIVE, severity))
    return get_review(conn, review_id)


def set_review_state(conn, review_id, state, *, reconciliation_status=None,
                     retry_count=0, next_retry_at=None, last_error=None,
                     aggregation_rule_version=None):
    _transaction(conn)
    state = ReviewState(state)
    old = _required(get_review(conn, review_id))
    if old['state'] == ReviewState.FINALIZED and state != ReviewState.FINALIZED:
        raise ValueError('Finalized review requires an explicit future revision workflow')
    if old['end_time'] is not None and state == ReviewState.ACTIVE:
        raise ValueError('Ended review cannot become active')
    if state in (ReviewState.CLOSING, ReviewState.FINALIZED) and old['end_time'] is None:
        raise ValueError('Closing/finalized review requires an end time')
    rule = aggregation_rule_version or old['aggregation_rule_version']
    if state == ReviewState.FINALIZED:
        _text(rule)
        if old['state'] == ReviewState.FINALIZED and rule != old['aggregation_rule_version']:
            raise ValueError('Finalized rule requires an explicit future revision workflow')
    status = ReconciliationStatus(reconciliation_status or old['reconciliation_status'])
    _retry(retry_count, next_retry_at)
    values = (state, status, retry_count, next_retry_at, last_error, rule)
    fields = ('state', 'reconciliation_status', 'retry_count', 'next_retry_at', 'last_error',
              'aggregation_rule_version')
    if values == tuple(old[key] for key in fields):
        return
    conn.execute('''UPDATE frigate_reviews SET state=?, reconciliation_status=?, retry_count=?,
        next_retry_at=?, last_error=?, aggregation_rule_version=?, updated_at=datetime('now')
        WHERE review_id=?''', (*values, review_id))


def list_open_reviews(conn):
    cursor = conn.execute("SELECT review_id FROM frigate_reviews WHERE state != 'finalized' "
                          'ORDER BY start_time, review_id')
    return [get_review(conn, row[0]) for row in cursor.fetchall()]


def upsert_event(conn, event_id, *, camera_name, label=None, start_time=None,
                 end_time=None, review_id=None):
    """Persist membership stubs before tracked-object metadata arrives."""
    _transaction(conn)
    for value in (event_id, camera_name):
        _text(value)
    if label is not None:
        _text(label)
    if start_time is not None:
        _time(start_time)
    if end_time is not None:
        _time(end_time)
    old = get_event(conn, event_id)
    if old and (old['camera_name'] != camera_name or (
            old['label'] is not None and label is not None and old['label'] != label)):
        raise ValueError('Event camera/label mismatch')
    if review_id is not None:
        _validate_membership(conn, old, review_id, camera_name)
    starts = [value for value in (start_time, old['start_time'] if old else None) if value is not None]
    merged_start = min(starts) if starts else None
    ends = [value for value in (end_time, old['end_time'] if old else None) if value is not None]
    merged_end = max(ends) if ends else None
    if merged_start is not None and merged_end is not None and merged_end < merged_start:
        raise ValueError('Event ends before it starts')
    if old:
        values = (merged_start, merged_end, review_id or old['review_id'], label or old['label'])
        if values != tuple(old[key] for key in ('start_time', 'end_time', 'review_id', 'label')):
            conn.execute('''UPDATE frigate_review_events SET start_time=?, end_time=?, review_id=?,
                label=?, updated_at=datetime('now') WHERE event_id=?''', (*values, event_id))
    else:
        conn.execute('''INSERT INTO frigate_review_events
            (event_id, camera_name, label, start_time, end_time, review_id) VALUES (?, ?, ?, ?, ?, ?)''',
                     (event_id, camera_name, label, start_time, end_time, review_id))
    return get_event(conn, event_id)


def _validate_membership(conn, event, review_id, camera_name):
    review = _required(get_review(conn, review_id))
    if review['camera_name'] != camera_name:
        raise ValueError('Review/event camera mismatch')
    if event and event['review_id'] not in (None, review_id):
        raise ValueError('Event already belongs to another review')
    if review['state'] == ReviewState.FINALIZED and (not event or event['review_id'] != review_id):
        raise ValueError('Finalized membership requires an explicit future revision workflow')


def link_event_to_review(conn, event_id, review_id):
    _transaction(conn)
    event = _required(get_event(conn, event_id))
    _validate_membership(conn, event, review_id, event['camera_name'])
    if event['review_id'] != review_id:
        conn.execute("UPDATE frigate_review_events SET review_id=?, updated_at=datetime('now') "
                     'WHERE event_id=?', (review_id, event_id))


def list_review_members(conn, review_id):
    ids = conn.execute('SELECT event_id FROM frigate_review_events WHERE review_id=? '
                       'ORDER BY start_time, event_id', (review_id,)).fetchall()
    return [get_event(conn, row[0]) for row in ids]


def list_pending_events(conn):
    """Unfinished evidence; readiness and retry policy belong to callers."""
    ids = conn.execute("SELECT event_id FROM frigate_review_events "
                       "WHERE classification_state IN ('pending', 'failed') "
                       "OR classification_phase = 'provisional' "
                       'ORDER BY start_time, event_id').fetchall()
    return [get_event(conn, row[0]) for row in ids]


def _retry(count, next_at):
    if type(count) is not int or count < 0:
        raise ValueError('Invalid retry count')
    if next_at is not None:
        _time(next_at)


def set_event_retry(conn, event_id, *, retry_count, next_retry_at=None, last_error=None):
    """Schedule recovery without destroying an available provisional result."""
    _transaction(conn)
    event = _required(get_event(conn, event_id))
    _retry(retry_count, next_retry_at)
    values = (retry_count, next_retry_at, last_error)
    if values != tuple(event[key] for key in ('retry_count', 'next_retry_at', 'last_error')):
        conn.execute('''UPDATE frigate_review_events SET retry_count=?, next_retry_at=?,
            last_error=?, updated_at=datetime('now') WHERE event_id=?''', (*values, event_id))


def persist_classification(conn, event_id, *, state, phase, scientific_name=None,
                           common_name=None, category_name=None, detection_index=None,
                           score=None, reason=None, snapshot_revision=None, snapshot_time=None,
                           classifier_model=None, retry_count=0, next_retry_at=None, last_error=None):
    """Persist one authoritative result; terminal evidence supersedes provisional.

    Same-phase older snapshot timestamps are ignored. A snapshot revision has one
    immutable result; reclassification/model changes need a later revision workflow.
    Returns whether the evidence changed. Retry metadata is absolute, never incremented.
    """
    _transaction(conn)
    state, phase = ClassificationState(state), ClassificationPhase(phase)
    if state == ClassificationState.PENDING:
        raise ValueError('Pending is initial work, not a classification result')
    old = _required(get_event(conn, event_id))
    _retry(retry_count, next_retry_at)
    if snapshot_time is not None:
        _time(snapshot_time)
    if score is not None and (type(score) not in (int, float) or not math.isfinite(score)
                              or not 0 <= score <= 1):
        raise ValueError('Invalid confidence score')
    if state == ClassificationState.CLASSIFIED:
        if old['label'] is None or old['start_time'] is None:
            raise ValueError('Classified evidence requires hydrated event metadata')
        for value in (scientific_name, snapshot_revision, classifier_model):
            _text(value)
        if score is None:
            raise ValueError('Classified evidence requires a score')
    if state in (ClassificationState.REJECTED, ClassificationState.FAILED):
        _text(reason)
    fields = ('classification_state', 'classification_phase', 'scientific_name', 'common_name',
              'category_name', 'detection_index', 'score', 'reason', 'snapshot_revision',
              'snapshot_time', 'classifier_model', 'retry_count', 'next_retry_at', 'last_error')
    values = (state, phase, scientific_name, common_name, category_name, detection_index, score,
              reason, snapshot_revision, snapshot_time, classifier_model, retry_count,
              next_retry_at, last_error)
    if values == tuple(old[key] for key in fields):
        return False
    if old['observation_id'] is not None:
        raise ValueError('Linked evidence requires an explicit future revision workflow')
    if old['classification_phase'] == ClassificationPhase.TERMINAL and phase != ClassificationPhase.TERMINAL:
        return False
    if old['classification_phase'] == phase:
        if old['snapshot_time'] is not None and (
                snapshot_time is None or snapshot_time < old['snapshot_time']):
            return False
        if terminal_complete(old):
            raise ValueError('Terminal event already has a result; explicit revision required')
        if (old['classification_state'] in (ClassificationState.CLASSIFIED, ClassificationState.REJECTED)
                and old['snapshot_revision'] == snapshot_revision):
            raise ValueError('Snapshot revision already has a result')
    assignments = ', '.join(f'{name}=?' for name in fields)
    conn.execute(f"UPDATE frigate_review_events SET {assignments}, updated_at=datetime('now') "
                 'WHERE event_id=?', (*values, event_id))
    return True


def link_event_to_observation(conn, event_id, observation_id):
    _transaction(conn)
    event = _required(get_event(conn, event_id))
    observation = _required(_get(conn, 'detections', 'id', observation_id))
    if (event['review_id'] is None or event['review_id'] != observation['frigate_review_id']
            or event['camera_name'] != observation['camera_name']
            or event['classification_state'] != ClassificationState.CLASSIFIED
            or ' '.join(event['scientific_name'].split()).casefold() !=
            ' '.join(observation['display_name'].split()).casefold()):
        raise ValueError('Observation must match classified event review, camera and species')
    if event['observation_id'] not in (None, observation_id):
        raise ValueError('Event already linked to another observation')
    if event['observation_id'] != observation_id:
        conn.execute("UPDATE frigate_review_events SET observation_id=?, updated_at=datetime('now') "
                     'WHERE event_id=?', (observation_id, event_id))


def suppress_species(conn, review_id, scientific_name):
    """Durable deletion primitive; does not delete observations, evidence or media."""
    _transaction(conn)
    _required(get_review(conn, review_id))
    _text(scientific_name)
    conn.execute('''INSERT INTO frigate_review_suppressions (review_id, scientific_name)
        VALUES (?, ?) ON CONFLICT (review_id, scientific_name) DO NOTHING''',
                 (review_id, scientific_name))


def is_suppressed(conn, review_id, scientific_name):
    return conn.execute('''SELECT 1 FROM frigate_review_suppressions
        WHERE review_id=? AND scientific_name=?''', (review_id, scientific_name)).fetchone() is not None


def create_grouped_observation(conn, *, review_id, representative_event_id,
                               detection_time, observation_end_time, support_count,
                               aggregation_rule_version, score, scientific_name=None):
    """Insert once from a real classified member. Caller owns finalization/commit.

    This primitive does not select a species, aggregate scores, archive media,
    finalize a review, link all supporters or send notifications.
    """
    _transaction(conn)
    review = _required(get_review(conn, review_id))
    event = _required(get_event(conn, representative_event_id))
    if (event['review_id'] != review_id or event['classification_state'] != ClassificationState.CLASSIFIED
            or review['state'] != ReviewState.CLOSING):
        raise ValueError('Representative must be a classified member of a closing review')
    name = scientific_name or event['scientific_name']
    if ' '.join(name.split()).casefold() != ' '.join(event['scientific_name'].split()).casefold():
        raise ValueError('Representative species mismatch')
    if is_suppressed(conn, review_id, name):
        raise ValueError('Species observation is suppressed')
    _text(detection_time)
    start = datetime.fromisoformat(detection_time)
    if observation_end_time is not None and datetime.fromisoformat(observation_end_time) < start:
        raise ValueError('Observation ends before it starts')
    _text(aggregation_rule_version)
    if type(score) not in (int, float) or not math.isfinite(score) or not 0 <= score <= 1:
        raise ValueError('Invalid aggregate confidence score')
    if type(support_count) is not int or support_count < 1:
        raise ValueError('Invalid support count')
    existing = conn.execute('SELECT id FROM detections WHERE frigate_review_id=? AND display_name=?',
                            (review_id, name)).fetchone()
    if existing:
        return existing[0]
    cursor = conn.execute('''INSERT INTO detections
        (detection_time, detection_index, score, display_name, category_name, frigate_event,
         camera_name, frigate_review_id, observation_end_time, support_count, aggregation_rule_version)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                          (detection_time, event['detection_index'], score,
                           name, event['category_name'], representative_event_id,
                           event['camera_name'], review_id, observation_end_time, support_count,
                           aggregation_rule_version))
    return cursor.lastrowid
