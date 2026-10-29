"""Species occurrences from settled review evidence, never individual counts."""

import logging
import math
import sqlite3
from datetime import datetime

from app import review_repository as repository
from app.archive_media import unreferenced_archive_paths
from app.db import connect_db
from app.media_coordination import media_activity_guard
from app.review_ingestion import metadata_connection
from app.terminal_evidence import terminal_complete

logger = logging.getLogger(__name__)
AGGREGATION_RULE = 'review-species-v1'


def species_key(name):
    return ' '.join(name.split()).casefold() if isinstance(name, str) else ''


def aggregation_plan(conn, review_id):
    """None means unresolved. Empty groups is a valid zero-observation review."""
    review = repository.get_review(conn, review_id)
    if (not review or review['state'] == 'finalized' or review['end_time'] is None
            or review['reconciliation_status'] != 'synced'
            or review['last_error'] in ('membership_conflict', 'camera_mismatch')):
        return None
    members = repository.list_review_members(conn, review_id)
    groups = {}
    for row in members:
        if (row['camera_name'] != review['camera_name'] or row['label'] is None
                or row['start_time'] is None or row['end_time'] is None
                or not terminal_complete(row)
                or (row['last_error'] or '').startswith('membership_conflict')):
            return None
        if row['label'] != 'bird' or row['classification_state'] != 'classified':
            continue
        name, score = species_key(row['scientific_name']), row['score']
        if not name or name in ('unknown', 'bird') or score is None or not math.isfinite(score) or not 0 <= score <= 1:
            raise ValueError('Invalid accepted constituent evidence')
        groups.setdefault(name, []).append(row)
    suppressed = {species_key(row[0]) for row in conn.execute(
        'SELECT scientific_name FROM frigate_review_suppressions WHERE review_id=?', (review_id,))}
    plan = []
    for key, rows in sorted(groups.items()):
        if key in suppressed:
            continue
        # Highest confidence; known/latest snapshot; earliest start; lexical ID.
        representative = min(rows, key=lambda row: (-row['score'],
            -(row['snapshot_time'] if row['snapshot_time'] is not None else -1),
            row['start_time'], row['event_id']))
        # Preserve a canonical spelling, independent of arrival order/confidence.
        name = min(' '.join(row['scientific_name'].split()) for row in rows)
        plan.append({'name': name, 'representative': representative, 'members': rows,
                     'start': datetime.fromtimestamp(min(row['start_time'] for row in rows)).isoformat(' '),
                     'end': datetime.fromtimestamp(max(row['end_time'] for row in rows)).isoformat(' ')})
    return plan


def finalize_review(conn, review_id, *, media=None):
    """Caller owns transaction/media guard. All species and links commit together."""
    plan = aggregation_plan(conn, review_id)
    if plan is None:
        return []
    repository.set_review_state(conn, review_id, 'closing', reconciliation_status='synced')
    created = []
    suppressed = {species_key(row[0]) for row in conn.execute(
        'SELECT scientific_name FROM frigate_review_suppressions WHERE review_id=?', (review_id,))}
    expected = {species_key(group['name']) for group in plan}
    for row in conn.execute('SELECT id,display_name FROM detections WHERE frigate_review_id=?', (review_id,)).fetchall():
        key = species_key(row[1])
        if key in suppressed:
            conn.execute('UPDATE frigate_review_events SET observation_id=NULL WHERE observation_id=?', (row[0],))
            conn.execute('DELETE FROM detections WHERE id=?', (row[0],))
        elif key not in expected:
            raise ValueError('Existing grouped species does not match settled evidence')
    for group in plan:
        representative = group['representative']
        existing = conn.execute('SELECT id, display_name FROM detections WHERE frigate_review_id=?',
                                (review_id,)).fetchall()
        matching = [row for row in existing if species_key(row[1]) == species_key(group['name'])]
        if len(matching) > 1:
            raise ValueError('Conflicting existing grouped observations')
        if matching:
            identity = matching[0][0]
            old = conn.execute('''SELECT frigate_event,camera_name,wamf_snapshot_path,wamf_clip_path
                FROM detections WHERE id=?''', (identity,)).fetchone()
            if (old[1] != representative['camera_name'] or
                    (old[0] != representative['event_id'] and any(old[2:]))):
                raise ValueError('Existing representative media does not match settled evidence')
            if not any(old[2:]):
                conn.execute('UPDATE detections SET wamf_snapshot_path=?,wamf_clip_path=? WHERE id=?',
                             (*(media or {}).get(representative['event_id'], (None, None)), identity))
            conn.execute('''UPDATE detections SET display_name=?, frigate_event=?, score=?,
                detection_time=?, observation_end_time=?, support_count=?, aggregation_rule_version=?,
                detection_index=?, category_name=? WHERE id=?''',
                (group['name'], representative['event_id'], representative['score'], group['start'], group['end'],
                 len(group['members']), AGGREGATION_RULE, representative['detection_index'], representative['category_name'], identity))
        else:
            identity = repository.create_grouped_observation(
                conn, review_id=review_id, representative_event_id=representative['event_id'],
                scientific_name=group['name'], detection_time=group['start'], observation_end_time=group['end'],
                support_count=len(group['members']), aggregation_rule_version=AGGREGATION_RULE,
                score=representative['score'])
            paths = (media or {}).get(representative['event_id'], (None, None))
            conn.execute('UPDATE detections SET wamf_snapshot_path=?, wamf_clip_path=? WHERE id=?', (*paths, identity))
            created.append({**representative, 'scientific_name': group['name'], 'observation_id': identity})
        for member in group['members']:
            repository.link_event_to_observation(conn, member['event_id'], identity)
    # Replace only exact authoritative members, never historical time windows.
    conn.execute('''DELETE FROM detections WHERE frigate_review_id IS NULL AND frigate_event IN
        (SELECT event_id FROM frigate_review_events WHERE review_id=?)''', (review_id,))
    repository.set_review_state(conn, review_id, 'finalized', reconciliation_status='synced',
                                aggregation_rule_version=AGGREGATION_RULE)
    return created


class ReviewAggregator:
    """Owned by the review worker. No network inside the finalization transaction."""

    def __init__(self, archive=None, publish=None, cleanup=None):
        self.archive, self.publish, self.cleanup = archive, publish, cleanup

    def run_batch(self, path, cameras, stop, clock):
        if not cameras or stop.is_set():
            return
        with metadata_connection(path) as conn:
            identities = [row[0] for row in conn.execute('''SELECT review_id FROM frigate_reviews
                WHERE state != 'finalized' AND end_time IS NOT NULL AND reconciliation_status='synced'
                AND COALESCE(last_error,'') NOT IN ('membership_conflict','camera_mismatch')
                AND NOT EXISTS (SELECT 1 FROM frigate_review_events e WHERE e.review_id=frigate_reviews.review_id
                    AND (e.label IS NULL OR e.start_time IS NULL OR e.end_time IS NULL
                        OR e.classification_phase IS NULL OR e.classification_phase!='terminal'
                        OR e.classification_state='pending' OR e.last_error LIKE 'membership_conflict%'))
                AND (next_retry_at IS NULL OR next_retry_at<=?) ORDER BY start_time,review_id LIMIT 4''', (clock(),))]
        for identity in identities:
            if stop.is_set():
                return
            try:
                self._finalize(path, identity, cameras, stop)
            except (sqlite3.Error, OSError, ValueError, RuntimeError):
                logger.warning('Review aggregation failed; durable evidence retained')
                if not stop.is_set():
                    with metadata_connection(path) as conn:
                        review = repository.get_review(conn, identity)
                        if review['state'] != 'finalized':
                            count = review['retry_count'] + 1
                            repository.set_review_state(conn, identity, 'closing', retry_count=count,
                                next_retry_at=clock() + min(5 * 2 ** min(count - 1, 6), 300),
                                last_error='aggregation_failed')

    def _finalize(self, path, identity, cameras, stop):
        with media_activity_guard(path):
            with metadata_connection(path) as conn:
                review = repository.get_review(conn, identity)
                plan = aggregation_plan(conn, identity) if review['camera_name'] in cameras else None
                transition = conn.execute('''SELECT 1 FROM detections WHERE frigate_review_id IS NULL
                    AND frigate_event IN (SELECT event_id FROM frigate_review_events WHERE review_id=?) LIMIT 1''',
                    (identity,)).fetchone() is not None
                old_media = conn.execute('''SELECT wamf_snapshot_path,wamf_clip_path FROM detections
                    WHERE frigate_review_id=? OR (frigate_review_id IS NULL AND frigate_event IN
                    (SELECT event_id FROM frigate_review_events WHERE review_id=?))''', (identity, identity)).fetchall()
                paths = {}
                if plan is not None:
                    for group in plan:
                        event_id = group['representative']['event_id']
                        paths[event_id] = conn.execute('''SELECT wamf_snapshot_path,wamf_clip_path FROM detections
                            WHERE frigate_event=? ORDER BY id LIMIT 1''', (event_id,)).fetchone()
            if plan is None:
                return
            acquired = []
            try:
                for event_id, existing in paths.items():
                    if stop.is_set():
                        return
                    if existing is None or not any(existing):
                        paths[event_id] = self.archive(event_id) if self.archive else (None, None)
                        acquired.append(paths[event_id])
                if stop.is_set():
                    return
                with metadata_connection(path) as conn:
                    # Compare the complete plan so a concurrent MQTT membership change
                    # cannot commit new species with unprepared representative media.
                    if aggregation_plan(conn, identity) != plan:
                        return
                    created = finalize_review(conn, identity, media=paths)
            finally:
                # Postcommit/rollback cleanup only; never unlink a referenced file.
                if self.cleanup:
                    conn = connect_db(path, row_factory=False)
                    try:
                        candidates = [stored for pair in [*old_media, *acquired] for stored in pair]
                        for stored in unreferenced_archive_paths(conn, candidates):
                            try:
                                self.cleanup(stored)
                            except OSError:
                                logger.warning('Unreferenced media cleanup deferred to retention')
                    finally:
                        conn.close()
        # Best effort at-most-once attempt, as with the existing commit-then-post
        # path. A crash after commit can lose publication; restart never replays it.
        if self.publish and not transition:
            for row in created:
                try:
                    self.publish(row)
                except (OSError, RuntimeError, ValueError):
                    logger.warning('Grouped observation publication failed after commit')
