import logging
import time

import requests
from app.system_events import log_system_event
from wamf_paths import ensure_storage_paths, get_clips_path, get_snapshots_path

logger = logging.getLogger(__name__)


def archive_snapshot(
    frigate_url: str,
    frigate_event: str
) -> str | None:

    ensure_storage_paths()
    snapshot_url = (
        f"{frigate_url}/api/events/"
        f"{frigate_event}/snapshot.jpg"
    )

    destination = (
        get_snapshots_path()
        / f"{frigate_event}.jpg"
    )

    try:

        response = requests.get(
            snapshot_url,
            timeout=10
        )

        if response.status_code != 200:

            logger.warning(
                f"Snapshot download failed: "
                f"{response.status_code}"
            )

            return None

        with open(destination, "wb") as f:
            f.write(response.content)

        logger.info("Archived snapshot: %s", destination)

        return str(destination)

    except requests.exceptions.RequestException as e:

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
    clip_url = (
        f"{frigate_url}/api/events/"
        f"{frigate_event}/clip.mp4"
    )

    destination = (
        get_clips_path()
        / f"{frigate_event}.mp4"
    )

    try:

        for attempt in range(10):

            response = requests.get(
                clip_url,
                timeout=30,
                stream=True
            )

            if response.status_code == 200:
                break

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
                response.status_code,
            )

            log_system_event(
                "ERROR",
                "ARCHIVE",
                f"Clip archive failed "
                f"for event {frigate_event}"
            )

            return None

        with open(destination, "wb") as f:

            for chunk in response.iter_content(
                chunk_size=8192
            ):

                if chunk:
                    f.write(chunk)

        logger.info("Archived clip: %s", destination)

        log_system_event(
            "INFO",
            "ARCHIVE",
            f"Archived clip successfully "
            f"for event {frigate_event}"
        )

        return str(destination)

    except requests.exceptions.RequestException as e:

        logger.warning("Clip archive request error: %s", e)

        return None

    except OSError as e:

        logger.warning("Clip archive file error: %s", e)

        return None
