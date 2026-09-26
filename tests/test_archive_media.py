import logging
from unittest.mock import Mock, call

import pytest
import requests

from app import archive_media
from app.frigate_client import FrigateError


@pytest.fixture
def archive_paths(tmp_path, monkeypatch):
    snapshots = tmp_path / "snapshots"
    clips = tmp_path / "clips"
    snapshots.mkdir()
    clips.mkdir()

    ensure_paths = Mock()
    system_event = Mock()
    monkeypatch.setattr(archive_media, "ensure_storage_paths", ensure_paths)
    monkeypatch.setattr(archive_media, "get_snapshots_path", lambda: snapshots)
    monkeypatch.setattr(archive_media, "get_clips_path", lambda: clips)
    monkeypatch.setattr(archive_media, "log_system_event", system_event)

    return snapshots, clips, ensure_paths, system_event


def test_snapshot_uses_client_and_writes_returned_bytes(
    archive_paths,
    monkeypatch,
):
    snapshots, _, ensure_paths, system_event = archive_paths
    client = Mock()
    client.get_event_snapshot.return_value = b"snapshot bytes"
    client_type = Mock(return_value=client)
    monkeypatch.setattr(archive_media, "FrigateClient", client_type)

    result = archive_media.archive_snapshot(
        "http://configured-frigate:5000/",
        "event-123",
    )

    client_type.assert_called_once_with("http://configured-frigate:5000/")
    client.get_event_snapshot.assert_called_once_with("event-123")
    ensure_paths.assert_called_once_with()
    system_event.assert_not_called()
    assert result == str(snapshots / "event-123.jpg")
    assert (snapshots / "event-123.jpg").read_bytes() == b"snapshot bytes"


@pytest.mark.parametrize("status_code", [None, 503])
def test_snapshot_frigate_failure_preserves_failure_result(
    archive_paths,
    monkeypatch,
    caplog,
    status_code,
):
    snapshots, _, _, system_event = archive_paths
    client = Mock()
    client.get_event_snapshot.side_effect = FrigateError(
        "snapshot unavailable",
        status_code=status_code,
    )
    monkeypatch.setattr(
        archive_media,
        "FrigateClient",
        Mock(return_value=client),
    )

    with caplog.at_level(logging.WARNING, logger=archive_media.__name__):
        result = archive_media.archive_snapshot(
            "http://frigate",
            "event-123",
        )

    assert result is None
    assert not (snapshots / "event-123.jpg").exists()
    system_event.assert_not_called()
    if status_code is None:
        assert "Snapshot archive request error: snapshot unavailable" in caplog.text
    else:
        assert "Snapshot download failed: 503" in caplog.text


def test_clip_streams_chunks_closes_response_and_reports_success(
    archive_paths,
    monkeypatch,
):
    _, clips, ensure_paths, system_event = archive_paths
    response = Mock()
    response.iter_content.return_value = [b"first", b"", b"second"]
    client = Mock()
    client.stream_event_media.return_value = response
    client_type = Mock(return_value=client)
    monkeypatch.setattr(archive_media, "FrigateClient", client_type)

    result = archive_media.archive_clip(
        "http://configured-frigate:5000",
        "event-456",
    )

    client_type.assert_called_once_with("http://configured-frigate:5000")
    client.stream_event_media.assert_called_once_with("event-456", "clip.mp4")
    response.iter_content.assert_called_once_with(chunk_size=8192)
    response.close.assert_called_once_with()
    ensure_paths.assert_called_once_with()
    system_event.assert_called_once_with(
        "INFO",
        "ARCHIVE",
        "Archived clip successfully for event event-456",
    )
    assert result == str(clips / "event-456.mp4")
    assert (clips / "event-456.mp4").read_bytes() == b"firstsecond"


def test_clip_http_status_failure_retries_ten_times(
    archive_paths,
    monkeypatch,
    caplog,
):
    _, clips, _, system_event = archive_paths
    client = Mock()
    client.stream_event_media.side_effect = FrigateError(
        "not ready",
        status_code=503,
    )
    monkeypatch.setattr(
        archive_media,
        "FrigateClient",
        Mock(return_value=client),
    )
    sleep = Mock()
    monkeypatch.setattr(archive_media.time, "sleep", sleep)

    with caplog.at_level(logging.INFO, logger=archive_media.__name__):
        result = archive_media.archive_clip("http://frigate", "event-456")

    assert result is None
    assert client.stream_event_media.call_count == 10
    assert client.stream_event_media.call_args_list == [
        call("event-456", "clip.mp4")
    ] * 10
    assert sleep.call_args_list == [call(2)] * 10
    assert system_event.call_args_list == [
        call(
            "WARN",
            "ARCHIVE",
            f"Clip not ready yet (attempt {attempt}) for event event-456",
        )
        for attempt in range(1, 11)
    ] + [
        call("ERROR", "ARCHIVE", "Clip archive failed for event event-456")
    ]
    assert "Clip download failed for event event-456: 503" in caplog.text
    assert not (clips / "event-456.mp4").exists()


def test_clip_transport_failure_is_immediate(
    archive_paths,
    monkeypatch,
    caplog,
):
    _, clips, _, system_event = archive_paths
    client = Mock()
    client.stream_event_media.side_effect = FrigateError("connection refused")
    monkeypatch.setattr(
        archive_media,
        "FrigateClient",
        Mock(return_value=client),
    )
    sleep = Mock()
    monkeypatch.setattr(archive_media.time, "sleep", sleep)

    with caplog.at_level(logging.WARNING, logger=archive_media.__name__):
        result = archive_media.archive_clip("http://frigate", "event-456")

    assert result is None
    client.stream_event_media.assert_called_once_with("event-456", "clip.mp4")
    sleep.assert_not_called()
    system_event.assert_not_called()
    assert "Clip archive request error: connection refused" in caplog.text
    assert not (clips / "event-456.mp4").exists()


def test_clip_iteration_failure_closes_and_retains_partial_file(
    archive_paths,
    monkeypatch,
    caplog,
):
    _, clips, _, system_event = archive_paths
    response = Mock()

    def failing_chunks(*, chunk_size):
        assert chunk_size == 8192
        yield b"partial data"
        raise requests.ConnectionError("stream interrupted")

    response.iter_content.side_effect = failing_chunks
    client = Mock()
    client.stream_event_media.return_value = response
    monkeypatch.setattr(
        archive_media,
        "FrigateClient",
        Mock(return_value=client),
    )

    with caplog.at_level(logging.WARNING, logger=archive_media.__name__):
        result = archive_media.archive_clip("http://frigate", "event-456")

    assert result is None
    response.close.assert_called_once_with()
    system_event.assert_not_called()
    assert "Clip archive request error: stream interrupted" in caplog.text
    assert (clips / "event-456.mp4").read_bytes() == b"partial data"


def test_clip_write_failure_closes_and_retains_partial_file(
    archive_paths,
    monkeypatch,
    caplog,
):
    _, clips, _, system_event = archive_paths
    response = Mock()
    response.iter_content.return_value = [b"partial data"]
    client = Mock()
    client.stream_event_media.return_value = response
    monkeypatch.setattr(
        archive_media,
        "FrigateClient",
        Mock(return_value=client),
    )

    class FailingWriter:
        def __enter__(self):
            self.file = (clips / "event-456.mp4").open("wb")
            return self

        def write(self, data):
            self.file.write(data)
            self.file.flush()
            raise OSError("disk full")

        def __exit__(self, *args):
            self.file.close()

    monkeypatch.setattr("builtins.open", Mock(return_value=FailingWriter()))

    with caplog.at_level(logging.WARNING, logger=archive_media.__name__):
        result = archive_media.archive_clip("http://frigate", "event-456")

    assert result is None
    response.close.assert_called_once_with()
    system_event.assert_not_called()
    assert "Clip archive file error: disk full" in caplog.text
    assert (clips / "event-456.mp4").read_bytes() == b"partial data"
