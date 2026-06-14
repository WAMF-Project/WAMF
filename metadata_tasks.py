import logging
import queue
import threading

import requests

from queries import get_common_name, get_species_info, save_species_info
from species_metadata import fetch_species_metadata
from system_events import log_system_event

logger = logging.getLogger(__name__)

_metadata_queue = queue.Queue()
_queued_species = set()
_worker_started = False
_worker_lock = threading.Lock()


def species_needs_metadata(species_info):
    """Return True when the UI is missing enough metadata to justify a refresh."""
    return (
        not species_info
        or not species_info.get("description")
        or not species_info.get("thumbnail_url")
    )


def refresh_species_metadata(scientific_name):
    metadata = fetch_species_metadata(scientific_name)

    save_species_info(
        scientific_name=scientific_name,
        common_name=get_common_name(scientific_name),
        description=metadata["description"],
        wikipedia_url=metadata["wikipedia_url"],
        thumbnail_url=metadata["thumbnail_url"]
    )


def _metadata_worker():
    """Process metadata refreshes outside request handling."""
    while True:
        scientific_name = _metadata_queue.get()

        try:
            if species_needs_metadata(get_species_info(scientific_name)):
                refresh_species_metadata(scientific_name)
                log_system_event(
                    "INFO",
                    "SPECIES",
                    f"Refreshed metadata for {scientific_name}"
                )
        except (requests.exceptions.RequestException, KeyError, ValueError) as exc:
            logger.warning(
                "Metadata refresh failed for %s: %s",
                scientific_name,
                exc,
            )
            log_system_event(
                "WARN",
                "SPECIES",
                f"Metadata refresh failed for {scientific_name}"
            )
        finally:
            _queued_species.discard(scientific_name)
            _metadata_queue.task_done()


def start_metadata_worker():
    global _worker_started

    with _worker_lock:
        if _worker_started:
            return

        worker = threading.Thread(
            target=_metadata_worker,
            name="wamf-metadata-refresh",
            daemon=True
        )
        worker.start()
        _worker_started = True


def queue_metadata_refresh(scientific_name):
    if not scientific_name:
        return False

    start_metadata_worker()

    # Keep repeat page views from piling up duplicate Wikipedia fetches.
    if scientific_name in _queued_species:
        return False

    _queued_species.add(scientific_name)
    _metadata_queue.put(scientific_name)
    return True
