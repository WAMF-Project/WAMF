"""
Tests for the Frigate sub-label fallback feature.

When WAMF's classifier scores below threshold, speciesid falls back to the
Frigate-supplied sub_label (common_name, score) if present. This also covers
the get_scientific_name() reverse lookup including the 20-char truncation
prefix-match strategy.

Heavy ML deps are patched out before importing speciesid — same pattern as
test_new_species.py.
"""
import json
import os
import sqlite3
import sys
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault('WHOSATMYFEEDER_CONFIG', 'config/config.yml.example')

for _mod in [
    "numpy", "cv2",
    "tflite_support",
    "tflite_support.task",
    "tflite_support.task.core",
    "tflite_support.task.processor",
    "tflite_support.task.vision",
    "PIL", "PIL.Image", "PIL.ImageOps",
    "paho", "paho.mqtt", "paho.mqtt.client",
]:
    sys.modules.setdefault(_mod, MagicMock())

import speciesid  # noqa: E402
import app.queries as queries    # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

def _make_det_db(path: str) -> None:
    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE detections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            detection_time TEXT NOT NULL,
            detection_index INTEGER,
            score REAL,
            display_name TEXT,
            category_name TEXT,
            frigate_event TEXT UNIQUE,
            camera_name TEXT,
            wamf_snapshot_path TEXT,
            wamf_clip_path TEXT
        )
    """)
    conn.commit()
    conn.close()


def _make_names_db(path: str, rows: list) -> None:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE birdnames (common_name TEXT, scientific_name TEXT)")
    conn.executemany("INSERT INTO birdnames VALUES (?, ?)", rows)
    conn.commit()
    conn.close()


@pytest.fixture()
def fresh_db(tmp_path):
    db = str(tmp_path / "fresh.db")
    _make_det_db(db)
    return db


@pytest.fixture()
def names_db(tmp_path):
    """Small birdnames DB for get_scientific_name tests."""
    path = str(tmp_path / "birdnames.db")
    _make_names_db(path, [
        ("American Robin",         "Turdus migratorius"),
        ("Blue Jay",               "Cyanocitta cristata"),
        # 22 chars; Frigate truncates to "Black-capped Chickad" (20 chars)
        ("Black-capped Chickadee", "Poecile atricapillus"),
        # Two species sharing the same first 20 chars: "Yellow-rumped Warble"
        ("Yellow-rumped Warbler Eastern", "Setophaga coronata coronata"),
        ("Yellow-rumped Warbler Myrtle",  "Setophaga coronata"),
    ])
    return path


# ---------------------------------------------------------------------------
# get_scientific_name unit tests
# ---------------------------------------------------------------------------

def test_get_scientific_name_exact_match(names_db):
    with patch.object(queries, 'NAMEDBPATH', names_db):
        result = queries.get_scientific_name("American Robin")
    assert result == "Turdus migratorius"


def test_get_scientific_name_not_found(names_db):
    """Unknown name shorter than 20 chars → None, no prefix fallback attempted."""
    with patch.object(queries, 'NAMEDBPATH', names_db):
        result = queries.get_scientific_name("Unknown Bird")
    assert result is None


def test_get_scientific_name_truncated_unambiguous(names_db):
    """Exactly 20-char prefix of a unique species → resolves via prefix match."""
    # "Black-capped Chickadee" (22 chars); [:20] = "Black-capped Chickad"
    truncated = "Black-capped Chickad"
    assert len(truncated) == 20
    with patch.object(queries, 'NAMEDBPATH', names_db):
        result = queries.get_scientific_name(truncated)
    assert result == "Poecile atricapillus"


def test_get_scientific_name_truncated_ambiguous(names_db):
    """20-char prefix matching 2+ species → returns None (avoids false positive)."""
    # "Yellow-rumped Warbler Eastern" and "Yellow-rumped Warbler Myrtle" both
    # start with "Yellow-rumped Warble" (20 chars).
    truncated = "Yellow-rumped Warble"
    assert len(truncated) == 20
    with patch.object(queries, 'NAMEDBPATH', names_db):
        result = queries.get_scientific_name(truncated)
    assert result is None


# ---------------------------------------------------------------------------
# on_message fallback integration tests
# ---------------------------------------------------------------------------

def _run_on_message(fresh_db, wamf_score, sub_label=None, event_id='evt-fallback-001',
                    threshold=0.7, scientific_name_return='Turdus migratorius'):
    """Run on_message with a mocked classifier result and optional Frigate sub_label."""
    speciesid.firstmessage = False
    speciesid.config = {
        'frigate': {
            'camera': ['birdcam'],
            'frigate_url': 'http://localhost:5000',
        },
        'classification': {'threshold': threshold},
    }

    msg = MagicMock()
    msg.payload = json.dumps({
        'type': 'new',
        'after': {
            'camera': 'birdcam',
            'label': 'bird',
            'id': event_id,
            'start_time': 1700000000.0,
            'sub_label': sub_label,
        }
    })

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.content = b'fakejpegdata'

    mock_image = MagicMock()
    mock_image.size = (100, 100)

    fake_category = MagicMock()
    fake_category.index = 42           # not 964 (background)
    fake_category.score = wamf_score
    fake_category.display_name = 'Turdus migratorius'
    fake_category.category_name = 'bird'

    client = MagicMock()

    with patch.object(speciesid, 'DBPATH', fresh_db), \
         patch('speciesid.requests.get', return_value=mock_response), \
         patch('speciesid.Image') as mock_Image, \
         patch('speciesid.classify', return_value=[fake_category]), \
         patch('speciesid.get_common_name', return_value='American Robin'), \
         patch('speciesid.get_scientific_name', return_value=scientific_name_return), \
         patch('speciesid.archive_snapshot', return_value=None), \
         patch('speciesid.archive_clip', return_value=None), \
         patch('speciesid.set_sublabel'), \
         patch('speciesid.publish_new_species') as mock_publish:
        mock_Image.open.return_value = mock_image
        speciesid.on_message(client, None, msg)

    return client, mock_publish


def test_fallback_used_when_wamf_below_threshold(fresh_db):
    """WAMF scores 0.13 (below 0.7); valid sub_label → DB row with category_name='frigate_classified'."""
    _run_on_message(fresh_db, wamf_score=0.13, sub_label=["American Robin", 0.85])

    conn = sqlite3.connect(fresh_db)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT category_name, detection_index FROM detections WHERE frigate_event = 'evt-fallback-001'"
    )
    row = cursor.fetchone()
    conn.close()

    assert row is not None, "Expected a row inserted via fallback"
    assert row[0] == 'frigate_classified'
    assert row[1] == -1


def test_fallback_not_used_when_wamf_above_threshold(fresh_db):
    """WAMF scores 0.92 (above 0.7); sub_label also present → single WAMF row, not frigate_classified."""
    _run_on_message(fresh_db, wamf_score=0.92, sub_label=["American Robin", 0.85])

    conn = sqlite3.connect(fresh_db)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT COUNT(*), category_name FROM detections WHERE frigate_event = 'evt-fallback-001'"
    )
    count, cat = cursor.fetchone()
    conn.close()

    assert count == 1
    assert cat != 'frigate_classified'


def test_fallback_skipped_when_name_unknown(fresh_db):
    """sub_label common name not resolvable → no DB write."""
    _run_on_message(
        fresh_db, wamf_score=0.13,
        sub_label=["Unknown Bird", 0.85],
        scientific_name_return=None,
    )

    conn = sqlite3.connect(fresh_db)
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM detections")
    count = cursor.fetchone()[0]
    conn.close()

    assert count == 0


def test_fallback_used_when_sub_label_is_string(fresh_db):
    """sub_label is a plain string (some Frigate versions) → fallback writes using WAMF's score."""
    _run_on_message(fresh_db, wamf_score=0.67, sub_label="American Robin")

    conn = sqlite3.connect(fresh_db)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT category_name, score FROM detections WHERE frigate_event = 'evt-fallback-001'"
    )
    row = cursor.fetchone()
    conn.close()

    assert row is not None, "Expected a row inserted via string sub_label fallback"
    assert row[0] == 'frigate_classified'
    assert abs(row[1] - 0.67) < 0.001  # score comes from WAMF classifier


def test_fallback_skipped_when_sub_label_null(fresh_db):
    """sub_label is null → no DB write."""
    _run_on_message(fresh_db, wamf_score=0.13, sub_label=None)

    conn = sqlite3.connect(fresh_db)
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM detections")
    count = cursor.fetchone()[0]
    conn.close()

    assert count == 0


def test_fallback_triggers_new_species_mqtt(fresh_db):
    """First ever detection via fallback → publish_new_species called."""
    _, mock_publish = _run_on_message(
        fresh_db, wamf_score=0.13, sub_label=["American Robin", 0.85]
    )
    mock_publish.assert_called_once()


def test_fallback_no_new_species_on_second_detection(fresh_db):
    """Species already in DB → publish_new_species not called on second event."""
    conn = sqlite3.connect(fresh_db)
    conn.execute("""
        INSERT INTO detections
            (detection_time, detection_index, score, display_name, category_name, frigate_event, camera_name, wamf_snapshot_path, wamf_clip_path)
        VALUES ('2024-06-01 08:00:00', -1, 0.80, 'Turdus migratorius', 'frigate_classified', 'evt-prior', 'birdcam', NULL, NULL)
    """)
    conn.commit()
    conn.close()

    _, mock_publish = _run_on_message(
        fresh_db, wamf_score=0.13,
        sub_label=["American Robin", 0.85],
        event_id='evt-fallback-002',
    )
    mock_publish.assert_not_called()
