"""Small HTTP boundary for the Frigate operations used by WAMF."""

import json

import requests


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


class FrigateClient:
    """Client for the subset of the Frigate HTTP API currently used by WAMF."""

    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")

    def _event_url(self, event_id: str, resource: str) -> str:
        return f"{self.base_url}/api/events/{event_id}/{resource}"

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
    ) -> bytes:
        """Return snapshot bytes, preserving current cropped/uncropped timeouts."""
        params = {}
        if crop:
            params["crop"] = 1
        if quality is not None:
            params["quality"] = quality

        try:
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

        return response.content

    def stream_event_media(
        self,
        event_id: str,
        media_name: str,
    ) -> requests.Response:
        """Return a streaming response for Frigate event media."""
        timeout = 30 if media_name == "clip.mp4" else 5
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
            except (ValueError, UnicodeError) as exc:
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
