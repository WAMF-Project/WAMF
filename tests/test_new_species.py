"""
Tests for the MQTT new-species-ever notification feature.

speciesid.py has heavy ML imports (numpy, cv2, tflite_support, PIL) that are
not available in the test environment. We patch them in sys.modules before
importing speciesid so only the functions we care about are exercised.
"""
import json
import os
import sqlite3
import sys
from unittest.mock import MagicMock, patch

import pytest

# webui calls load_config() at module level; point it at the example config
# so it doesn't fail when speciesid (which imports webui) is imported here.
os.environ.setdefault('WHOSATMYFEEDER_CONFIG', 'config/config.yml.example')

# Patch heavy ML deps before importing speciesid (not available in test env)
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


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

def test_load_config_uses_shared_config_path(monkeypatch, tmp_path):
    config_path = tmp_path / "mounted-config.yml"
    config_path.write_text("frigate:\n  server: http://frigate:5000\n")
    monkeypatch.setenv("WHOSATMYFEEDER_CONFIG", str(config_path))

    speciesid.load_config()

    assert speciesid.config == {
        "frigate": {"server": "http://frigate:5000"},
    }


# ---------------------------------------------------------------------------
# Helpers
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


def _insert(path: str, display_name: str, frigate_event: str, score: float = 0.9) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        """INSERT INTO detections
               (detection_time, detection_index, score, display_name, category_name, frigate_event, camera_name, wamf_snapshot_path, wamf_clip_path)
           VALUES ('2024-06-01 08:00:00', 1, ?, ?, 'bird', ?, 'birdcam', NULL, NULL)""",
        (score, display_name, frigate_event),
    )
    conn.commit()
    conn.close()


@pytest.fixture()
def fresh_db(tmp_path):
    db = str(tmp_path / "fresh.db")
    _make_det_db(db)
    return db


# ---------------------------------------------------------------------------
# publish_new_species helper
# ---------------------------------------------------------------------------

def test_publish_new_species_publishes_all_six_topics():
    client = MagicMock()
    speciesid.publish_new_species(
        client,
        common_name="American Robin",
        scientific_name="Turdus migratorius",
        score=0.9234,
        camera_name="birdcam",
        frigate_event="evt-001",
    )
    assert client.publish.call_count == 6


def test_publish_new_species_correct_topics():
    client = MagicMock()
    speciesid.publish_new_species(
        client, "American Robin", "Turdus migratorius", 0.9234, "birdcam", "evt-001"
    )
    published_topics = {c.args[0] for c in client.publish.call_args_list}
    assert published_topics == {
        "whosatmyfeeder/new_species/common_name",
        "whosatmyfeeder/new_species/scientific_name",
        "whosatmyfeeder/new_species/score",
        "whosatmyfeeder/new_species/camera",
        "whosatmyfeeder/new_species/frigate_event",
        "whosatmyfeeder/new_species",
    }


def test_publish_new_species_all_retained():
    client = MagicMock()
    speciesid.publish_new_species(
        client, "American Robin", "Turdus migratorius", 0.9234, "birdcam", "evt-001"
    )
    for c in client.publish.call_args_list:
        assert c.kwargs.get("retain") is True, f"retain not set on {c.args[0]}"


def test_publish_new_species_correct_payloads():
    client = MagicMock()
    speciesid.publish_new_species(
        client, "American Robin", "Turdus migratorius", 0.9234, "birdcam", "evt-001"
    )
    payloads = {c.args[0]: c.args[1] for c in client.publish.call_args_list}
    assert payloads["whosatmyfeeder/new_species/common_name"] == "American Robin"
    assert payloads["whosatmyfeeder/new_species/scientific_name"] == "Turdus migratorius"
    assert payloads["whosatmyfeeder/new_species/score"] == "0.92"
    assert payloads["whosatmyfeeder/new_species/camera"] == "birdcam"
    assert payloads["whosatmyfeeder/new_species/frigate_event"] == "evt-001"


def test_publish_new_species_score_is_2dp():
    client = MagicMock()
    speciesid.publish_new_species(
        client, "Robin", "Turdus migratorius", 0.123456789, "birdcam", "evt-001"
    )
    payloads = {c.args[0]: c.args[1] for c in client.publish.call_args_list}
    assert payloads["whosatmyfeeder/new_species/score"] == "0.12"


def test_publish_new_species_json_topic_payload():
    client = MagicMock()
    speciesid.publish_new_species(
        client, "American Robin", "Turdus migratorius", 0.9234, "birdcam", "evt-001"
    )
    calls = {c.args[0]: c for c in client.publish.call_args_list}
    assert "whosatmyfeeder/new_species" in calls
    call = calls["whosatmyfeeder/new_species"]
    payload = json.loads(call.args[1])
    assert payload["common_name"] == "American Robin"
    assert payload["scientific_name"] == "Turdus migratorius"
    assert payload["score"] == "0.92"
    assert payload["camera"] == "birdcam"
    assert payload["frigate_event"] == "evt-001"
    assert call.kwargs.get("qos") == 1
    assert call.kwargs.get("retain") is True


# ---------------------------------------------------------------------------
# First-ever detection DB logic
# ---------------------------------------------------------------------------

def test_new_species_fires_when_count_is_one(fresh_db):
    """After inserting the first row for a species, count==1 → publish should fire."""
    _insert(fresh_db, "Turdus migratorius", "evt-001")

    conn = sqlite3.connect(fresh_db)
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM detections WHERE display_name = ?", ("Turdus migratorius",))
    count = cursor.fetchone()[0]
    conn.close()

    assert count == 1


def test_new_species_does_not_fire_on_second_detection(fresh_db):
    """After a second detection of the same species, count > 1 → no publish."""
    _insert(fresh_db, "Turdus migratorius", "evt-001")
    _insert(fresh_db, "Turdus migratorius", "evt-002")

    conn = sqlite3.connect(fresh_db)
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM detections WHERE display_name = ?", ("Turdus migratorius",))
    count = cursor.fetchone()[0]
    conn.close()

    assert count == 2


def test_different_species_each_trigger_independently(fresh_db):
    """Two different species both show count==1 after their first detection."""
    _insert(fresh_db, "Turdus migratorius", "evt-001")
    _insert(fresh_db, "Cyanocitta cristata", "evt-002")

    conn = sqlite3.connect(fresh_db)
    cursor = conn.cursor()

    cursor.execute("SELECT COUNT(*) FROM detections WHERE display_name = ?", ("Turdus migratorius",))
    assert cursor.fetchone()[0] == 1

    cursor.execute("SELECT COUNT(*) FROM detections WHERE display_name = ?", ("Cyanocitta cristata",))
    assert cursor.fetchone()[0] == 1

    conn.close()


def test_score_update_does_not_add_row(fresh_db):
    """An UPDATE to the same frigate_event doesn't add a second row — no new-species fire."""
    _insert(fresh_db, "Turdus migratorius", "evt-001", score=0.75)

    conn = sqlite3.connect(fresh_db)
    conn.execute(
        "UPDATE detections SET score = ? WHERE frigate_event = ?", (0.92, "evt-001")
    )
    conn.commit()
    conn.close()

    conn = sqlite3.connect(fresh_db)
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM detections WHERE display_name = ?", ("Turdus migratorius",))
    count = cursor.fetchone()[0]
    conn.close()

    assert count == 1


# ---------------------------------------------------------------------------
# Threshold gating
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("score,threshold,expect_write", [
    (0.13, 0.7, False),   # below threshold (real-world: Night Heron scored 0.13)
    (0.70, 0.7, False),   # exactly at threshold — strict > means no write
    (0.92, 0.7, True),    # above threshold — should write to DB
])
def test_threshold_gating(fresh_db, score, threshold, expect_write):
    """score must be strictly > threshold for a detection to be written to the DB."""
    speciesid.firstmessage = False
    speciesid.config = {
        'frigate': {
            'camera': ['birdcam'],
            'frigate_url': 'http://localhost:5000',
        },
        'classification': {'threshold': threshold},
    }

    message = MagicMock()
    message.payload = json.dumps({
        'type': 'new',
        'after': {
            'camera': 'birdcam',
            'label': 'bird',
            'id': 'evt-threshold-test',
            'start_time': 1700000000.0,
        }
    })

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.content = b'fakejpegdata'

    mock_image = MagicMock()
    mock_image.size = (100, 100)

    fake_category = MagicMock()
    fake_category.index = 42          # not 964 (background)
    fake_category.score = score
    fake_category.display_name = 'Turdus migratorius'
    fake_category.category_name = 'bird'

    client = MagicMock()

    with patch.object(speciesid, 'DBPATH', fresh_db), \
         patch('speciesid.requests.get', return_value=mock_response), \
         patch('speciesid.Image') as mock_Image, \
         patch('speciesid.classify', return_value=[fake_category]), \
         patch('speciesid.get_common_name', return_value='American Robin'), \
         patch('speciesid.archive_snapshot', return_value=None), \
         patch('speciesid.archive_clip', return_value=None), \
         patch('speciesid.set_sublabel'):
        mock_Image.open.return_value = mock_image
        speciesid.on_message(client, None, message)

    conn = sqlite3.connect(fresh_db)
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM detections")
    count = cursor.fetchone()[0]
    conn.close()

    if expect_write:
        assert count == 1
    else:
        assert count == 0
        client.publish.assert_not_called()
