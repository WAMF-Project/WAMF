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


def archive_representative(client, event_id, stop):
    """One bounded attempt per media type; only detection rows own these files.

    Atomic replacement avoids leaving a partial playable archive on interruption.
    A missing clip is optional, as in the legacy archive path.
    """
    from pathlib import Path

    ensure_storage_paths()
    paths = []
    for root, suffix, media_name, limit in (
        (get_snapshots_path(), '.jpg', 'snapshot.jpg', 4 * 1024 * 1024),
        (get_clips_path(), '.mp4', 'clip.mp4', 64 * 1024 * 1024),
    ):
        destination = root / f'{event_id}{suffix}'
        temporary = Path(str(destination) + '.pending')
        try:
            if stop.is_set():
                paths.append(None)
                continue
            if destination.is_file():
                paths.append(str(destination))
                continue
            response = client.stream_event_media(event_id, media_name, timeout=(2, 2))
            try:
                deadline, size = time.monotonic() + 5, 0
                with temporary.open('wb') as stream:
                    for chunk in response.iter_content(chunk_size=65536):
                        size += len(chunk)
                        if stop.is_set() or size > limit or time.monotonic() > deadline:
                            raise OSError('Archive interrupted or exceeds bounds')
                        stream.write(chunk)
                if not size:
                    raise OSError('Empty media')
                temporary.replace(destination)
                paths.append(str(destination))
            finally:
                response.close()
        except (FrigateError, requests.exceptions.RequestException, OSError):
            logger.warning('Representative media unavailable')
            paths.append(None)
        finally:
            temporary.unlink(missing_ok=True)
    return tuple(paths)


def cleanup_unreferenced_archive(stored):
    """Caller checks DB references and holds media_activity_guard."""
    from wamf_paths import delete_wamf_media_files

    delete_wamf_media_files(stored, stored)


def unreferenced_archive_paths(conn, candidates):
    """Compare resolved paths too: historical references may be relative/absolute.

    Stream current references once; memory is bounded by this review's candidates,
    not the size of detection history. Caller holds the media guard.
    """
    from wamf_paths import contained_media_path

    candidates = {stored for stored in candidates if stored}
    by_path = {}
    for stored in candidates:
        for kind in ('snapshots', 'clips'):
            path = contained_media_path(stored, kind)
            if path is not None:
                by_path.setdefault(path.resolve(), set()).add(stored)
    protected = set()
    for row in conn.execute('SELECT wamf_snapshot_path,wamf_clip_path FROM detections'):
        for stored, kind in zip(row, ('snapshots', 'clips'), strict=True):
            if not stored:
                continue
            path = contained_media_path(stored, kind)
            if path is not None:
                protected.update(by_path.get(path.resolve(), ()))
    return candidates - protected
