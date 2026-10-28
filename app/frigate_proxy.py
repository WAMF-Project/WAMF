import logging
import re
from urllib.parse import quote

import requests
from flask import Response, request, send_from_directory, stream_with_context
from urllib3.exceptions import HTTPError as UpstreamHTTPError
from werkzeug.http import parse_range_header

from app.frigate_events import is_valid_frigate_event

logger = logging.getLogger(__name__)


def proxy_frigate_media(
    frigate_url,
    frigate_event,
    media_name,
    timeout,
    fallback_filename="1x1.png",
):
    """Fetch Frigate event media and return a tiny placeholder if unavailable."""
    def fallback():
        return send_from_directory(
            "static/images", fallback_filename, mimetype="image/png", conditional=False
        )

    # Keep event IDs in one URL segment, including Frigate's timestamp-ID form.
    if not is_valid_frigate_event(frigate_event):
        return fallback()

    media_url = f"{frigate_url}/api/events/{quote(frigate_event, safe='')}/{media_name}"

    upstream_headers = {"Accept-Encoding": "identity"}
    if media_name == "clip.mp4" and request.method == "GET":
        range_header = request.headers.get("Range", "")
        byte_range = (
            parse_range_header(range_header) if len(range_header) <= 128 else None
        )
        # Ignore multipart/invalid ranges rather than amplify upstream work.
        if byte_range and byte_range.units == "bytes" and len(byte_range.ranges) == 1:
            upstream_headers["Range"] = byte_range.to_header()
            if_range = request.headers.get("If-Range", "")
            if re.fullmatch(r"[\x20-\x7e]{1,256}", if_range):
                upstream_headers["If-Range"] = if_range
            elif if_range:
                # Do not apply a range without the client's supplied validator.
                upstream_headers.pop("Range")

    response = None
    try:
        response = requests.get(
            media_url,
            stream=True,
            timeout=timeout,
            allow_redirects=False,
            headers=upstream_headers,
        )
        range_response = (
            "Range" in upstream_headers and response.status_code in {206, 416}
        )
        if response.status_code != 200 and not range_response:
            logger.info(
                "Frigate media request returned HTTP %s",
                response.status_code,
            )
            response.close()
            return fallback()

        # Keep the representation intact: iter_content decompresses bytes, which
        # would invalidate Content-Length and Content-Range for encoded media.
        chunks = response.raw.stream(8192, decode_content=False)
        # Catch initial read failures before committing the downstream headers.
        first_chunk = next(chunks, b"")
    except (requests.exceptions.RequestException, UpstreamHTTPError):
        if response is not None:
            response.close()
        # Requests exception messages can contain the configured URL/credentials.
        logger.warning("Error fetching Frigate media")
        return fallback()
    except Exception:
        if response is not None:
            response.close()
        raise

    closed = False

    def close_upstream():
        nonlocal closed
        if not closed:
            closed = True
            response.close()

    def generate():
        try:
            if first_chunk:
                yield first_chunk
            for chunk in chunks:
                if chunk:
                    yield chunk
        except (requests.exceptions.RequestException, UpstreamHTTPError):
            # WSGI servers may log streaming exceptions after headers are sent.
            raise requests.exceptions.RequestException(
                "Frigate media stream failed"
            ) from None
        finally:
            close_upstream()

    try:
        downstream = Response(
            status=response.status_code,
            content_type=(
                response.headers.get("Content-Type") or "application/octet-stream"
            ),
        )
        # Only representation/playback metadata crosses the proxy boundary.
        # In particular, do not forward cookies, redirects or hop-by-hop headers.
        connection_headers = {
            name.strip().lower()
            for name in response.headers.get("Connection", "").split(",")
        }
        for name in (
            "Content-Length", "Content-Encoding", "Accept-Ranges",
            "Content-Range", "ETag", "Last-Modified",
        ):
            if name in response.headers and name.lower() not in connection_headers:
                downstream.headers[name] = response.headers[name]
        downstream.response = stream_with_context(generate())
    except Exception:
        close_upstream()
        raise
    # Preserve send_file's default cache policy for event media.
    downstream.cache_control.no_cache = True
    # HEAD requests or cancellation may close the response before generate starts.
    downstream.call_on_close(close_upstream)
    return downstream
