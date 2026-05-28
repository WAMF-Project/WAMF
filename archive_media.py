from pathlib import Path
import time

import requests
from system_events import log_system_event

WAMF_SNAPSHOT_DIR = Path("media/wamf/snapshots")
WAMF_CLIP_DIR = Path("media/wamf/clips")


def archive_snapshot(
    frigate_url: str,
    frigate_event: str
) -> str | None:

    snapshot_url = (
        f"{frigate_url}/api/events/"
        f"{frigate_event}/snapshot.jpg"
    )

    destination = (
        WAMF_SNAPSHOT_DIR
        / f"{frigate_event}.jpg"
    )

    try:

        response = requests.get(
            snapshot_url,
            timeout=10
        )

        if response.status_code != 200:

            print(
                f"Snapshot download failed: "
                f"{response.status_code}"
            )

            return None

        with open(destination, "wb") as f:
            f.write(response.content)

        print(
            f"Archived snapshot: {destination}"
        )

        return str(destination)

    except Exception as e:

        print(
            f"Snapshot archive error: {e}"
        )

        return None


def archive_clip(
    frigate_url: str,
    frigate_event: str
) -> str | None:

    clip_url = (
        f"{frigate_url}/api/events/"
        f"{frigate_event}/clip.mp4"
    )

    destination = (
        WAMF_CLIP_DIR
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

            print(
                f"Clip not ready yet "
                f"(attempt {attempt + 1})"
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

            print(
                f"Clip download failed: "
                f"{response.status_code}"
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

        print(
            f"Archived clip: {destination}"
        )

        log_system_event(
            "INFO",
            "ARCHIVE",
            f"Archived clip successfully "
            f"for event {frigate_event}"
        )

        return str(destination)

    except Exception as e:

        print(
            f"Clip archive error: {e}"
        )

        return None