"""Small safe diagnostics shared through the supervisor's existing MQTT IPC."""

from dataclasses import dataclass

ISSUE_CODES = frozenset({
    'malformed_review', 'malformed_event', 'membership_conflict', 'camera_mismatch',
    'database_write_failed', 'api_unavailable', 'unsupported', 'review_missing',
    'event_missing', 'lookup_unavailable', 'lookup_incomplete', 'unexpected_response',
    'timeout', 'connection_error', 'scan_failed', 'worker_failed', 'discovery_truncated',
})


@dataclass(frozen=True)
class ReviewRuntimeSnapshot:
    worker_state: str = 'unknown'
    api_state: str = 'unknown'
    active_reviews: int = 0
    closing_reviews: int = 0
    deferred_reviews: int = 0
    unmatched_events: int = 0
    backlog: int = 0
    error_code: str | None = None
    terminal_pending: int = 0
    terminal_retrying: int = 0
    terminal_classified: int = 0
    terminal_rejected: int = 0
    terminal_failed: int = 0
    aggregation_ready: int = 0
    finalized_reviews: int = 0
    aggregation_failed: int = 0
    grouped_observations: int = 0

    def valid(self):
        return (isinstance(self.worker_state, str) and isinstance(self.api_state, str)
                and (self.error_code is None or isinstance(self.error_code, str))
                and self.worker_state in ('unknown', 'running', 'stopped', 'failed')
                and self.api_state in ('unknown', 'available', 'degraded', 'unsupported')
                and self.error_code in ISSUE_CODES | {None}
                and all(type(value) is int and value >= 0 for value in (
                    self.active_reviews, self.closing_reviews, self.deferred_reviews,
                    self.unmatched_events, self.backlog, self.terminal_pending, self.terminal_retrying,
                    self.terminal_classified, self.terminal_rejected, self.terminal_failed,
                    self.aggregation_ready, self.finalized_reviews, self.aggregation_failed, self.grouped_observations)))
