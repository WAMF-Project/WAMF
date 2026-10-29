"""Versioned metadata hints and image provenance; no retained media ownership."""

import hashlib
import json
import math
from dataclasses import dataclass


def number(value):
    try:
        return value if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None
    except OverflowError:
        return None


def evidence_hint(payload, *, ended=False):
    """Only facts needed to identify/evaluate an event snapshot, never sublabels."""
    snapshot = payload.get('snapshot')
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    data = payload.get('data')
    data = data if isinstance(data, dict) else {}
    frame_time = number(snapshot.get('frame_time'))
    if frame_time is None:
        frame_time = number(data.get('snapshot_time'))
    if frame_time is None:
        frame_time = number(payload.get('snapshot_time'))
    false_positive = payload.get('false_positive')
    return {'version': 1, 'source': 'mqtt_hint', 'terminal': bool(ended),
            'frame_time': frame_time,
            'event_frame_time': number(payload.get('frame_time')),
            'false_positive': false_positive if type(false_positive) is bool else None}


def decode_revision(value):
    if not isinstance(value, str) or len(value) > 4096:
        return {}
    try:
        result = json.loads(value)
    except (ValueError, RecursionError):
        return {}
    return result if isinstance(result, dict) and result.get('version') == 1 else {}


def encode_revision(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def terminal_complete(row):
    return (row['classification_phase'] == 'terminal'
            and row['classification_state'] in ('classified', 'rejected', 'failed'))


def ready(row):
    return row['review_id'] is not None and row['label'] is not None and row['end_time'] is not None


def remember_hint(conn, event_id, hint):
    """A pending hint describes announced metadata, not an image already classified."""
    from app.review_repository import get_event

    row = get_event(conn, event_id)
    if row is None or terminal_complete(row) or row['classification_phase'] == 'provisional':
        return
    old = decode_revision(row['snapshot_revision'])
    if old and old.get('source') != 'mqtt_hint':
        return
    if old.get('terminal') and not hint['terminal']:
        return
    old_order = number(old.get('event_frame_time'))
    new_order = number(hint.get('event_frame_time'))
    if old_order is None:
        old_order = number(old.get('frame_time'))
    if new_order is None:
        new_order = number(hint.get('frame_time'))
    if (old.get('terminal') == hint['terminal'] and old_order is not None
            and (new_order is None or new_order < old_order)):
        return
    merged = {**old, **{key: value for key, value in hint.items() if value is not None}}
    # Sparse end metadata must not clear the latest known snapshot timestamp.
    revision = encode_revision(merged)
    if revision != row['snapshot_revision']:
        conn.execute('''UPDATE frigate_review_events SET snapshot_revision=?, snapshot_time=?,
            updated_at=datetime('now') WHERE event_id=?''', (revision, merged.get('frame_time'), event_id))


@dataclass(frozen=True)
class TerminalImage:
    content: bytes
    frame_time: float | None
    source: str
    metadata_source: str

    def revision(self, event_id):
        return encode_revision({'version': 1, 'source': self.source, 'event_id': event_id,
                                'frame_time': self.frame_time, 'metadata_source': self.metadata_source,
                                'crop': True, 'quality': 95,
                                'sha256': hashlib.sha256(self.content).hexdigest()})


class TerminalEvidenceError(Exception):
    def __init__(self, code, *, retryable=True, provenance=None):
        super().__init__(code)
        self.code, self.retryable = code, retryable
        self.provenance = provenance or {}


class IneligibleEvent(Exception):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason
