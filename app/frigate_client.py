"""Small HTTP boundary for the Frigate operations used by WAMF."""

import json
import math
import time
from collections import OrderedDict
from threading import Lock
from urllib.parse import quote

import requests

from app.terminal_evidence import (
    IneligibleEvent,
    TerminalEvidenceError,
    TerminalImage,
    decode_revision,
    evidence_hint,
    number,
)


class FrigateError(Exception):
    """Raised when a Frigate HTTP operation cannot be completed."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code


class FrigateDiscoveryError(FrigateError):
    """A safe, classified discovery failure with no remote response data."""

    def __init__(self, code):
        super().__init__("Frigate camera discovery failed")
        self.code = code


class FrigateReviewError(FrigateError):
    """Fixed safe reconciliation diagnostic, never a response body or URL."""

    def __init__(self, code, status_code=None):
        super().__init__('Frigate review reconciliation failed', status_code=status_code)
        self.code = code


def nonnegative_number(value):
    """Unknown telemetry is never silently converted to zero."""
    try:
        return value if (type(value) in (int, float) and math.isfinite(value)
                         and value >= 0) else None
    except OverflowError:
        return None


def normalize_camera_stats(payload):
    """Discard everything except the small camera-health evidence contract."""
    if not isinstance(payload, dict) or not isinstance(payload.get('cameras'), dict):
        raise FrigateDiscoveryError('unexpected_response')
    cameras = {}
    for camera_id, camera in payload['cameras'].items():
        if not isinstance(camera_id, str) or not camera_id or not camera_id.isprintable():
            raise FrigateDiscoveryError('unexpected_response')
        camera = camera if isinstance(camera, dict) else {}
        cameras[camera_id] = {
            field: nonnegative_number(camera.get(field))
            for field in ('camera_fps', 'process_fps', 'detection_fps')
        }
        cameras[camera_id]['detection_enabled'] = (
            camera.get('detection_enabled') if type(camera.get('detection_enabled')) is bool else None
        )
        for field in ('pid', 'capture_pid', 'ffmpeg_pid'):
            value = camera.get(field)
            cameras[camera_id][field] = value if type(value) is int and value > 0 else None
    service = payload.get('service')
    service = service if isinstance(service, dict) else {}
    # Version is not needed for classification or presentation. Do not retain
    # arbitrary remote strings, command lines, or other service metadata.
    return {'cameras': cameras, 'service': {
        field: nonnegative_number(service.get(field)) for field in ('last_updated', 'uptime')
    }}


class FrigateClient:
    """Client for the subset of the Frigate HTTP API currently used by WAMF."""

    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        self._snapshot_cache = OrderedDict()
        self._snapshot_cache_lock = Lock()

    def remember_event_snapshot(self, event_id, content, metadata):
        """Reuse a legacy crop only as a bounded, ephemeral last-resort fallback."""
        if (not isinstance(content, bytes) or not content or len(content) > 2 * 1024 * 1024
                or metadata.get('id') != event_id):
            return
        frame_time = evidence_hint(metadata)['frame_time']
        with self._snapshot_cache_lock:
            self._snapshot_cache.pop(event_id, None)
            self._snapshot_cache[event_id] = (content, frame_time, metadata.get('camera'))
            while (len(self._snapshot_cache) > 32
                   or sum(len(value[0]) for value in self._snapshot_cache.values()) > 16 * 1024 * 1024):
                self._snapshot_cache.popitem(last=False)

    def acquire_terminal_image(self, event, *, allow_fallback=False):
        """Use this event's completed snapshot; never a camera's latest image.

        Frigate does not expose a separate timestamp-addressable best-snapshot
        URL. Metadata identifies the best frame; snapshot.jpg serves its image.
        Older completed-event APIs may omit the frame timestamp and ignore crop
        overrides in favor of saved snapshot configuration.
        """
        identity = event['event_id']
        hint = decode_revision(event['snapshot_revision'])
        if event['end_time'] is None:
            raise TerminalEvidenceError('event_not_terminal')
        if event['label'] != 'bird':
            raise IneligibleEvent('non_bird')
        if hint.get('terminal') and hint.get('false_positive') is True:
            raise IneligibleEvent('false_positive')
        frame_time = number(hint.get('frame_time')) if hint.get('terminal') else None
        source = 'mqtt_terminal_hint' if hint.get('terminal') else 'durable_event_end'
        try:
            metadata = self.get_tracked_event(identity)
        except FrigateReviewError:
            # An observed MQTT end remains completion evidence if the object API
            # has pruned the object or this API family lacks metadata lookup.
            metadata = None
        if metadata is not None:
            if (metadata.get('id') != identity or metadata.get('camera') != event['camera_name']
                    or metadata.get('label') != event['label']):
                raise TerminalEvidenceError('event_identity_mismatch', retryable=False)
            if metadata.get('end_time') is None:
                raise TerminalEvidenceError('event_not_terminal')
            if (number(metadata.get('end_time')) is None or
                    (metadata.get('false_positive') is not None and type(metadata['false_positive']) is not bool)):
                raise TerminalEvidenceError('invalid_event_metadata')
            if metadata.get('false_positive') is True:
                raise IneligibleEvent('false_positive')
            final_time = evidence_hint(metadata, ended=True)['frame_time']
            if final_time is not None:
                frame_time = final_time
            source = ('terminal_event_api+mqtt_snapshot_hint'
                      if final_time is None and frame_time is not None else 'terminal_event_api')
        try:
            content = self.get_event_snapshot(identity, crop=True, quality=95, max_bytes=4 * 1024 * 1024)
        except FrigateError as error:
            if allow_fallback and error.status_code in (404, 410):
                with self._snapshot_cache_lock:
                    cached = self._snapshot_cache.get(identity)
                if cached is not None and cached[2] == event['camera_name']:
                    return TerminalImage(cached[0], cached[1], 'cached_legacy_fallback', source)
            retryable = error.status_code not in (400, 401, 403, 405, 413)
            raise TerminalEvidenceError('snapshot_unavailable', retryable=retryable) from None
        if not content:
            raise TerminalEvidenceError('snapshot_unavailable')
        return TerminalImage(content, frame_time, 'event_snapshot_after_end', source)

    def _event_url(self, event_id: str, resource: str) -> str:
        return f"{self.base_url}/api/events/{event_id}/{resource}"

    def _review_request(self, resource, *, params=None):
        response = None
        deadline = time.monotonic() + 5
        try:
            response = requests.get(f'{self.base_url}/api/{resource}', params=params,
                                    timeout=(2, 2), stream=True, allow_redirects=False)
            if response.status_code != 200:
                raise FrigateReviewError('http_error', response.status_code)
            body = bytearray()
            for chunk in response.iter_content(chunk_size=8192):
                body.extend(chunk)
                if len(body) > 1024 * 1024 or time.monotonic() > deadline:
                    raise FrigateReviewError('unexpected_response')
            try:
                return json.loads(body)
            except (ValueError, UnicodeError, RecursionError):
                raise FrigateReviewError('unexpected_response') from None
        except requests.exceptions.Timeout:
            raise FrigateReviewError('timeout') from None
        except requests.exceptions.RequestException:
            raise FrigateReviewError('connection_error') from None
        finally:
            if response is not None:
                response.close()

    def list_reviews(self, camera, *, after=0.000001, before=None, limit=100):
        params = {'cameras': camera, 'after': after, 'limit': limit}
        if before is not None:
            params['before'] = before
        try:
            rows = self._review_request('review', params=params)
        except FrigateReviewError as exc:
            if exc.status_code in (404, 405, 501):
                raise FrigateReviewError('unsupported') from None
            raise
        if not isinstance(rows, list) or len(rows) > limit:
            raise FrigateReviewError('unexpected_response')
        return rows

    def get_review(self, review_id):
        """Modern singular lookup, falling back to the older review_ids API."""
        try:
            row = self._review_request(f'review/{quote(review_id, safe="")}')
        except FrigateReviewError as exc:
            if exc.status_code not in (404, 405, 501):
                raise
            try:
                rows = self._review_request('review_ids', params={'ids': review_id})
            except FrigateReviewError as fallback:
                if fallback.status_code not in (404, 405, 501):
                    raise
                raise FrigateReviewError('lookup_unavailable') from None
            if not isinstance(rows, list):
                raise FrigateReviewError('unexpected_response')
            row = next((item for item in rows if isinstance(item, dict)
                        and item.get('id') == review_id), None)
            if row is None:
                raise FrigateReviewError('review_missing')
        if not isinstance(row, dict) or row.get('id') != review_id:
            raise FrigateReviewError('unexpected_response')
        return row

    def get_review_for_event(self, event_id, *, camera, start_time, end_time=None):
        """Explicit membership only. Interval fallback never invents a grouping."""
        try:
            row = self._review_request(f'review/event/{quote(event_id, safe="")}')
        except FrigateReviewError as exc:
            if exc.status_code not in (404, 405, 501):
                raise
            if start_time is None:
                raise FrigateReviewError('lookup_unavailable') from None
            rows = self.list_reviews(camera, after=max(start_time - .000001, .000001),
                                     before=(end_time or time.time()) + .000001)
            if any(not isinstance(item, dict) or not isinstance(item.get('data'), dict)
                   or not isinstance(item['data'].get('detections'), list) for item in rows):
                raise FrigateReviewError('unexpected_response')
            matches = [item for item in rows if isinstance(item, dict)
                       and isinstance(item.get('data'), dict)
                       and event_id in item['data'].get('detections', [])]
            if len(matches) > 1:
                raise FrigateReviewError('membership_conflict')
            if not matches:
                raise FrigateReviewError('lookup_incomplete' if len(rows) == 100 else 'event_missing')
            row = matches[0]
        if not isinstance(row, dict):
            raise FrigateReviewError('unexpected_response')
        return row

    def get_tracked_event(self, event_id):
        try:
            row = self._review_request(f'events/{quote(event_id, safe="")}')
        except FrigateReviewError as exc:
            if exc.status_code == 404:
                raise FrigateReviewError('event_missing') from None
            raise
        if not isinstance(row, dict) or row.get('id') != event_id:
            raise FrigateReviewError('unexpected_response')
        return row

    @staticmethod
    def _require_ok(response: requests.Response, operation: str) -> None:
        if response.status_code == 200:
            return

        try:
            response.raise_for_status()
        except requests.exceptions.RequestException as exc:
            raise FrigateError(
                f"Frigate {operation} failed with HTTP {response.status_code}",
                status_code=response.status_code,
            ) from exc

        # Keep the existing callers' exact-200 success contract, including for
        # redirects or other statuses which requests does not treat as errors.
        raise FrigateError(
            f"Frigate {operation} failed with HTTP {response.status_code}",
            status_code=response.status_code,
        )

    def set_event_sub_label(self, event_id: str, sub_label: str) -> None:
        """Set an event sublabel using Frigate's existing JSON payload format."""
        # Frigate currently limits sublabels to 20 characters.
        sub_label = sub_label[:20]
        try:
            response = requests.post(
                self._event_url(event_id, "sub_label"),
                data=json.dumps({"subLabel": sub_label}),
                headers={"Content-Type": "application/json"},
                timeout=2,
            )
            self._require_ok(response, "sublabel update")
        except FrigateError:
            raise
        except requests.exceptions.RequestException as exc:
            raise FrigateError("Frigate sublabel update request failed") from exc

    def get_event_snapshot(
        self,
        event_id: str,
        *,
        crop: bool = False,
        quality: int | None = None,
        max_bytes: int | None = None,
    ) -> bytes:
        """Return snapshot bytes, preserving current cropped/uncropped timeouts."""
        params = {}
        if crop:
            params["crop"] = 1
        if quality is not None:
            params["quality"] = quality

        response = None
        try:
            if max_bytes is not None:
                response = requests.get(
                    self._event_url(event_id, "snapshot.jpg"), params=params or None,
                    timeout=(2, 2), stream=True, allow_redirects=False,
                )
                self._require_ok(response, 'snapshot request')
                body = bytearray()
                deadline = time.monotonic() + 5
                for chunk in response.iter_content(chunk_size=8192):
                    body.extend(chunk)
                    if len(body) > max_bytes or time.monotonic() > deadline:
                        raise FrigateError('Frigate snapshot response exceeds limits', status_code=413)
                return bytes(body)
            response = requests.get(
                self._event_url(event_id, "snapshot.jpg"),
                params=params or None,
                timeout=2 if crop else 10,
            )
            self._require_ok(response, "snapshot request")
        except FrigateError:
            raise
        except requests.exceptions.RequestException as exc:
            raise FrigateError("Frigate snapshot request failed") from exc
        finally:
            if max_bytes is not None and response is not None:
                response.close()

        return response.content

    def stream_event_media(
        self,
        event_id: str,
        media_name: str,
        *, timeout=None,
    ) -> requests.Response:
        """Return a streaming response for Frigate event media."""
        timeout = timeout if timeout is not None else (30 if media_name == "clip.mp4" else 5)
        try:
            response = requests.get(
                self._event_url(event_id, media_name),
                stream=True,
                timeout=timeout,
            )
            try:
                self._require_ok(response, "media request")
            except FrigateError:
                response.close()
                raise
        except FrigateError:
            raise
        except requests.exceptions.RequestException as exc:
            raise FrigateError("Frigate media request failed") from exc

        return response

    def check_api_connection(self) -> str:
        """Return a safe diagnostic code from one bounded-size API request.

        The caller owns the wall-clock deadline (including DNS and reads).
        Never expose the potentially credential-bearing URL or response body.
        """
        response = None
        try:
            response = requests.get(
                f"{self.base_url}/api/", timeout=(2, 2), stream=True,
                allow_redirects=False, verify=True,
            )
            status = response.status_code
            if status in (401, 403):
                return "auth_rejected"
            if 300 <= status < 400:
                return "redirect"
            if status != 200:
                return "http_error"
            body = bytearray()
            for chunk in response.iter_content(chunk_size=256):
                body.extend(chunk)
                if len(body) > 1024:
                    return "unexpected_response"
            return "success" if bytes(body).strip() == (
                b"Frigate is running. Alive and healthy!"
            ) else "unexpected_response"
        except requests.exceptions.SSLError:
            return "tls_error"
        except requests.exceptions.Timeout:
            return "timeout"
        except requests.exceptions.RequestException:
            return "connection_error"
        finally:
            if response is not None:
                response.close()

    def get_camera_inventory(self) -> list[dict]:
        """Fetch one config response and retain only camera presentation fields."""
        response = None
        try:
            response = requests.get(
                f"{self.base_url}/api/config", timeout=(2, 2), stream=True,
                allow_redirects=False, verify=True,
            )
            status = response.status_code
            if status in (401, 403):
                raise FrigateDiscoveryError("auth_rejected")
            if 300 <= status < 400:
                raise FrigateDiscoveryError("redirect")
            if status != 200:
                raise FrigateDiscoveryError("http_error")
            body = bytearray()
            for chunk in response.iter_content(chunk_size=8192):
                body.extend(chunk)
                if len(body) > 4 * 1024 * 1024:
                    raise FrigateDiscoveryError("unexpected_response")
            try:
                config = json.loads(body)
            except (ValueError, UnicodeError, RecursionError) as exc:
                raise FrigateDiscoveryError("unexpected_response") from exc
            if not isinstance(config, dict) or not isinstance(config.get("cameras"), dict):
                raise FrigateDiscoveryError("unexpected_response")
            inventory = []
            for camera_id, camera in config["cameras"].items():
                if (
                    not isinstance(camera_id, str) or not camera_id
                    or any(ord(char) < 32 for char in camera_id)
                    or not isinstance(camera, dict)
                ):
                    raise FrigateDiscoveryError("unexpected_response")
                friendly = camera.get("friendly_name")
                if (
                    not isinstance(friendly, str) or not friendly.strip()
                    or len(friendly) > 256 or not friendly.isprintable()
                ):
                    friendly = camera_id
                detection = camera.get("detect")
                detection = detection.get("enabled") if isinstance(detection, dict) else None
                enabled = camera.get("enabled")
                inventory.append({
                    "id": camera_id,
                    "display_name": friendly.strip() if friendly != camera_id else camera_id,
                    "enabled": enabled if isinstance(enabled, bool) else None,
                    "detection_enabled": detection if isinstance(detection, bool) else None,
                })
            return inventory
        except requests.exceptions.SSLError as exc:
            raise FrigateDiscoveryError("tls_error") from exc
        except requests.exceptions.Timeout as exc:
            raise FrigateDiscoveryError("timeout") from exc
        except requests.exceptions.RequestException as exc:
            raise FrigateDiscoveryError("connection_error") from exc
        finally:
            if response is not None:
                response.close()

    def get_camera_stats(self) -> dict:
        """One bounded HTTP read; the observation worker owns the hard deadline."""
        response = None
        try:
            response = requests.get(
                f'{self.base_url}/api/stats', timeout=(2, 2), stream=True,
                allow_redirects=False, verify=True,
            )
            status = response.status_code
            if status in (401, 403):
                raise FrigateDiscoveryError('auth_rejected')
            if 300 <= status < 400:
                raise FrigateDiscoveryError('redirect')
            if status != 200:
                raise FrigateDiscoveryError('http_error')
            body = bytearray()
            for chunk in response.iter_content(chunk_size=8192):
                body.extend(chunk)
                if len(body) > 4 * 1024 * 1024:
                    raise FrigateDiscoveryError('unexpected_response')
            try:
                return normalize_camera_stats(json.loads(body))
            except (ValueError, UnicodeError, RecursionError):
                raise FrigateDiscoveryError('unexpected_response') from None
        except requests.exceptions.SSLError:
            raise FrigateDiscoveryError('tls_error') from None
        except requests.exceptions.Timeout:
            raise FrigateDiscoveryError('timeout') from None
        except requests.exceptions.RequestException:
            raise FrigateDiscoveryError('connection_error') from None
        finally:
            if response is not None:
                response.close()

    def get_version(self) -> str:
        """Return the Frigate version response body."""
        try:
            response = requests.get(f"{self.base_url}/api/version", timeout=5)
            self._require_ok(response, "version request")
        except FrigateError:
            raise
        except requests.exceptions.RequestException as exc:
            raise FrigateError("Frigate version request failed") from exc

        return response.text
