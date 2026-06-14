import logging

import requests
from flask import send_file, send_from_directory

logger = logging.getLogger(__name__)


def proxy_frigate_media(
    frigate_url,
    frigate_event,
    media_name,
    timeout,
    fallback_filename="1x1.png",
):
    """Fetch Frigate event media and return a tiny placeholder if unavailable."""
    media_url = f"{frigate_url}/api/events/{frigate_event}/{media_name}"

    try:
        response = requests.get(
            media_url,
            stream=True,
            timeout=timeout
        )
    except requests.exceptions.RequestException as exc:
        logger.warning(
            "Error fetching %s for Frigate event %s: %s",
            media_name,
            frigate_event,
            exc,
        )
        return send_from_directory(
            "static/images",
            fallback_filename,
            mimetype="image/png"
        )

    if response.status_code != 200:
        logger.info(
            "Frigate returned %s for %s on event %s",
            response.status_code,
            media_name,
            frigate_event,
        )
        return send_from_directory(
            "static/images",
            fallback_filename,
            mimetype="image/png"
        )

    return send_file(
        response.raw,
        mimetype=response.headers.get("Content-Type")
    )
