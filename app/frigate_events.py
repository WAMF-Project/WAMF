"""Frigate event identifiers shared by ingestion and media consumers."""

import re


def is_valid_frigate_event(value):
    """Preserve the proxy's existing unreserved ASCII identifier alphabet."""
    return bool(
        isinstance(value, str)
        and re.fullmatch(r'[A-Za-z0-9_.-]+', value)
        and value not in {'.', '..'}
    )
