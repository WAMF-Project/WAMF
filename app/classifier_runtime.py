"""Small loaded-classifier observation carried by MQTT worker diagnostics."""

import hashlib
from dataclasses import dataclass


@dataclass(frozen=True)
class ClassifierRuntimeSnapshot:
    state: str = "unknown"
    model_id: str | None = None
    model_version: str | None = None
    path: str | None = None
    sha256: str | None = None
    loaded_at: str | None = None


def terminal_model_identity(snapshot):
    """Use the actually loaded model, never a newly saved Settings selection."""
    if not isinstance(snapshot, ClassifierRuntimeSnapshot) or snapshot.state != 'ready' or not snapshot.sha256:
        return None
    label = f'{snapshot.model_id}@{snapshot.model_version}' if snapshot.model_id else 'custom'
    return f'{label}:sha256:{snapshot.sha256}'


def loaded_classifier(selection):
    """Called only after successful initialization, never from saved config."""
    from datetime import UTC, datetime

    digest = hashlib.sha256()
    with selection.path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(64 * 1024), b""):
            digest.update(chunk)
    artifact = selection.artifact
    return ClassifierRuntimeSnapshot(
        state="ready", model_id=artifact.model_id if artifact else None,
        model_version=artifact.version if artifact else None,
        path=str(selection.path.resolve()), sha256=digest.hexdigest(),
        loaded_at=datetime.now(UTC).isoformat(),
    )
