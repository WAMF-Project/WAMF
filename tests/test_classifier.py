import json
from contextlib import contextmanager
from pathlib import Path
import sqlite3
import threading
from unittest.mock import MagicMock, patch

import numpy as np
from PIL import Image
import pytest

import speciesid


REPO_ROOT = Path(__file__).resolve().parent.parent
MODEL_PATH = REPO_ROOT / 'model.tflite'
GOLDEN_SNAPSHOT_PATH = (
    REPO_ROOT / 'media/wamf/snapshots/1779981851.294031-5nvuxv.jpg'
)


class FakeInterpreter:
    def __init__(self, *, model_path, num_threads):
        self.model_path = model_path
        self.num_threads = num_threads
        self.allocate_calls = 0
        self.invoke_calls = 0
        self.input = None
        self.output = np.zeros((1, 965), dtype=np.uint8)
        self.output[0, 665] = 22

    def allocate_tensors(self):
        self.allocate_calls += 1

    def get_input_details(self):
        return [{
            'index': 170,
            'shape': np.array([1, 224, 224, 3]),
            'dtype': np.uint8,
            'quantization': (0.0078125, 128),
        }]

    def get_output_details(self):
        return [{
            'index': 171,
            'shape': np.array([1, 965]),
            'dtype': np.uint8,
            'quantization': (0.00390625, 0),
        }]

    def set_tensor(self, index, value):
        assert index == 170
        self.input = value

    def invoke(self):
        self.invoke_calls += 1

    def get_tensor(self, index):
        assert index == 171
        return self.output


def test_litert_initialization_and_category_compatibility():
    with patch('speciesid.Interpreter', FakeInterpreter):
        interpreter = speciesid.initialize_classifier(MODEL_PATH)

    image = np.zeros((224, 224, 3), dtype=np.uint8)
    categories = speciesid.classify(image)

    assert interpreter.model_path == str(MODEL_PATH)
    assert interpreter.num_threads == 4
    assert interpreter.allocate_calls == 1
    assert interpreter.invoke_calls == 1
    assert interpreter.input.shape == (1, 224, 224, 3)
    assert interpreter.input.flags.c_contiguous
    assert categories == [speciesid.Category(
        index=665,
        score=0.0859375,
        display_name='Cyanistes caeruleus',
        category_name='/m/01kvvt',
    )]


def test_classifier_filters_sorts_and_limits_results():
    with patch('speciesid.Interpreter', FakeInterpreter):
        interpreter = speciesid.initialize_classifier(MODEL_PATH)

    interpreter.output.fill(0)
    interpreter.output[0, 10] = 12  # Below the 0.05 threshold.
    interpreter.output[0, 20] = 13
    interpreter.output[0, 30] = 14
    interpreter.output[0, 40] = 15
    interpreter.output[0, 50] = 16
    interpreter.output[0, 60] = 17
    interpreter.output[0, 70] = 18

    categories = speciesid.classify(
        np.zeros((224, 224, 3), dtype=np.uint8)
    )

    assert [category.index for category in categories] == [70, 60, 50, 40, 30]
    assert len(categories) == speciesid.CLASSIFIER_MAX_RESULTS
    assert all(
        category.score >= speciesid.CLASSIFIER_SCORE_THRESHOLD
        for category in categories
    )
    assert [category.score for category in categories] == sorted(
        (category.score for category in categories), reverse=True
    )
    assert 10 not in [category.index for category in categories]


def test_event_pipeline_directly_resizes_golden_snapshot(tmp_path):
    frigate_client = MagicMock()
    frigate_client.get_event_snapshot.return_value = (
        GOLDEN_SNAPSHOT_PATH.read_bytes()
    )
    message = MagicMock(
        retain=False,
        topic='frigate/events',
        payload=json.dumps({
            'type': 'new',
            'after': {
                'camera': 'birdcam',
                'label': 'bird',
                'id': 'golden-preprocessing',
                'start_time': 1700000000.0,
            },
        }),
    )
    background = speciesid.Category(
        index=964,
        score=0.82421875,
        display_name='None',
        category_name='__background__',
    )
    config = {
        'frigate': {
            'camera': ['birdcam'],
            'frigate_url': 'http://localhost:5000',
        },
        'mqtt': {'topic_prefix': 'frigate'},
        'classification': {'threshold': 0.7},
    }

    with patch.object(speciesid, 'config', config), patch.object(
        speciesid, 'DBPATH', tmp_path / 'speciesid.db'
    ), patch.object(
        speciesid, 'frigate_client', frigate_client
    ), patch(
        'speciesid.classify', return_value=[background]
    ) as classify:
        speciesid._on_message_inner(MagicMock(), None, message)

    frigate_client.get_event_snapshot.assert_called_once_with(
        'golden-preprocessing',
        crop=True,
        quality=95,
    )

    classified_image = classify.call_args.args[0]
    source = Image.open(GOLDEN_SNAPSHOT_PATH).convert('RGB')
    expected = np.ascontiguousarray(
        np.array(source.resize((224, 224)), dtype=np.uint8)
    )

    thumbnail = source.copy()
    thumbnail.thumbnail((224, 224))
    letterboxed = Image.new('RGB', (224, 224), (0, 0, 0))
    letterboxed.paste(
        thumbnail,
        ((224 - thumbnail.width) // 2, (224 - thumbnail.height) // 2),
    )
    letterboxed = np.ascontiguousarray(
        np.array(letterboxed, dtype=np.uint8)
    )

    assert classified_image.shape == (224, 224, 3)
    assert classified_image.dtype == np.uint8
    assert classified_image.flags.c_contiguous
    assert np.array_equal(classified_image, expected)
    assert not np.array_equal(classified_image, letterboxed)


def test_golden_image_matches_existing_classification_and_name_database():
    speciesid.initialize_classifier(MODEL_PATH)
    image = Image.open(
        GOLDEN_SNAPSHOT_PATH
    ).convert('RGB').resize((224, 224))

    categories = speciesid.classify(np.asarray(image, dtype=np.uint8))

    assert categories == [speciesid.Category(
        index=665,
        score=0.0859375,
        display_name='Cyanistes caeruleus',
        category_name='/m/01kvvt',
    )]
    with patch('app.queries.NAMEDBPATH', str(REPO_ROOT / 'birdnames.db')):
        assert speciesid.get_common_name(categories[0].display_name) == 'Eurasian Blue Tit'


def _qualifying_message(event_id):
    return MagicMock(
        retain=False,
        topic="frigate/events",
        payload=json.dumps({
            "type": "new",
            "after": {
                "camera": "birdcam",
                "label": "bird",
                "id": event_id,
                "start_time": 1700000000.0,
            },
        }),
    )


def _qualifying_category(score=0.92):
    return speciesid.Category(
        index=42,
        score=score,
        display_name="Turdus migratorius",
        category_name="bird",
    )


def test_live_archive_guard_starts_after_classification_and_ends_at_commit(tmp_path):
    fresh_db = tmp_path / "speciesid.db"
    speciesid.ensure_schema(fresh_db)
    events = []
    guard_active = False
    event_id = "evt-guard-scope"
    client = MagicMock()
    frigate = MagicMock()

    def get_snapshot(*args, **kwargs):
        events.append("classification_snapshot")
        return b"fakejpegdata"

    def classify(image):
        events.append("classify")
        return [_qualifying_category()]

    def get_common_name(scientific_name):
        events.append("name_lookup")
        return "American Robin"

    def publish(topic, *args, **kwargs):
        assert not guard_active
        assert topic == "whosatmyfeeder/detections"
        events.append("detection_publish")

    @contextmanager
    def guard(database_path):
        nonlocal guard_active
        assert events == [
            "classification_snapshot",
            "classify",
            "name_lookup",
            "detection_publish",
        ]
        assert database_path == fresh_db
        guard_active = True
        events.append("guard_enter")
        try:
            yield
        finally:
            with sqlite3.connect(fresh_db) as conn:
                row = conn.execute(
                    "SELECT wamf_snapshot_path, wamf_clip_path "
                    "FROM detections WHERE frigate_event = ?",
                    (event_id,),
                ).fetchone()
            assert row == ("snapshot.jpg", "clip.mp4")
            guard_active = False
            events.append("guard_exit")

    def archive_snapshot(*args):
        assert guard_active
        events.append("archive_snapshot")
        return "snapshot.jpg"

    def archive_clip(*args):
        assert guard_active
        events.append("archive_clip")
        return "clip.mp4"

    def post_commit(name):
        def record(*args, **kwargs):
            assert not guard_active
            events.append(name)
        return record

    frigate.get_event_snapshot.side_effect = get_snapshot
    client.publish.side_effect = publish
    config = {
        "frigate": {
            "camera": ["birdcam"],
            "frigate_url": "http://localhost:5000",
        },
        "classification": {"threshold": 0.7},
        "bridge": {},
    }

    with patch.object(speciesid, "config", config), patch.object(
        speciesid, "DBPATH", fresh_db
    ), patch.object(
        speciesid, "frigate_client", frigate
    ), patch("speciesid.Image") as image, patch.object(
        speciesid, "classify", side_effect=classify
    ), patch.object(
        speciesid, "get_common_name", side_effect=get_common_name
    ), patch.object(
        speciesid, "media_activity_guard", guard
    ), patch.object(
        speciesid, "archive_snapshot", side_effect=archive_snapshot
    ), patch.object(
        speciesid, "archive_clip", side_effect=archive_clip
    ), patch.object(
        speciesid, "set_sublabel", side_effect=post_commit("sublabel")
    ), patch.object(
        speciesid,
        "publish_new_species",
        side_effect=post_commit("new_species"),
    ), patch.object(
        speciesid,
        "post_observation_event",
        side_effect=post_commit("bridge"),
    ):
        speciesid._on_message_inner(client, None, _qualifying_message(event_id))

    assert events == [
        "classification_snapshot",
        "classify",
        "name_lookup",
        "detection_publish",
        "guard_enter",
        "archive_snapshot",
        "archive_clip",
        "guard_exit",
        "sublabel",
        "new_species",
        "bridge",
    ]


def test_qualifying_detection_rechecks_existing_row_after_guard_acquisition(tmp_path):
    fresh_db = tmp_path / "speciesid.db"
    speciesid.ensure_schema(fresh_db)
    event_id = "evt-created-while-waiting"

    @contextmanager
    def guard(database_path):
        with sqlite3.connect(fresh_db) as conn:
            conn.execute(
                """
                INSERT INTO detections (
                    detection_time, detection_index, score, display_name,
                    category_name, frigate_event, camera_name
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "2023-11-14 22:13:20",
                    42,
                    0.95,
                    "Turdus migratorius",
                    "bird",
                    event_id,
                    "birdcam",
                ),
            )
        yield

    after_data = {"camera": "birdcam"}
    with patch.object(speciesid, "DBPATH", fresh_db), patch.object(
        speciesid, "media_activity_guard", guard
    ), patch.object(speciesid, "archive_snapshot") as snapshot, patch.object(
        speciesid, "archive_clip"
    ) as clip, patch.object(speciesid, "set_sublabel") as sublabel:
        speciesid._process_qualifying_detection(
            MagicMock(),
            after_data,
            event_id,
            "http://localhost:5000",
            "2023-11-14 22:13:20",
            42,
            0.92,
            "Turdus migratorius",
            "bird",
            "American Robin",
        )

    snapshot.assert_not_called()
    clip.assert_not_called()
    sublabel.assert_not_called()
    with sqlite3.connect(fresh_db) as conn:
        rows = conn.execute(
            "SELECT score FROM detections WHERE frigate_event = ?",
            (event_id,),
        ).fetchall()
    assert rows == [(0.95,)]


def test_media_guard_blocks_scan_until_archive_reference_commit(tmp_path):
    fresh_db = tmp_path / "speciesid.db"
    speciesid.ensure_schema(fresh_db)
    event_id = "evt-precommit-window"
    snapshot_path = tmp_path / "snapshots" / f"{event_id}.jpg"
    snapshot_path.parent.mkdir()
    archive_created = threading.Event()
    commit_waiting = threading.Event()
    allow_commit = threading.Event()
    scan_attempted = threading.Event()
    scan_entered = threading.Event()
    errors = []
    observed_rows = []
    real_connect_db = speciesid.connect_db

    class PausingConnection:
        def __init__(self, connection):
            self.connection = connection

        def __getattr__(self, name):
            return getattr(self.connection, name)

        def commit(self):
            commit_waiting.set()
            if not allow_commit.wait(2):
                raise AssertionError("test did not release the paused commit")
            self.connection.commit()

    def connect_with_paused_commit(database_path, row_factory=False):
        return PausingConnection(
            real_connect_db(database_path, row_factory=row_factory)
        )

    def archive_snapshot(*args):
        snapshot_path.write_bytes(b"snapshot")
        archive_created.set()
        return str(snapshot_path)

    def writer():
        try:
            speciesid._process_qualifying_detection(
                MagicMock(),
                {"camera": "birdcam"},
                event_id,
                "http://localhost:5000",
                "2023-11-14 22:13:20",
                42,
                0.92,
                "Turdus migratorius",
                "bird",
                "American Robin",
            )
        except BaseException as exc:
            errors.append(exc)

    def scanner():
        try:
            scan_attempted.set()
            with speciesid.media_activity_guard(fresh_db):
                scan_entered.set()
                with sqlite3.connect(fresh_db) as conn:
                    observed_rows.extend(conn.execute(
                        "SELECT wamf_snapshot_path FROM detections "
                        "WHERE frigate_event = ?",
                        (event_id,),
                    ).fetchall())
        except BaseException as exc:
            errors.append(exc)

    with patch.object(speciesid, "DBPATH", fresh_db), patch.object(
        speciesid, "config", {"bridge": {}}
    ), patch.object(
        speciesid, "connect_db", side_effect=connect_with_paused_commit
    ), patch.object(
        speciesid, "archive_snapshot", side_effect=archive_snapshot
    ), patch.object(
        speciesid, "archive_clip", return_value=None
    ), patch.object(speciesid, "set_sublabel"), patch.object(
        speciesid, "publish_new_species"
    ), patch.object(speciesid, "post_observation_event"):
        writer_thread = threading.Thread(target=writer)
        writer_thread.start()
        assert archive_created.wait(1)
        assert commit_waiting.wait(1)

        scanner_thread = threading.Thread(target=scanner)
        scanner_thread.start()
        assert scan_attempted.wait(1)
        assert not scan_entered.wait(0.05)

        allow_commit.set()
        writer_thread.join(2)
        scanner_thread.join(2)

    assert not writer_thread.is_alive()
    assert not scanner_thread.is_alive()
    assert errors == []
    assert scan_entered.is_set()
    assert observed_rows == [(str(snapshot_path),)]


def test_commit_failure_releases_media_guard_and_skips_post_commit_work(tmp_path):
    fresh_db = tmp_path / "speciesid.db"
    speciesid.ensure_schema(fresh_db)
    event_id = "evt-commit-failure"
    events = []
    real_connect_db = speciesid.connect_db

    class FailingCommitConnection:
        def __init__(self, connection):
            self.connection = connection

        def __getattr__(self, name):
            return getattr(self.connection, name)

        def commit(self):
            raise sqlite3.OperationalError("controlled commit failure")

    @contextmanager
    def guard(database_path):
        events.append("guard_enter")
        try:
            yield
        finally:
            events.append("guard_exit")

    def failing_connection(database_path, row_factory=False):
        return FailingCommitConnection(
            real_connect_db(database_path, row_factory=row_factory)
        )

    with patch.object(speciesid, "DBPATH", fresh_db), patch.object(
        speciesid, "media_activity_guard", guard
    ), patch.object(
        speciesid, "connect_db", side_effect=failing_connection
    ), patch.object(
        speciesid, "archive_snapshot", return_value="snapshot.jpg"
    ), patch.object(
        speciesid, "archive_clip", return_value="clip.mp4"
    ), patch.object(speciesid, "set_sublabel") as sublabel, patch.object(
        speciesid, "publish_new_species"
    ) as new_species, patch.object(
        speciesid, "post_observation_event"
    ) as bridge, pytest.raises(sqlite3.OperationalError, match="controlled"):
        speciesid._process_qualifying_detection(
            MagicMock(),
            {"camera": "birdcam"},
            event_id,
            "http://localhost:5000",
            "2023-11-14 22:13:20",
            42,
            0.92,
            "Turdus migratorius",
            "bird",
            "American Robin",
        )

    assert events == ["guard_enter", "guard_exit"]
    sublabel.assert_not_called()
    new_species.assert_not_called()
    bridge.assert_not_called()
    with sqlite3.connect(fresh_db) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM detections WHERE frigate_event = ?",
            (event_id,),
        ).fetchone()[0] == 0
