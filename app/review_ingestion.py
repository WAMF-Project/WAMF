"""Bounded payload validation and short, transaction-owned metadata ingestion."""

import json
import math
import re
from contextlib import contextmanager

from app import review_repository as repository
from app.db import connect_db
from app.terminal_evidence import evidence_hint, ready, remember_hint, terminal_complete

MAX_PAYLOAD_BYTES = 1024 * 1024
MAX_MEMBERS = 4096


class ReviewInputError(ValueError):
    """A malformed payload, with no remote text in its diagnostic."""


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,200}', value):
        raise ReviewInputError('invalid_identifier')
    return value


def timestamp(value):
    try:
        if type(value) in (int, float) and math.isfinite(value) and value >= 0:
            return value
    except OverflowError:
        pass
    raise ReviewInputError('invalid_timestamp')


def decode_payload(payload):
    if not isinstance(payload, (bytes, str)) or len(payload) > MAX_PAYLOAD_BYTES:
        raise ReviewInputError('invalid_payload')
    try:
        value = json.loads(payload)
    except (ValueError, UnicodeError, RecursionError):
        raise ReviewInputError('invalid_payload') from None
    if not isinstance(value, dict) or value.get('type') not in ('new', 'update', 'end'):
        raise ReviewInputError('invalid_lifecycle')
    if not isinstance(value.get('after'), dict):
        raise ReviewInputError('invalid_payload')
    return value


def normalize_review(value, *, ended=False):
    if not isinstance(value, dict) or not isinstance(value.get('data'), dict):
        raise ReviewInputError('invalid_review')
    start = timestamp(value.get('start_time'))
    end = value.get('end_time')
    if end is not None:
        end = timestamp(end)
        if end < start:
            raise ReviewInputError('invalid_interval')
    if ended and end is None:
        raise ReviewInputError('missing_end')
    severity = value.get('severity')
    if severity is not None and severity not in ('alert', 'detection'):
        raise ReviewInputError('invalid_severity')
    data = value['data']
    members = data.get('detections')
    if not isinstance(members, list) or len(members) > MAX_MEMBERS:
        raise ReviewInputError('invalid_membership')
    members = tuple(dict.fromkeys(identifier(item) for item in members))
    # Context is deliberately not persisted or used as independent evidence.
    for field in ('objects', 'verified_objects', 'sub_labels', 'zones'):
        items = data.get(field, [])
        if not isinstance(items, list) or len(items) > MAX_MEMBERS:
            raise ReviewInputError('invalid_context')
        for item in items:
            if not isinstance(item, str) or len(item) > 256 or not item.isprintable():
                raise ReviewInputError('invalid_context')
    return {'review_id': identifier(value.get('id')), 'camera_name': identifier(value.get('camera')),
            'start_time': start, 'end_time': end, 'severity': severity, 'members': members}


def normalize_event(value, *, ended=False):
    if not isinstance(value, dict):
        raise ReviewInputError('invalid_event')
    start = timestamp(value.get('start_time'))
    end = value.get('end_time')
    if end is not None:
        end = timestamp(end)
        if end < start:
            raise ReviewInputError('invalid_interval')
    if ended and end is None:
        raise ReviewInputError('missing_end')
    return {'event_id': identifier(value.get('id')), 'camera_name': identifier(value.get('camera')),
            'label': identifier(value.get('label')), 'start_time': start, 'end_time': end}


@contextmanager
def metadata_connection(path, *, callback=False):
    conn = connect_db(path, row_factory=False)
    try:
        conn.execute(f'PRAGMA busy_timeout={50 if callback else 250}')
        conn.execute('BEGIN IMMEDIATE')
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def apply_review(conn, review, *, now, from_api=False):
    """Merge valid facts, preserving conflicting membership for diagnosis.

    Returns a fixed diagnostic code or None. Never finalizes observations.
    """
    values = {key: value for key, value in review.items() if key != 'members'}
    old = repository.get_review(conn, review['review_id'])
    if old and old['camera_name'] != review['camera_name']:
        _review_issue(conn, old['review_id'], 'camera_mismatch')
        return 'camera_mismatch'
    current = repository.upsert_review(conn, **values)
    changed = old != current
    conflict = False
    for event_id in review['members']:
        event = repository.get_event(conn, event_id)
        if event and (event['camera_name'] != review['camera_name']
                      or event['review_id'] not in (None, review['review_id'])):
            conflict = True
            # IDs are bounded validated identifiers, never rendered in normal health UI.
            conn.execute('UPDATE frigate_review_events SET last_error=? WHERE event_id=?',
                         (f'membership_conflict:{review["review_id"]}', event_id))
            if event['review_id']:
                _review_issue(conn, event['review_id'], 'membership_conflict')
            continue
        if current['state'] == 'finalized' and (not event or event['review_id'] is None):
            conflict = True
            continue
        changed |= event is None or event['review_id'] is None
        member = repository.upsert_event(conn, event_id, camera_name=review['camera_name'],
                                         review_id=review['review_id'])
        if ready(member) and not terminal_complete(member) and (event is None or not ready(event)):
            repository.set_event_retry(conn, event_id, retry_count=0)
    if conflict:
        _review_issue(conn, review['review_id'], 'membership_conflict')
        return 'membership_conflict'
    # An ordinary replay must not silently clear a previously diagnosed conflict.
    if current['last_error'] in ('membership_conflict', 'camera_mismatch'):
        return current['last_error']
    if from_api or changed:
        state = current['state']
        if state in ('deferred', 'incomplete'):
            state = 'closing' if current['end_time'] is not None else 'active'
        repository.set_review_state(
            conn, review['review_id'], state,
            reconciliation_status='synced' if from_api else 'pending',
            next_retry_at=(now + 60 if current['end_time'] is None else None) if from_api else now,
        )
    return None


def _review_issue(conn, review_id, code):
    conn.execute('''UPDATE frigate_reviews SET reconciliation_status='incomplete',
        last_error=?, next_retry_at=NULL, updated_at=datetime('now') WHERE review_id=?''',
                 (code, review_id))


def ingest_review(path, payload, cameras, *, now):
    envelope = decode_payload(payload)
    review = normalize_review(envelope['after'], ended=envelope['type'] == 'end')
    if review['camera_name'] not in cameras:
        return False, None
    with metadata_connection(path, callback=True) as conn:
        issue = apply_review(conn, review, now=now)
    return True, issue


def ingest_event(path, payload, cameras):
    envelope = decode_payload(payload)
    event = normalize_event(envelope['after'], ended=envelope['type'] == 'end')
    if event['camera_name'] not in cameras or event['label'] != 'bird':
        return False
    with metadata_connection(path, callback=True) as conn:
        old = repository.get_event(conn, event['event_id'])
        row = repository.upsert_event(conn, **event)
        remember_hint(conn, event['event_id'], evidence_hint(envelope['after'], ended=envelope['type'] == 'end'))
        if ready(row) and not terminal_complete(row) and (old is None or not ready(old)):
            repository.set_event_retry(conn, event['event_id'], retry_count=0)
    return True
