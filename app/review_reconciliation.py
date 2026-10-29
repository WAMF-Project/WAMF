"""Owned review worker: bounded reconciliation, terminal evidence and aggregation."""

import logging
import sqlite3
import threading
import time
from dataclasses import replace

from app import review_repository as repository
from app.database_lifecycle import database_consumer
from app.db import connect_db
from app.frigate_client import FrigateReviewError
from app.review_ingestion import (
    ReviewInputError,
    apply_review,
    metadata_connection,
    normalize_event,
    normalize_review,
)
from app.review_runtime import ISSUE_CODES, ReviewRuntimeSnapshot
from app.terminal_evidence import ready, terminal_complete

logger = logging.getLogger(__name__)
BATCH_SIZE = 4
POLL_SECONDS = 5
MAX_RETRY_SECONDS = 300


def retry_delay(attempt):
    return min(5 * 2 ** min(max(attempt - 1, 0), 6), MAX_RETRY_SECONDS)


class ReviewReconciler:
    def __init__(self, path, cameras, client, runtime=None, *, clock=time.time, terminal_classifier=None, aggregator=None):
        self.path, self.cameras, self.client, self.runtime = path, tuple(cameras), client, runtime
        self.clock = clock
        self.stop = threading.Event()
        self.wakeup = threading.Event()
        self.thread = None
        self.next_discovery = 0
        self.camera_cursor = 0
        self.status = ReviewRuntimeSnapshot(worker_state='running')
        self.aggregator = aggregator
        self.terminal_classifier = terminal_classifier

    def __enter__(self):
        self.thread = threading.Thread(target=self.run, name='wamf-reviews', daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.stop.set()
        self.wakeup.set()
        self.thread.join(timeout=7)
        # A pathological blocked HTTP/DNS call cannot hold supervisor shutdown.
        # The process owns this daemon thread; no writes may start after stop.

    def wake(self):
        self.wakeup.set()  # One bit, not one queued job per MQTT delivery.

    def _publish(self):
        if self.runtime is not None:
            self.runtime.record_reviews(self.status)

    def run(self):
        try:
            while not self.stop.is_set():
                try:
                    # Hold #49 exclusion across API calls as well as DB work.
                    with database_consumer(self.path):
                        self.wakeup.clear()
                        self.scan()
                except (sqlite3.Error, OSError, RuntimeError, ValueError, TypeError,
                        AttributeError, LookupError, ArithmeticError):
                    self.status = replace(self.status, api_state='degraded', error_code='scan_failed')
                    logger.warning('Review reconciliation scan failed; durable work retained')
                    self._publish()
                self.wakeup.wait(POLL_SECONDS)
        finally:
            self.status = replace(self.status, worker_state='stopped')
            self._publish()

    def _read(self, sql, params=()):
        conn = connect_db(self.path, row_factory=False)
        try:
            conn.execute('PRAGMA busy_timeout=250')
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    def _error(self, error):
        code = getattr(error, 'code', None)
        if isinstance(error, ReviewInputError):
            code = 'unexpected_response'
        elif isinstance(error, ValueError):
            code = 'membership_conflict'
        if code not in ISSUE_CODES:
            code = 'api_unavailable'
        self.scan_error = code
        self.status = replace(self.status, api_state='unsupported' if code == 'unsupported' else 'degraded',
                              error_code=code)
        return code

    def _apply(self, row):
        review = normalize_review(row)
        if review['camera_name'] not in self.cameras:
            raise FrigateReviewError('camera_mismatch')
        if self.stop.is_set():
            return
        with metadata_connection(self.path) as conn:
            issue = apply_review(conn, review, now=self.clock(), from_api=True)
        if issue:
            raise FrigateReviewError(issue)

    def _defer(self, kind, identity, error):
        code = self._error(error)
        if self.stop.is_set():
            return
        with metadata_connection(self.path) as conn:
            if kind == 'review':
                row = repository.get_review(conn, identity)
                if not row or row['state'] == 'finalized':
                    return
                if code in ('membership_conflict', 'camera_mismatch'):
                    conn.execute('''UPDATE frigate_reviews SET reconciliation_status='incomplete',
                        next_retry_at=NULL, last_error=? WHERE review_id=?''', (code, identity))
                    return
                if row['last_error'] in ('membership_conflict', 'camera_mismatch'):
                    return
                count = min(row['retry_count'] + 1, 1000000)
                repository.set_review_state(
                    conn, identity, row['state'], reconciliation_status='deferred',
                    retry_count=count, next_retry_at=self.clock() + retry_delay(count), last_error=code,
                )
            else:
                row = repository.get_event(conn, identity)
                if not row or (row['last_error'] or '').startswith('membership_conflict'):
                    return
                if ready(row):
                    if not terminal_complete(row) and not (row['last_error'] or '').startswith('terminal:'):
                        repository.set_event_retry(conn, identity, retry_count=0)
                    return  # Terminal work owns retry metadata once the member is ready.
                count = min(row['retry_count'] + 1, 1000000)
                repository.set_event_retry(conn, identity, retry_count=count,
                                           next_retry_at=self.clock() + retry_delay(count), last_error=code)

    def scan(self):
        now = self.clock()
        self.scan_error = None
        self.refresh_counts()
        # Discovery is bounded and independent of MQTT silence. Direct membership
        # lookup recovers older known events beyond this recent-review page.
        if self.cameras and now >= self.next_discovery and not self.stop.is_set():
            camera = self.cameras[self.camera_cursor % len(self.cameras)]
            self.camera_cursor += 1
            self.next_discovery = now + 60
            try:
                rows = self.client.list_reviews(camera, before=now + .000001)
                normalized = [normalize_review(row) for row in rows]
                for row, checked in zip(rows, normalized):
                    if checked['camera_name'] != camera:
                        raise FrigateReviewError('unexpected_response')
                    self._apply(row)
                self.status = replace(self.status, api_state='available',
                                      error_code='discovery_truncated' if len(rows) == 100 else None)
            except (FrigateReviewError, ValueError) as error:
                self._error(error)
                self.next_discovery = now + MAX_RETRY_SECONDS
        reviews = self._read('''SELECT review_id FROM frigate_reviews
            WHERE state != 'finalized' AND reconciliation_status != 'incomplete'
            AND (end_time IS NULL OR reconciliation_status != 'synced')
            AND (next_retry_at IS NULL OR next_retry_at <= ?)
            ORDER BY COALESCE(next_retry_at, 0), start_time, review_id LIMIT ?''', (now, BATCH_SIZE))
        for (identity,) in reviews:
            if self.stop.is_set():
                break
            try:
                self._apply(self.client.get_review(identity))
                self.status = replace(self.status, api_state='available')
            except (FrigateReviewError, ValueError) as error:
                self._defer('review', identity, error)
        events = self._read('''SELECT event_id FROM frigate_review_events
            WHERE (review_id IS NULL OR label IS NULL OR end_time IS NULL)
            AND COALESCE(last_error, '') NOT LIKE 'membership_conflict%'
            AND (next_retry_at IS NULL OR next_retry_at <= ?)
            ORDER BY COALESCE(next_retry_at, 0), start_time, event_id LIMIT ?''', (now, BATCH_SIZE))
        for (identity,) in events:
            if self.stop.is_set():
                break
            try:
                self.reconcile_event(identity)
                self.status = replace(self.status, api_state='available')
            except (FrigateReviewError, ValueError) as error:
                self._defer('event', identity, error)
        if self.scan_error is not None:
            self.status = replace(self.status, error_code=self.scan_error,
                                  api_state='unsupported' if self.status.api_state == 'unsupported' else 'degraded')
        if self.terminal_classifier is not None and not self.stop.is_set():
            self.terminal_classifier.run_batch(self.path, self.cameras, self.stop, self.clock)
        if self.aggregator is not None and not self.stop.is_set():
            self.aggregator.run_batch(self.path, self.cameras, self.stop, self.clock)
        self.refresh_counts()

    def reconcile_event(self, identity):
        with metadata_connection(self.path) as conn:
            event = repository.get_event(conn, identity)
        if not event:
            return
        metadata_error = None
        if event['label'] is None or event['start_time'] is None or event['end_time'] is None:
            try:
                metadata = normalize_event(self.client.get_tracked_event(identity))
            except FrigateReviewError as error:
                # Review membership may survive after tracked-object metadata expires.
                metadata_error = error
            else:
                if metadata['event_id'] != identity or metadata['camera_name'] != event['camera_name']:
                    raise FrigateReviewError('unexpected_response')
                if self.stop.is_set():
                    return
                with metadata_connection(self.path) as conn:
                    event = repository.upsert_event(conn, **metadata)
        if self.stop.is_set():
            return
        if event['review_id'] is None:
            row = self.client.get_review_for_event(identity, camera=event['camera_name'],
                                                  start_time=event['start_time'], end_time=event['end_time'])
            checked = normalize_review(row)
            if identity not in checked['members'] or checked['camera_name'] != event['camera_name']:
                raise FrigateReviewError('unexpected_response')
            self._apply(row)
        if metadata_error is not None:
            raise metadata_error
        if not self.stop.is_set():
            with metadata_connection(self.path) as conn:
                current = repository.get_event(conn, identity)
                if (not (current['last_error'] or '').startswith(('membership_conflict', 'terminal:'))
                        and not terminal_complete(current)):
                    repository.set_event_retry(conn, identity, retry_count=0,
                                               next_retry_at=None if ready(current) else self.clock() + 60)

    def refresh_counts(self):
        counts = self._read('''SELECT
            (SELECT COUNT(*) FROM frigate_reviews WHERE end_time IS NULL AND state != 'finalized'),
            (SELECT COUNT(*) FROM frigate_reviews WHERE state = 'closing'),
            (SELECT COUNT(*) FROM frigate_reviews WHERE reconciliation_status IN ('deferred','incomplete')),
            (SELECT COUNT(*) FROM frigate_review_events WHERE review_id IS NULL),
            (SELECT COUNT(*) FROM frigate_reviews WHERE reconciliation_status != 'synced') +
            (SELECT COUNT(*) FROM frigate_review_events WHERE review_id IS NULL OR label IS NULL OR end_time IS NULL)
            ''')[0]
        self.status = replace(self.status, active_reviews=counts[0], closing_reviews=counts[1],
                              deferred_reviews=counts[2], unmatched_events=counts[3], backlog=counts[4])
        terminal = self._read('''SELECT
            COALESCE(SUM(CASE WHEN end_time IS NOT NULL AND label IS NOT NULL AND
                (classification_phase IS NULL OR classification_phase != 'terminal' OR classification_state='pending')
                THEN 1 ELSE 0 END), 0),
            COALESCE(SUM(CASE WHEN last_error LIKE 'terminal:%' AND next_retry_at IS NOT NULL
                AND (classification_phase IS NULL OR classification_phase != 'terminal' OR classification_state='pending')
                THEN 1 ELSE 0 END), 0),
            COALESCE(SUM(CASE WHEN classification_phase='terminal' AND classification_state='classified' THEN 1 ELSE 0 END), 0),
            COALESCE(SUM(CASE WHEN classification_phase='terminal' AND classification_state='rejected' THEN 1 ELSE 0 END), 0),
            COALESCE(SUM(CASE WHEN classification_phase='terminal' AND classification_state='failed' THEN 1 ELSE 0 END), 0)
            FROM frigate_review_events WHERE review_id IS NOT NULL''')[0]
        self.status = replace(self.status, terminal_pending=terminal[0], terminal_retrying=terminal[1],
                              terminal_classified=terminal[2], terminal_rejected=terminal[3], terminal_failed=terminal[4])
        aggregate = self._read("""SELECT
            (SELECT COUNT(*) FROM frigate_reviews r WHERE state != 'finalized'
                AND end_time IS NOT NULL AND reconciliation_status='synced'
                AND NOT EXISTS (SELECT 1 FROM frigate_review_events e WHERE e.review_id=r.review_id
                    AND (e.label IS NULL OR e.start_time IS NULL OR e.end_time IS NULL
                        OR e.classification_phase IS NULL OR e.classification_phase!='terminal'
                        OR e.classification_state='pending' OR e.last_error LIKE 'membership_conflict%'))),
            (SELECT COUNT(*) FROM frigate_reviews WHERE state='finalized'),
            (SELECT COUNT(*) FROM frigate_reviews WHERE last_error='aggregation_failed'),
            (SELECT COUNT(*) FROM detections WHERE frigate_review_id IS NOT NULL)""")[0]
        self.status = replace(self.status, aggregation_ready=aggregate[0], finalized_reviews=aggregate[1],
                              aggregation_failed=aggregate[2], grouped_observations=aggregate[3])
        self._publish()
