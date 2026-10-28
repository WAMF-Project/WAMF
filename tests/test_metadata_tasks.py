"""Background refresh diagnostics must not alter log-record boundaries."""

import json
import logging
from unittest.mock import Mock, call

import pytest
import requests

from app import metadata_tasks


@pytest.mark.parametrize("error_type", [requests.RequestException, KeyError, ValueError])
@pytest.mark.parametrize("scientific_name,error_message", [
    ("Turdus migratorius", "Request failed"),
    ("Turdus\rFORGED", "Error\rFORGED"),
    ("Turdus\nFORGED", "Error\nFORGED"),
    ("Turdus\x1b[31mFORGED", "Error\x1b[2J"),
    ("Turdus migratorius", "Error\r\nFORGED\x1b[2J\t\x00\x7f\x85\u2028\u202e"),
    ("Turdus\r\nFORGED\x1b[2J", "Request failed"),
])
def test_worker_failure_logs_are_safe_and_processing_continues(
    monkeypatch, caplog, error_type, scientific_name, error_message
):
    error = error_type(error_message)
    next_species = "Corvus corax"
    work_queue = Mock()
    # Stop deterministically after one failure and the next successful job.
    work_queue.get.side_effect = [scientific_name, next_species, StopIteration]
    queued = {scientific_name, next_species}
    get_info = Mock(return_value=None)
    refresh = Mock(side_effect=[error, None])
    system_event = Mock()
    monkeypatch.setattr(metadata_tasks, "_metadata_queue", work_queue)
    monkeypatch.setattr(metadata_tasks, "_queued_species", queued)
    monkeypatch.setattr(metadata_tasks, "get_species_info", get_info)
    monkeypatch.setattr(metadata_tasks, "refresh_species_metadata", refresh)
    monkeypatch.setattr(metadata_tasks, "log_system_event", system_event)

    with caplog.at_level(logging.WARNING, logger="app.metadata_tasks"):
        with pytest.raises(StopIteration):
            metadata_tasks._metadata_worker()

    records = [record for record in caplog.records if record.name == "app.metadata_tasks"]
    assert len(records) == 1
    record = records[0]
    assert record.levelno == logging.WARNING
    assert record.msg == "Metadata refresh failed for %s: %s"
    assert [json.loads(value) for value in record.args] == [scientific_name, str(error)]
    assert all(32 <= ord(char) <= 126 for char in record.getMessage())
    assert len(record.getMessage().splitlines()) == 1

    # Logging sanitization must not change refresh inputs, events or cleanup.
    assert get_info.call_args_list == [call(scientific_name), call(next_species)]
    assert refresh.call_args_list == [call(scientific_name), call(next_species)]
    assert system_event.call_args_list == [
        call("WARN", "SPECIES", f"Metadata refresh failed for {scientific_name}"),
        call("INFO", "SPECIES", f"Refreshed metadata for {next_species}"),
    ]
    assert work_queue.task_done.call_count == 2
    assert queued == set()

    # Failure must release deduplication state, allowing a later retry request.
    start_worker = Mock()
    monkeypatch.setattr(metadata_tasks, "start_metadata_worker", start_worker)
    assert metadata_tasks.queue_metadata_refresh(scientific_name) is True
    start_worker.assert_called_once_with()
    work_queue.put.assert_called_once_with(scientific_name)
    assert queued == {scientific_name}
