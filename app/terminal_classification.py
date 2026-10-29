"""Terminal constituent evidence only: no detections, media files or publications."""

import math
from collections import OrderedDict

from app import review_repository as repository
from app.review_ingestion import metadata_connection
from app.review_reconciliation import BATCH_SIZE, retry_delay
from app.terminal_evidence import (
    IneligibleEvent,
    TerminalEvidenceError,
    decode_revision,
    terminal_complete,
)

MAX_ATTEMPTS = 6


class TerminalClassifier:
    def __init__(self, client, classify_snapshot, *, model_id, threshold, background_index,
                 common_name):
        self.client, self.classify_snapshot = client, classify_snapshot
        self.model_id, self.threshold, self.background_index = model_id, threshold, background_index
        self.common_name = common_name
        # Preserve computed results across transient DB failure, without keeping
        # images. A crash before commit can recompute; committed terminal rows
        # are never selected again. Only one supervisor per DB is supported.
        self._uncommitted = OrderedDict()

    def run_batch(self, path, cameras, stop, clock):
        if not cameras or stop.is_set():
            return
        with metadata_connection(path) as conn:
            slots = ','.join('?' for _ in cameras)
            cursor = conn.execute(f'''SELECT event_id FROM frigate_review_events
                WHERE review_id IS NOT NULL AND end_time IS NOT NULL AND label IS NOT NULL
                AND EXISTS (SELECT 1 FROM frigate_reviews AS review
                    WHERE review.review_id=frigate_review_events.review_id AND review.state != 'finalized')
                AND camera_name IN ({slots})
                AND (classification_phase IS NULL OR classification_phase != 'terminal'
                     OR classification_state = 'pending')
                AND COALESCE(last_error, '') NOT LIKE 'membership_conflict%'
                AND (next_retry_at IS NULL OR next_retry_at <= ?)
                ORDER BY COALESCE(next_retry_at, 0), end_time, event_id LIMIT ?''',
                                  (*cameras, clock(), BATCH_SIZE))
            identities = [row[0] for row in cursor.fetchall()]
        for identity in identities:
            if stop.is_set():
                return
            with metadata_connection(path) as conn:
                row = repository.get_event(conn, identity)
            if terminal_complete(row):
                self._uncommitted.pop(identity, None)
                continue
            outcome = self._uncommitted.get(identity)
            if outcome is None:
                try:
                    outcome = self.classify_event(row)
                except TerminalEvidenceError as error:
                    if stop.is_set():
                        return
                    self.record_failure(path, row, error, clock())
                    continue
                self._uncommitted[identity] = outcome
                while len(self._uncommitted) > BATCH_SIZE:
                    self._uncommitted.popitem(last=False)
            if stop.is_set():
                return
            # On DB failure the owner reports a safe scan error. Keep this
            # bounded outcome cache so the next scan need not invoke again.
            with metadata_connection(path) as conn:
                current = repository.get_event(conn, identity)
                if not terminal_complete(current):
                    repository.persist_classification(conn, identity, phase='terminal', **outcome)
            self._uncommitted.pop(identity, None)

    def classify_event(self, row):
        hint = decode_revision(row['snapshot_revision'])
        eligibility = ('non_bird' if row['label'] != 'bird' else
                       'false_positive' if hint.get('terminal') and hint.get('false_positive') is True else None)
        if eligibility:
            return {'state': 'rejected', 'reason': eligibility,
                    'snapshot_revision': row['snapshot_revision'], 'snapshot_time': row['snapshot_time']}
        if not self.model_id:
            raise TerminalEvidenceError('classifier_unavailable')
        try:
            evidence = self.client.acquire_terminal_image(row, allow_fallback=row['retry_count'] + 1 >= MAX_ATTEMPTS)
        except IneligibleEvent as error:
            return {'state': 'rejected', 'reason': error.reason,
                    'snapshot_revision': row['snapshot_revision'], 'snapshot_time': row['snapshot_time']}
        provenance = {'snapshot_revision': evidence.revision(row['event_id']),
                      'snapshot_time': evidence.frame_time, 'classifier_model': self.model_id}
        try:
            categories = self.classify_snapshot(evidence.content)
        except OSError:
            raise TerminalEvidenceError('invalid_snapshot', provenance=provenance) from None
        except (RuntimeError, ValueError):
            raise TerminalEvidenceError('classifier_execution_failed', provenance=provenance) from None
        if not categories:
            return {**provenance, 'state': 'rejected', 'reason': 'no_usable_classification'}
        category = categories[0]
        try:
            score = float(category.score)
        except (TypeError, ValueError):
            raise TerminalEvidenceError('invalid_classifier_score', retryable=False, provenance=provenance) from None
        if not math.isfinite(score) or not 0 <= score <= 1:
            raise TerminalEvidenceError('invalid_classifier_score', retryable=False, provenance=provenance)
        result = {**provenance, 'scientific_name': category.display_name,
                  'common_name': self.common_name(category.display_name) or category.display_name,
                  'category_name': category.category_name, 'detection_index': int(category.index),
                  'score': score}
        reason = ('background' if category.index == self.background_index else
                  'below_threshold' if score <= self.threshold else
                  'unresolved_species' if not category.display_name else None)
        if reason:
            return {**result, 'state': 'rejected', 'reason': reason}
        return {**result, 'state': 'classified',
                'reason': 'fallback_cached_snapshot' if evidence.source == 'cached_legacy_fallback' else None}

    def record_failure(self, path, row, error, now):
        with metadata_connection(path) as conn:
            current = repository.get_event(conn, row['event_id'])
            if terminal_complete(current):
                return
            count = current['retry_count'] + 1
            if not error.retryable or count >= MAX_ATTEMPTS:
                repository.persist_classification(
                    conn, row['event_id'], state='failed', phase='terminal', reason=error.code,
                    snapshot_revision=error.provenance.get('snapshot_revision', current['snapshot_revision']),
                    snapshot_time=error.provenance.get('snapshot_time', current['snapshot_time']),
                    classifier_model=self.model_id, retry_count=count, last_error=f'terminal:{error.code}',
                )
            else:
                repository.set_event_retry(conn, row['event_id'], retry_count=count,
                                           next_retry_at=now + retry_delay(count),
                                           last_error=f'terminal:{error.code}')
