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
