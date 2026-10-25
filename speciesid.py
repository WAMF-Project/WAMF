from app.bootstrap import DEFAULT_PORT, REPO_ROOT, preflight, prepare_native_startup

# Configuration bootstrap belongs exclusively to the native parent.
if __name__ == "__main__":
    prepare_native_startup()

import json
import logging
import multiprocessing
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from contextlib import ExitStack
from datetime import datetime
from functools import partial
from io import BytesIO

import numpy as np
import paho.mqtt.client as mqtt
from ai_edge_litert.interpreter import Interpreter
from PIL import Image

from app.archive_media import archive_clip, archive_snapshot
from app.config_editor import get_config_path
from app.config_loader import load_runtime_config
from app.config_normalization import normalize_config
from app.classifier_model import LEGACY_BACKGROUND_INDEX, ModelValidationError, prepare_classifier
from app.model_registry import resolve_classifier_model
from app.model_validation import validate_model_artifact
from app.db import DatabaseNewerError, SchemaMigrationError, connect_db, ensure_schema
from app.database_lifecycle import maintenance_access
from app.database_restore_application import database_startup
from app.frigate_client import FrigateClient, FrigateError
from app.health import set_detection_worker_enabled, start_health_monitor
from app.media_coordination import media_activity_guard
from app.mqtt_settings import MqttSettings, mqtt_settings_from_config
from app.mqtt_connection import configure_mqtt_client
from app.mqtt_runtime import MqttRuntimeDiagnostics, get_mqtt_runtime_status
from app.process_control import WorkerSupervisor, configure_worker_signals
from app.queries import get_common_name, get_scientific_name
from app.retention_schedule import parse_retention_schedule
from app.retention_scheduler import (
    SHUTDOWN_GRACE_SECONDS as RETENTION_SCHEDULER_SHUTDOWN_GRACE_SECONDS,
)
from app.retention_scheduler import (
    build_scheduler_config_snapshot,
    run_scheduler_child,
)
from app.system_events import log_system_event
from integrations.bridge import post_observation_event
from version import VERSION
from wamf_paths import ensure_storage_paths, get_database_path, get_models_path
from webui import app

classifier = None
classifier_input = None
classifier_output = None
classifier_display_names = None
classifier_category_names = None
classifier_background_index = LEGACY_BACKGROUND_INDEX
config = None
frigate_client = None
logger = logging.getLogger(__name__)

# Optional test/explicit override. None keeps config resolution dynamic.
DBPATH = None
CLASSIFIER_MAX_RESULTS = 5
CLASSIFIER_SCORE_THRESHOLD = 0.05
SCHEDULER_RESTART_INITIAL_SECONDS = 5.0
SCHEDULER_RESTART_MAX_SECONDS = 60.0
SCHEDULER_RESTART_STABLE_SECONDS = 300.0
SCHEDULER_PARENT_SHUTDOWN_GRACE_SECONDS = (
    RETENTION_SCHEDULER_SHUTDOWN_GRACE_SECONDS + 2.0
)


@dataclass(frozen=True)
class Category:
    index: int
    score: float
    display_name: str
    category_name: str


@dataclass
class SchedulerWorkerState:
    config_snapshot: dict
    process: object = None
    started_at: float | None = None
    restart_at: float | None = None
    restart_delay: float = SCHEDULER_RESTART_INITIAL_SECONDS


@dataclass
class MqttConsumerContext:
    settings: MqttSettings = field(repr=False)
    runtime: object = field(repr=False)
    subscription_mid: int | None = None


def _record_mqtt_runtime(userdata, action, reason=None):
    if isinstance(userdata, MqttConsumerContext):
        userdata.runtime.record(action, reason)


def _start_mqtt_worker(workers, runtime):
    writer = runtime.begin_worker()
    process = workers.start(
        run_mqtt_client, args=(writer, config, DBPATH),
        on_exit=lambda: runtime.stop_worker(writer),
    )
    if process is None:
        runtime.stop_worker(writer)
    return process


def _start_scheduler_worker(workers, state, *, monotonic=time.monotonic):
    state.process = workers.start(
        run_scheduler_child,
        args=(state.config_snapshot,),
        shutdown_grace_seconds=SCHEDULER_PARENT_SHUTDOWN_GRACE_SECONDS,
    )
    state.started_at = monotonic()
    state.restart_at = None
    return state.process


def _monitor_scheduler_worker(workers, state, *, monotonic=time.monotonic):
    """Restart an exited scheduler with capped exponential backoff."""

    now = monotonic()
    if state.process is not None:
        if state.process.is_alive():
            if (
                state.started_at is not None
                and now - state.started_at >= SCHEDULER_RESTART_STABLE_SECONDS
            ):
                state.restart_delay = SCHEDULER_RESTART_INITIAL_SECONDS
            return state.process

        exitcode = state.process.exitcode
        workers.reap(state.process)
        delay = state.restart_delay
        state.process = None
        state.started_at = None
        state.restart_at = now + delay
        state.restart_delay = min(
            delay * 2,
            SCHEDULER_RESTART_MAX_SECONDS,
        )
        logger.error(
            "Retention scheduler subprocess exited unexpectedly with code %s; "
            "restarting in %.1f seconds",
            exitcode,
            delay,
        )
        return None

    if state.restart_at is not None and now >= state.restart_at:
        logger.warning("Restarting retention scheduler subprocess")
        return _start_scheduler_worker(workers, state, monotonic=monotonic)
    return None


def configure_logging():
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(levelname)s [%(name)s] %(message)s'
    )


def classify(image):
    expected_shape = tuple(int(size) for size in classifier_input['shape'][1:])
    image = np.asarray(image, dtype=np.uint8)
    if image.shape != expected_shape:
        raise ValueError(
            f"Classifier image must have shape {expected_shape}, got {image.shape}"
        )

    input_tensor = np.ascontiguousarray(image[np.newaxis, ...])
    classifier.set_tensor(classifier_input['index'], input_tensor)
    classifier.invoke()

    raw_scores = classifier.get_tensor(classifier_output['index']).reshape(-1)
    scale, zero_point = classifier_output['quantization']
    if scale <= 0:
        raise ValueError("Classifier output tensor has no valid quantization scale")
    scores = (raw_scores.astype(np.float32) - zero_point) * scale

    ranked_indexes = np.argsort(-scores, kind='stable')
    return [
        Category(
            index=int(index),
            score=float(scores[index]),
            display_name=classifier_display_names[index],
            category_name=classifier_category_names[index],
        )
        for index in ranked_indexes
        if scores[index] >= CLASSIFIER_SCORE_THRESHOLD
    ][:CLASSIFIER_MAX_RESULTS]


def initialize_classifier(model_path, *, artifact=None, allowed_root=None):
    global classifier
    global classifier_input
    global classifier_output
    global classifier_display_names
    global classifier_category_names
    global classifier_background_index

    if artifact is not None:
        result = validate_model_artifact(model_path, artifact, allowed_root=allowed_root)
        if not result.valid:
            raise ModelValidationError(result.code)
    prepared = prepare_classifier(model_path, artifact=artifact, interpreter_factory=Interpreter)
    classifier = prepared.interpreter
    classifier_input = prepared.input_details
    classifier_output = prepared.output_details
    classifier_display_names = prepared.display_labels
    classifier_category_names = prepared.category_labels
    classifier_background_index = prepared.background_index
    return classifier


def on_connect(client, userdata, flags, reason_code, properties):
    if isinstance(userdata, MqttConsumerContext):
        userdata.subscription_mid = None
    if reason_code != 0:
        _record_mqtt_runtime(userdata, "connack_rejected", reason_code)
        logger.warning("MQTT connection refused: %s", reason_code)
        return
    _record_mqtt_runtime(userdata, "connack_accepted", reason_code)
    logger.info("MQTT connected")

    log_system_event(
        event_type="MQTT",
        severity="INFO",
        message="MQTT connected"
    )

    settings = _mqtt_runtime_settings(userdata)
    result = client.subscribe(f"{settings.topic_prefix}/events")
    if isinstance(userdata, MqttConsumerContext):
        rc, mid = result
        if rc == mqtt.MQTT_ERR_SUCCESS:
            userdata.subscription_mid = mid
        else:
            _record_mqtt_runtime(userdata, "subscription_failed")


def on_subscribe(client, userdata, mid, reason_codes, properties):
    expected_ack = (
        isinstance(userdata, MqttConsumerContext)
        and userdata.subscription_mid is not None
        and mid == userdata.subscription_mid
    )
    if not reason_codes or any(code.is_failure for code in reason_codes):
        if expected_ack:
            _record_mqtt_runtime(userdata, "subscription_failed")
        logger.warning("MQTT event subscription refused")
        return
    if expected_ack:
        _record_mqtt_runtime(userdata, "subscribed")
    logger.info("MQTT event subscription ready")


def on_disconnect(client, userdata, flags, reason_code, properties):
    if isinstance(userdata, MqttConsumerContext):
        userdata.subscription_mid = None
    _record_mqtt_runtime(userdata, "disconnect", reason_code)
    if reason_code != 0:
        logger.warning("Unexpected MQTT disconnection, trying to reconnect")
        while True:
            try:
                client.reconnect()
                break
            except (OSError, RuntimeError) as e:
                logger.warning(
                    "MQTT reconnection failed; retrying in 60 seconds: %s",
                    e,
                )
                time.sleep(60)
    else:
        logger.info("Expected MQTT disconnection")


def publish_new_species(client, common_name, scientific_name, score, camera_name, frigate_event):
    base = 'whosatmyfeeder/new_species'

    client.publish(
        f'{base}/common_name',
        common_name,
        qos=0,
        retain=True
    )

    client.publish(
        f'{base}/scientific_name',
        scientific_name,
        qos=0,
        retain=True
    )

    client.publish(
        f'{base}/score',
        f'{score:.2f}',
        qos=0,
        retain=True
    )

    client.publish(
        f'{base}/camera',
        camera_name,
        qos=0,
        retain=True
    )

    client.publish(
        f'{base}/frigate_event',
        frigate_event,
        qos=0,
        retain=True
    )

    # Combined JSON for HA automation trigger
    client.publish(
        base,
        json.dumps({
            'common_name': common_name,
            'scientific_name': scientific_name,
            'score': f'{score:.2f}',
            'camera': camera_name,
            'frigate_event': frigate_event,
        }),
        qos=1,
        retain=True
    )


def set_sublabel(client, frigate_event, sublabel):
    try:
        client.set_event_sub_label(frigate_event, sublabel)
    except FrigateError as e:
        logger.warning("Failed to set sublabel due to request error: %s", e)
        return

    logger.info("Sublabel set successfully to: %s", sublabel[:20])


def commit_detection(conn, pending_observation=None):
    """Commit detection changes, then notify Bridge for a new row if present."""
    conn.commit()

    if pending_observation:
        post_observation_event(
            config.get('bridge', {}),
            **pending_observation
        )


def _process_qualifying_detection(
    client,
    after_data,
    frigate_event,
    frigate_url,
    formatted_start_time,
    index,
    score,
    display_name,
    category_name,
    common_name,
):
    """Archive and commit one qualifying detection under the media guard."""

    pending_observation = None
    should_set_sublabel = False
    is_first_species_detection = False

    # Archive helpers write directly to their final paths. Serialize from before
    # those writes until their database references are durably committed.
    with media_activity_guard(DBPATH):
        conn = connect_db(DBPATH, row_factory=False)
        try:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT
                    id,
                    detection_time,
                    detection_index,
                    score,
                    display_name,
                    category_name,
                    frigate_event,
                    camera_name,
                    wamf_snapshot_path,
                    wamf_clip_path
                FROM detections
                WHERE frigate_event = ?
                """,
                (frigate_event,)
            )
            result = cursor.fetchone()

            if result is None:
                logger.info("No record yet for event %s. Storing.", frigate_event)

                wamf_snapshot_path = archive_snapshot(
                    frigate_url,
                    frigate_event
                )
                wamf_clip_path = archive_clip(
                    frigate_url,
                    frigate_event
                )

                cursor.execute(
                    """
                    INSERT INTO detections
                    (
                        detection_time,
                        detection_index,
                        score,
                        display_name,
                        category_name,
                        frigate_event,
                        camera_name,
                        wamf_snapshot_path,
                        wamf_clip_path
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        formatted_start_time,
                        index,
                        score,
                        display_name,
                        category_name,
                        frigate_event,
                        after_data['camera'],
                        wamf_snapshot_path,
                        wamf_clip_path
                    )
                )

                pending_observation = {
                    'common_name': common_name,
                    'scientific_name': display_name,
                    'confidence': score,
                    'camera': after_data.get('camera'),
                    'frigate_event': frigate_event,
                }
                should_set_sublabel = True

                cursor.execute(
                    """
                    SELECT COUNT(*)
                    FROM detections
                    WHERE display_name = ?
                    """,
                    (display_name,)
                )
                is_first_species_detection = cursor.fetchone()[0] == 1

            else:
                logger.info(
                    "Record already exists for event %s. Checking score.",
                    frigate_event,
                )
                existing_score = result[3]

                if score > existing_score:
                    logger.info("New score is higher. Updating record.")
                    cursor.execute(
                        """
                        UPDATE detections
                        SET detection_time = ?,
                            detection_index = ?,
                            score = ?,
                            display_name = ?,
                            category_name = ?
                        WHERE frigate_event = ?
                        """,
                        (
                            formatted_start_time,
                            index,
                            score,
                            display_name,
                            category_name,
                            frigate_event
                        )
                    )
                    should_set_sublabel = True
                else:
                    logger.info("New score is lower. Keeping existing record.")

            conn.commit()
        finally:
            conn.close()

    if should_set_sublabel:
        set_sublabel(
            frigate_client,
            frigate_event,
            common_name
        )

    if is_first_species_detection:
        logger.info(
            "New species detected for the first time: %s",
            common_name,
        )
        publish_new_species(
            client,
            common_name,
            display_name,
            score,
            after_data['camera'],
            frigate_event
        )

    if pending_observation:
        post_observation_event(
            config.get('bridge', {}),
            **pending_observation
        )


def on_message(client, userdata, message):

    try:
        _on_message_inner(
            client,
            userdata,
            message
        )

    except (
        json.JSONDecodeError,
        KeyError,
        TypeError,
        ValueError,
        sqlite3.Error,
        OSError,
        AttributeError,
    ) as e:

        logger.exception("ERROR in on_message: %s", e)


def _on_message_inner(client, userdata, message):

    settings = _mqtt_runtime_settings(userdata)
    expected_topic = f"{settings.topic_prefix}/events"
    if message.topic != expected_topic:
        return
    _record_mqtt_runtime(userdata, "event_received")
    if message.retain:
        return

    return _process_live_message(client, message)


def _process_live_message(client, message):

    conn = None

    try:

        payload_dict = json.loads(
            message.payload
        )

        event_type = payload_dict.get("type")

        if event_type != "new":
            return

        after_data = payload_dict.get(
            'after',
            {}
        )

        if (
            after_data['camera'] in config['frigate']['camera']
            and after_data['label'] == 'bird'
        ):

            frigate_event = after_data['id']

            frigate_url = config['frigate']['frigate_url']

            logger.info("Getting image for event: %s", frigate_event)

            logger.info("Fetching snapshot")

            try:
                snapshot = frigate_client.get_event_snapshot(
                    frigate_event,
                    crop=True,
                    quality=95,
                )
            except FrigateError as e:
                logger.warning("Could not retrieve image due to request error: %s", e)
                return

            logger.info("Snapshot HTTP 200")

            if snapshot is not None:

                image = Image.open(
                    BytesIO(snapshot)
                ).convert("RGB")

                image = image.resize((224, 224))

                # Convert to numpy
                np_arr = np.array(
                    image,
                    dtype=np.uint8
                )

                # Ensure contiguous memory
                np_arr = np.ascontiguousarray(
                    np_arr
                )

                logger.debug("Image shape: %s dtype: %s", np_arr.shape, np_arr.dtype)
                logger.info("Classifying snapshot")

                categories = classify(np_arr)

                if not categories:
                    logger.info("No usable classification result for event %s", frigate_event)
                    return

                category = categories[0]

                index = category.index

                score = float(category.score)

                display_name = (
                    category.display_name
                    or "Unknown"
                )

                category_name = (
                    category.category_name
                    or "Unknown"
                )

                start_time = datetime.fromtimestamp(
                    after_data['start_time']
                )

                formatted_start_time = (
                    start_time.strftime(
                        "%Y-%m-%d %H:%M:%S"
                    )
                )

                result_text = (
                    formatted_start_time
                    + "\n"
                    + str(category)
                )

                logger.info("Classification result: %s", result_text)

                if (
                    index != classifier_background_index
                    and score > config['classification']['threshold']
                ):

                    common_name = (
                        get_common_name(display_name)
                        or display_name
                    )

                    client.publish(
                        'whosatmyfeeder/detections',
                        common_name,
                        qos=0,
                        retain=False
                    )

                    _process_qualifying_detection(
                        client,
                        after_data,
                        frigate_event,
                        frigate_url,
                        formatted_start_time,
                        index,
                        score,
                        display_name,
                        category_name,
                        common_name,
                    )

                else:

                    sub_label_data = after_data.get(
                        'sub_label'
                    )

                    # sub_label is ["Common Name", score]
                    # or a plain string

                    if (
                        isinstance(sub_label_data, list)
                        and len(sub_label_data) >= 2
                        and sub_label_data[1] is not None
                    ):

                        frigate_common = sub_label_data[0]

                        frigate_score = float(
                            sub_label_data[1]
                        )

                    elif (
                        isinstance(sub_label_data, str)
                        and sub_label_data
                    ):

                        frigate_common = sub_label_data

                        frigate_score = score

                    else:

                        frigate_common = None
                        frigate_score = None

                    if frigate_common:

                        scientific_name = get_scientific_name(
                            frigate_common
                        )

                        if scientific_name:

                            logger.info(
                                "WAMF below threshold; using Frigate sub_label: "
                                "%s (WAMF score: %.2f)",
                                frigate_common,
                                frigate_score,
                            )

                            conn = connect_db(DBPATH, row_factory=False)
                            cursor = conn.cursor()

                            cursor.execute(
                                """
                                SELECT
                                    id,
                                    detection_time,
                                    detection_index,
                                    score,
                                    display_name,
                                    category_name,
                                    frigate_event,
                                    camera_name,
                                    wamf_snapshot_path,
                                    wamf_clip_path
                                FROM detections
                                WHERE frigate_event = ?
                                """,
                                (frigate_event,)
                            )

                            result = cursor.fetchone()

                            pending_observation = None

                            if result is None:

                                cursor.execute(
                                    """
                                    INSERT INTO detections
                                    (
                                        detection_time,
                                        detection_index,
                                        score,
                                        display_name,
                                        category_name,
                                        frigate_event,
                                        camera_name
                                    )
                                    VALUES (?, ?, ?, ?, ?, ?, ?)
                                    """,
                                    (
                                        formatted_start_time,
                                        -1,
                                        frigate_score,
                                        scientific_name,
                                        'frigate_classified',
                                        frigate_event,
                                        after_data['camera']
                                    )
                                )

                                pending_observation = {
                                    'common_name': frigate_common,
                                    'scientific_name': scientific_name,
                                    'confidence': frigate_score,
                                    'camera': after_data.get('camera'),
                                    'frigate_event': frigate_event,
                                }

                                set_sublabel(
                                    frigate_client,
                                    frigate_event,
                                    frigate_common
                                )

                                cursor.execute(
                                    """
                                    SELECT COUNT(*)
                                    FROM detections
                                    WHERE display_name = ?
                                    """,
                                    (scientific_name,)
                                )

                                if cursor.fetchone()[0] == 1:

                                    logger.info(
                                        "New species via Frigate sub_label: %s",
                                        frigate_common,
                                    )

                                    publish_new_species(
                                        client,
                                        frigate_common,
                                        scientific_name,
                                        frigate_score,
                                        after_data['camera'],
                                        frigate_event
                                    )

                            else:

                                existing_score = result[3]

                                if frigate_score > existing_score:

                                    cursor.execute(
                                        """
                                        UPDATE detections
                                        SET score = ?,
                                            display_name = ?,
                                            category_name = ?
                                        WHERE frigate_event = ?
                                        """,
                                        (
                                            frigate_score,
                                            scientific_name,
                                            'frigate_classified',
                                            frigate_event
                                        )
                                    )

                                    set_sublabel(
                                        frigate_client,
                                        frigate_event,
                                        frigate_common
                                    )

                            commit_detection(conn, pending_observation)

    finally:
        if conn is not None:
            conn.close()


def load_config():

    global config
    global frigate_client

    config = load_runtime_config()

    frigate_client = FrigateClient(config['frigate']['frigate_url'])


def configure_worker_runtime(runtime_config, database_path):
    """Receive the parent's validated snapshot without reloading or migrating."""
    global config, DBPATH, frigate_client
    from copy import deepcopy

    import webui

    config = deepcopy(runtime_config)
    config.setdefault('storage', {})['database_path'] = str(database_path)
    DBPATH = database_path
    frigate_client = FrigateClient(config.get('frigate', {}).get('frigate_url') or '')
    webui.configure_runtime(config, database_path)


def _mqtt_runtime_settings(userdata=None):
    if isinstance(userdata, MqttConsumerContext):
        return userdata.settings
    if isinstance(userdata, MqttSettings):
        return userdata
    return mqtt_settings_from_config(normalize_config(config).config)


def run_webui(mqtt_runtime_reader=None, runtime_config=None, database_path=None):
    configure_worker_signals()
    if runtime_config is not None:
        configure_worker_runtime(runtime_config, database_path)
    logger.info("Starting Flask app")

    set_detection_worker_enabled(not preflight(config))
    if mqtt_runtime_reader is not None:
        read_runtime = partial(get_mqtt_runtime_status, mqtt_runtime_reader)
        app.extensions['mqtt_runtime_status'] = read_runtime
        start_health_monitor(mqtt_runtime_status=read_runtime)
    else:
        start_health_monitor()

    app.run(
        debug=False,
        host=config.get("webui", {}).get("host", "0.0.0.0"),
        port=config.get("webui", {}).get("port", DEFAULT_PORT),
    )


def initialize_worker_classifier(runtime_writer=None):
    """Initialize once in the consumer and publish its actual loaded identity."""
    from app.classifier_runtime import ClassifierRuntimeSnapshot, loaded_classifier

    try:
        selection = resolve_classifier_model(config, repo_root=REPO_ROOT)
        if selection.managed:
            initialize_classifier(
                selection.path, artifact=selection.artifact,
                allowed_root=REPO_ROOT if selection.artifact.bundled_path else get_models_path(config),
            )
        else:
            initialize_classifier(selection.path)
    except Exception:
        if runtime_writer is not None:
            runtime_writer.record_classifier(ClassifierRuntimeSnapshot(state="failed"))
        raise
    if runtime_writer is not None:
        try:
            observation = loaded_classifier(selection)
        except OSError:
            observation = ClassifierRuntimeSnapshot(state="unknown")
        runtime_writer.record_classifier(observation)


def run_mqtt_client(runtime_writer=None, runtime_config=None, database_path=None):
    configure_worker_signals()
    if runtime_config is not None:
        configure_worker_runtime(runtime_config, database_path)

    if runtime_writer is not None:
        runtime_writer.record("started")

    settings = _mqtt_runtime_settings()

    initialize_worker_classifier(runtime_writer)

    logger.info("Classifier initialized in MQTT subprocess")
    logger.info(
        "Starting MQTT client. Connecting to: %s",
        settings.host,
    )

    now = datetime.now()

    current_time = now.strftime(
        "%Y%m%d%H%M%S"
    )

    client = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        client_id="birdspeciesid" + current_time
    )
    client.user_data_set(
        MqttConsumerContext(settings, runtime_writer)
        if runtime_writer is not None else settings
    )

    client.on_message = on_message
    client.on_subscribe = on_subscribe
    client.on_disconnect = on_disconnect
    client.on_connect = on_connect

    configure_mqtt_client(client, settings)

    client.connect(
        settings.host,
        settings.port
    )

    client.loop_forever()


def main():
    with ExitStack() as lifecycle:
        return _main(lifecycle)


def _main(lifecycle):
    configure_logging()

    try:
        load_config()
        setup_issues = preflight(config)
        # Resolve once from the validated snapshot, never from a later YAML read.
        database_path = get_database_path(config).resolve()
        startup = lifecycle.enter_context(database_startup(database_path))
        with maintenance_access(startup.lease):
            ensure_schema(database_path)
        # ensure_schema has committed and closed its connection before binding
        # any consumers or constructing diagnostics/processes/background work.
        ensure_storage_paths(config)
        configure_worker_runtime(config, database_path)
        startup.allow_consumers()
    except Exception as exc:
        if isinstance(exc, DatabaseNewerError):
            reason = 'database is newer than this WAMF build'
        elif isinstance(exc, SchemaMigrationError):
            reason = 'invalid or unsupported database schema/history'
        else:
            reason = 'configuration or database initialization failed'
        # Exception text may contain SQL values or secrets. Process logging is
        # available even when no application database can be opened.
        logger.error('WAMF startup refused: %s (%s)', reason, type(exc).__name__)
        raise SystemExit(1) from exc

    now = datetime.now()

    current_time = now.strftime(
        '%Y-%m-%d %H:%M:%S.%f'
    )[:-3]

    

    log_system_event(
        event_type="SYSTEM",
        severity="INFO",
        message=f"WAMF {VERSION} started",
        db_path=DBPATH,
    )

    ...

    logger.info("Time: %s", current_time)
    logger.info("Python version: %s", sys.version)
    logger.info("Version info: %s", sys.version_info)

    retention_schedule = parse_retention_schedule(config)
    scheduler_state = None
    if retention_schedule.enabled:
        scheduler_state = SchedulerWorkerState(
            config_snapshot=build_scheduler_config_snapshot(config)
        )

    mqtt_runtime = MqttRuntimeDiagnostics()
    with WorkerSupervisor(
        multiprocessing.Process, on_workers_stopped=mqtt_runtime.close,
    ) as workers:
        mqtt_process = None
        if setup_issues:
            logger.warning(
                "WAMF setup/configuration required: %s. Starting web/admin UI on port %s; "
                "MQTT/classification worker is disabled. Complete Configuration in the Admin UI "
                "and restart WAMF to start detection.",
                ', '.join(setup_issues),
                config.get('webui', {}).get('port', DEFAULT_PORT),
            )
            flask_process = workers.start(run_webui, args=(mqtt_runtime.reader, config, DBPATH))
        else:
            logger.info("Starting processes for Flask and MQTT")
            flask_process = workers.start(run_webui, args=(mqtt_runtime.reader, config, DBPATH))
            mqtt_process = _start_mqtt_worker(workers, mqtt_runtime)

        if scheduler_state is not None:
            logger.info("Starting native retention scheduler process")
            _start_scheduler_worker(workers, scheduler_state)

        # Keep worker watchdogs active while Flask is serving, but never
        # respawn during shutdown or after the Flask worker has exited.
        startup.mark_succeeded()
        while not workers.stopping and flask_process.is_alive():
            if mqtt_process is None:
                flask_process.join(timeout=0.5)
            else:
                mqtt_process.join(timeout=0.5)
            if workers.stopping or not flask_process.is_alive():
                break

            if scheduler_state is not None:
                _monitor_scheduler_worker(workers, scheduler_state)

            if mqtt_process is not None and not mqtt_process.is_alive():
                workers.reap(mqtt_process)
                logger.warning("MQTT subprocess exited unexpectedly; restarting")
                mqtt_process = _start_mqtt_worker(workers, mqtt_runtime)

        if workers.received_signal is not None:
            action = "Restart" if workers.restart_requested else "Shutdown"
            logger.info("%s requested by signal %s; stopping all workers", action, workers.received_signal)

    logger.info("WAMF stopped; all child processes joined")


if __name__ == '__main__':

    configure_logging()
    logger.info("Calling main")

    main()
