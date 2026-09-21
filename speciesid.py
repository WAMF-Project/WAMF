from app.bootstrap import DEFAULT_PORT, REPO_ROOT, preflight, prepare_native_startup

# Bootstrap once in the native parent, before webui import creates storage.
if __name__ == "__main__":
    prepare_native_startup()

import sqlite3
from dataclasses import dataclass
from pathlib import Path
import logging
import numpy as np
from datetime import datetime
import time
import multiprocessing

from ai_edge_litert.interpreter import Interpreter
import paho.mqtt.client as mqtt
import yaml
from webui import app
import sys
import json
import requests
from PIL import Image
from io import BytesIO
from app.queries import get_common_name, get_scientific_name
from app.archive_media import (
    archive_snapshot,
    archive_clip
)
from app.system_events import log_system_event
from version import VERSION
from app.db import connect_db, ensure_schema
from app.config_editor import get_config_path
from app.process_control import WorkerSupervisor, configure_worker_signals
from wamf_paths import ensure_storage_paths
from integrations.bridge import post_observation_event
from app.health import start_health_monitor, set_detection_worker_enabled

classifier = None
classifier_input = None
classifier_output = None
classifier_display_names = None
classifier_category_names = None
config = None
logger = logging.getLogger(__name__)

# Optional test/explicit override. None keeps config resolution dynamic.
DBPATH = None
DEFAULT_MQTT_PORT = 1883
DEFAULT_INSECURE_TLS = False
CLASSIFIER_MAX_RESULTS = 5
CLASSIFIER_SCORE_THRESHOLD = 0.05


@dataclass(frozen=True)
class Category:
    index: int
    score: float
    display_name: str
    category_name: str


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


def initialize_classifier(model_path):
    import zipfile

    global classifier
    global classifier_input
    global classifier_output
    global classifier_display_names
    global classifier_category_names

    model_path = Path(model_path)
    new_classifier = Interpreter(model_path=str(model_path), num_threads=4)
    new_classifier.allocate_tensors()
    input_details = new_classifier.get_input_details()[0]
    output_details = new_classifier.get_output_details()[0]

    if input_details['dtype'] != np.uint8:
        raise ValueError("Classifier input tensor must use uint8 data")
    if tuple(input_details['shape']) != (1, 224, 224, 3):
        raise ValueError(
            "Classifier input tensor must have shape (1, 224, 224, 3)"
        )

    with zipfile.ZipFile(model_path) as model:
        display_names = model.read('probability-labels-en.txt').decode('utf-8').splitlines()
        category_names = model.read('probability-labels.txt').decode('utf-8').splitlines()

    output_size = int(np.prod(output_details['shape']))
    if len(display_names) != output_size or len(category_names) != output_size:
        raise ValueError("Classifier labels do not match the output tensor size")

    classifier = new_classifier
    classifier_input = input_details
    classifier_output = output_details
    classifier_display_names = display_names
    classifier_category_names = category_names
    return classifier


def on_connect(client, userdata, flags, rc):
    if rc != 0:
        logger.warning("MQTT connection refused: %s", rc)
        return
    logger.info("MQTT connected")

    log_system_event(
        event_type="MQTT",
        severity="INFO",
        message="MQTT connected"
    )

    # we are going subscribe to frigate/events and look for bird detections there
    client.subscribe(config['frigate']['main_topic'] + "/events")


def on_subscribe(client, userdata, mid, granted_qos):
    if not granted_qos or any(qos == 128 for qos in granted_qos):
        logger.warning("MQTT event subscription refused")
        return
    logger.info("MQTT event subscription ready")


def on_disconnect(client, userdata, rc):
    if rc != 0:
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


def set_sublabel(frigate_url, frigate_event, sublabel):

    post_url = (
        frigate_url
        + "/api/events/"
        + frigate_event
        + "/sub_label"
    )

    # frigate limits sublabels to 20 characters currently
    if len(sublabel) > 20:
        sublabel = sublabel[:20]

    payload = {
        "subLabel": sublabel
    }

    headers = {
        "Content-Type": "application/json"
    }

    try:
        response = requests.post(
            post_url,
            data=json.dumps(payload),
            headers=headers,
            timeout=2
        )

    except requests.exceptions.RequestException as e:
        logger.warning("Failed to set sublabel due to request error: %s", e)
        return

    if response.status_code == 200:
        logger.info("Sublabel set successfully to: %s", sublabel)

    else:
        logger.warning("Failed to set sublabel. Status code: %s", response.status_code)


def commit_detection(conn, pending_observation=None):
    """Commit detection changes, then notify Bridge for a new row if present."""
    conn.commit()

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
        requests.exceptions.RequestException,
        OSError,
        AttributeError,
    ) as e:

        logger.exception("ERROR in on_message: %s", e)


def _on_message_inner(client, userdata, message):

    expected_topic = config['frigate'].get('main_topic', 'frigate') + '/events'
    if message.retain or message.topic != expected_topic:
        return

    conn = connect_db(DBPATH, row_factory=False)

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

            snapshot_url = (
                frigate_url
                + "/api/events/"
                + frigate_event
                + "/snapshot.jpg"
            )

            logger.info("Getting image for event: %s", frigate_event)
            logger.debug("Snapshot URL: %s", snapshot_url)

            params = {
                "crop": 1,
                "quality": 95
            }

            logger.info("Fetching snapshot")

            try:

                response = requests.get(
                    snapshot_url,
                    params=params,
                    timeout=2
                )

            except requests.exceptions.RequestException as e:

                logger.warning("Could not retrieve image due to request error: %s", e)

                return

            logger.info("Snapshot HTTP %s", response.status_code)

            if response.status_code == 200:

                image = Image.open(
                    BytesIO(response.content)
                ).convert("RGB")

                # Resize while preserving aspect ratio
                image.thumbnail((224, 224))

                # Create fixed-size canvas
                canvas = Image.new(
                    "RGB",
                    (224, 224),
                    (0, 0, 0)
                )

                # Center image
                x = (224 - image.width) // 2
                y = (224 - image.height) // 2

                canvas.paste(
                    image,
                    (x, y)
                )

                # Convert to numpy
                np_arr = np.array(
                    canvas,
                    dtype=np.uint8
                )

                # Ensure contiguous memory
                np_arr = np.ascontiguousarray(
                    np_arr
                )

                logger.debug("Image shape: %s dtype: %s", np_arr.shape, np_arr.dtype)
                logger.info("Classifying snapshot")

                categories = classify(np_arr)

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

                # 964 = background
                if (
                    index != 964
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

                        set_sublabel(
                            frigate_url,
                            frigate_event,
                            common_name
                        )

                        cursor.execute(
                            """
                            SELECT COUNT(*)
                            FROM detections
                            WHERE display_name = ?
                            """,
                            (display_name,)
                        )

                        if cursor.fetchone()[0] == 1:

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

                            set_sublabel(
                                frigate_url,
                                frigate_event,
                                common_name
                            )

                        else:

                            logger.info("New score is lower. Keeping existing record.")

                    commit_detection(conn, pending_observation)

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
                                    frigate_url,
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
                                        frigate_url,
                                        frigate_event,
                                        frigate_common
                                    )

                            commit_detection(conn, pending_observation)

            else:

                logger.warning(
                    "Could not retrieve image. Status code: %s",
                    response.status_code,
                )

    finally:
        conn.close()


def setupdb():

    ensure_storage_paths()
    ensure_schema(DBPATH)


def load_config():

    global config

    with open(get_config_path(), 'r') as config_file:

        config = yaml.safe_load(
            config_file
        )


def run_webui():
    configure_worker_signals()
    logger.info("Starting Flask app")

    set_detection_worker_enabled(not preflight(config))
    start_health_monitor()

    app.run(
        debug=False,
        host=config.get("webui", {}).get("host", "0.0.0.0"),
        port=config.get("webui", {}).get("port", DEFAULT_PORT),
    )


def run_mqtt_client():
    configure_worker_signals()

    initialize_classifier(
        REPO_ROOT / Path(config['classification']['model']).expanduser()
    )

    logger.info("Classifier initialized in MQTT subprocess")
    logger.info(
        "Starting MQTT client. Connecting to: %s",
        config['frigate']['mqtt_server'],
    )

    now = datetime.now()

    current_time = now.strftime(
        "%Y%m%d%H%M%S"
    )

    client = mqtt.Client(
        "birdspeciesid" + current_time
    )

    client.on_message = on_message
    client.on_subscribe = on_subscribe
    client.on_disconnect = on_disconnect
    client.on_connect = on_connect

    if config['frigate'].get('mqtt_auth', False):

        username = config['frigate']['mqtt_username']

        password = config['frigate']['mqtt_password']

        client.username_pw_set(
            username,
            password
        )

    mqtt_port = config['frigate'].get(
        'mqtt_port',
        DEFAULT_MQTT_PORT
    )

    if config['frigate'].get(
        'mqtt_use_tls',
        False
    ):

        ca_certs = config['frigate'].get(
            'mqtt_tls_ca_certs'
        )

        client.tls_set(ca_certs)

        client.tls_insecure_set(
            config['frigate'].get(
                'mqtt_tls_insecure',
                DEFAULT_INSECURE_TLS
            )
        )

    client.connect(
        config['frigate']['mqtt_server'],
        mqtt_port
    )

    client.loop_forever()


def main():
    configure_logging()

    now = datetime.now()

    current_time = now.strftime(
        '%Y-%m-%d %H:%M:%S.%f'
    )[:-3]

    

    log_system_event(
        event_type="SYSTEM",
        severity="INFO",
        message=f"WAMF {VERSION} started"
    )

    ...

    logger.info("Time: %s", current_time)
    logger.info("Python version: %s", sys.version)
    logger.info("Version info: %s", sys.version_info)

    load_config()

    setup_issues = preflight(config)
    setupdb()

    with WorkerSupervisor(multiprocessing.Process) as workers:
        if setup_issues:
            logger.warning(
                "WAMF setup/configuration required: %s. Starting web/admin UI on port %s; "
                "MQTT/classification worker is disabled. Complete Configuration in the Admin UI "
                "and restart WAMF to start detection.",
                ', '.join(setup_issues),
                config.get('webui', {}).get('port', DEFAULT_PORT),
            )
            flask_process = workers.start(run_webui)
            while not workers.stopping and flask_process.is_alive():
                flask_process.join(timeout=0.5)
        else:
            logger.info("Starting processes for Flask and MQTT")
            flask_process = workers.start(run_webui)
            mqtt_process = workers.start(run_mqtt_client)

            # Keep the normal MQTT watchdog, but never respawn during shutdown
            # or after the Flask worker has exited.
            while not workers.stopping and flask_process.is_alive():
                mqtt_process.join(timeout=0.5)
                if workers.stopping or not flask_process.is_alive():
                    break
                if not mqtt_process.is_alive():
                    workers.reap(mqtt_process)
                    logger.warning("MQTT subprocess exited unexpectedly; restarting")
                    mqtt_process = workers.start(run_mqtt_client)

        if workers.received_signal is not None:
            action = "Restart" if workers.restart_requested else "Shutdown"
            logger.info("%s requested by signal %s; stopping all workers", action, workers.received_signal)

    logger.info("WAMF stopped; all child processes joined")


if __name__ == '__main__':

    configure_logging()
    logger.info("Calling main")

    main()
