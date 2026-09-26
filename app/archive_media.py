import logging
import time

import requests

from app.frigate_client import FrigateClient, FrigateError
from app.system_events import log_system_event
from wamf_paths import ensure_storage_paths, get_clips_path, get_snapshots_path

logger = logging.getLogger(__name__)


def archive_snapshot(
    frigate_url: str,
    frigate_event: str
) -> str | None:

    ensure_storage_paths()

    destination = (
        get_snapshots_path()
        / f"{frigate_event}.jpg"
    )

    try:

        snapshot = FrigateClient(frigate_url).get_event_snapshot(
            frigate_event
        )

        with open(destination, "wb") as f:
            f.write(snapshot)

        logger.info("Archived snapshot: %s", destination)

        return str(destination)

    except FrigateError as e:

        if e.status_code is not None:
            logger.warning(
                f"Snapshot download failed: "
                f"{e.status_code}"
            )

            return None

        logger.warning("Snapshot archive request error: %s", e)

        return None

    except OSError as e:

        logger.warning("Snapshot archive file error: %s", e)

        return None


def archive_clip(
    frigate_url: str,
    frigate_event: str
) -> str | None:

    ensure_storage_paths()

    destination = (
        get_clips_path()
        / f"{frigate_event}.mp4"
    )

    try:

        client = FrigateClient(frigate_url)

        for attempt in range(10):

            try:
                response = client.stream_event_media(
                    frigate_event,
                    "clip.mp4",
                )
                break
            except FrigateError as e:
                if e.status_code is None:
                    raise
                status_code = e.status_code

            logger.info(
                "Clip not ready yet for event %s (attempt %s)",
                frigate_event,
                attempt + 1,
            )

            log_system_event(
                "WARN",
                "ARCHIVE",
                f"Clip not ready yet "
                f"(attempt {attempt + 1}) "
                f"for event {frigate_event}"
            )

            time.sleep(2)

        else:

            logger.error(
                "Clip download failed for event %s: %s",
                frigate_event,
                status_code,
            )

            log_system_event(
                "ERROR",
                "ARCHIVE",
                f"Clip archive failed "
                f"for event {frigate_event}"
            )

            return None

        try:
            with open(destination, "wb") as f:

                for chunk in response.iter_content(
                    chunk_size=8192
                ):

                    if chunk:
                        f.write(chunk)
        finally:
            response.close()

        logger.info("Archived clip: %s", destination)

        log_system_event(
            "INFO",
            "ARCHIVE",
            f"Archived clip successfully "
            f"for event {frigate_event}"
        )

        return str(destination)

    except (FrigateError, requests.exceptions.RequestException) as e:

        logger.warning("Clip archive request error: %s", e)

        return None

    except OSError as e:

        logger.warning("Clip archive file error: %s", e)

        return None
