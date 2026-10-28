"""Log values must remain diagnostic data rather than forge log records."""

import json
import logging
import sqlite3
from unittest.mock import Mock
from urllib.parse import quote

import pytest

from app.logging_utils import sanitize_log_value


MALICIOUS = "input\r\nFORGED\t\x00\x07\x08\x1b[2J\x7f\x85\u2028\u2029\u202e"


@pytest.mark.parametrize("value", [
    "Turdus migratorius", "Grünfink", MALICIOUS, r"literal\n and \u001b",
    'quotes " and \' brackets [] percent %s', None, sqlite3.Error(MALICIOUS),
] + [chr(code) for code in range(32)] + [chr(code) for code in range(127, 160)])
def test_sanitize_log_value_preserves_data_without_controls(value):
    encoded = sanitize_log_value(value)
    assert json.loads(encoded) == str(value)
    assert all(32 <= ord(char) <= 126 for char in encoded)
    assert len(encoded.splitlines()) == 1


def _assert_safe_record(record, original_values):
    assert [json.loads(value) for value in record.args] == original_values
    assert all(32 <= ord(char) <= 126 for char in record.getMessage())
    assert r"\r\nFORGED" in record.getMessage()
    assert r"\u001b[2J" in record.getMessage()


@pytest.mark.parametrize("failure", ["database", "media"])
def test_detection_error_logs_escape_identifiers_and_exceptions(
    flask_client, monkeypatch, caplog, failure
):
    import routes.detections as detection_routes
    import webui

    event = "evt-" + MALICIOUS
    error = sqlite3.OperationalError(MALICIOUS) if failure == "database" else OSError(MALICIOUS)
    connection = Mock()
    cursor = connection.cursor.return_value
    cursor.fetchone.return_value = {"wamf_snapshot_path": "snapshot", "wamf_clip_path": "clip"}
    monkeypatch.setattr(detection_routes, "connect_db", Mock(return_value=connection))
    delete_media = Mock(side_effect=error)
    monkeypatch.setattr(webui, "delete_wamf_media_files", delete_media)
    if failure == "database":
        cursor.execute.side_effect = error

    with caplog.at_level(logging.WARNING, logger="webui"):
        response = flask_client.delete("/detections/" + quote(event, safe=""))

    assert response.status_code == 500
    assert response.get_json() == {
        "success": False,
        "message": "Unable to delete detection." if failure == "database" else "Unable to delete detection media.",
    }
    records = [record for record in caplog.records if record.name == "webui"]
    assert len(records) == 1
    _assert_safe_record(records[0], [event, str(error)])
    assert cursor.execute.call_args.args[1] == (event,)
    connection.commit.assert_not_called()
    connection.close.assert_called_once_with()
    if failure == "media":
        delete_media.assert_called_once_with("snapshot", "clip")
    else:
        delete_media.assert_not_called()


def test_public_route_logs_escape_scientific_name_and_date(flask_client, caplog):
    scientific_name = "species-" + MALICIOUS
    date = "date-" + MALICIOUS
    # The existing date-range response occurs after both diagnostic log calls.
    path = (
        "/detections/by_scientific_name/" + quote(scientific_name, safe="")
        + "/" + quote(date, safe="") + "/2024-06-02"
    )
    with caplog.at_level(logging.DEBUG, logger="webui"):
        response = flask_client.get(path)

    assert response.status_code == 501
    assert response.get_json() == {"error": "Date range queries are not yet implemented."}
    records = [record for record in caplog.records if record.name == "webui"]
    assert len(records) == 2
    _assert_safe_record(records[0], [scientific_name])
    _assert_safe_record(records[1], [date])


def test_common_name_log_is_safe_without_changing_lookup(
    patched_queries, tmp_path, monkeypatch, caplog
):
    scientific_name = "species-" + MALICIOUS
    db_path = tmp_path / "names.db"
    with sqlite3.connect(db_path) as connection:
        connection.execute("CREATE TABLE birdnames (scientific_name TEXT, common_name TEXT)")
    monkeypatch.setattr(patched_queries, "NAMEDBPATH", str(db_path))

    with caplog.at_level(logging.DEBUG, logger="app.queries"):
        assert patched_queries.get_common_name(scientific_name) is None

    records = [record for record in caplog.records if record.name == "app.queries"]
    assert len(records) == 1
    _assert_safe_record(records[0], [scientific_name])

    # A matching row must still be found using the original, unsanitized value.
    with sqlite3.connect(db_path) as connection:
        connection.execute("INSERT INTO birdnames VALUES (?, ?)", (scientific_name, "Test bird"))
    assert patched_queries.get_common_name(scientific_name) == "Test bird"
