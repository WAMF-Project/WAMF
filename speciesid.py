import sqlite3
import numpy as np
from datetime import datetime
import time
import multiprocessing
import cv2
from tflite_support.task import core
from tflite_support.task import processor
from tflite_support.task import vision
import paho.mqtt.client as mqtt
import yaml
from webui import app
import sys
import json
import requests
from PIL import Image, ImageOps
from io import BytesIO
from queries import get_common_name, get_scientific_name

classifier = None
config = None
firstmessage = True

DBPATH = './data/speciesid.db'
DEFAULT_MQTT_PORT = 1883
DEFAULT_INSECURE_TLS = False


def classify(image):

    image = np.ascontiguousarray(
        image.astype(np.uint8)
    )

    tensor_image = vision.TensorImage.create_from_array(
        image
    )

    result = classifier.classify(
        tensor_image
    )

    return result.classifications[0].categories


def on_connect(client, userdata, flags, rc):
    print("MQTT Connected", flush=True)

    # we are going subscribe to frigate/events and look for bird detections there
    client.subscribe(config['frigate']['main_topic'] + "/events")


def on_disconnect(client, userdata, rc):
    if rc != 0:
        print("Unexpected disconnection, trying to reconnect", flush=True)
        while True:
            try:
                client.reconnect()
                break
            except Exception as e:
                print(f"Reconnection failed due to {e}, retrying in 60 seconds", flush=True)
                time.sleep(60)
    else:
        print("Expected disconnection", flush=True)


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
        print(
            f"Failed to set sublabel (request error): {e}",
            flush=True
        )
        return

    if response.status_code == 200:
        print(
            "Sublabel set successfully to: " + sublabel,
            flush=True
        )

    else:
        print(
            "Failed to set sublabel. Status code:",
            response.status_code,
            flush=True
        )


def on_message(client, userdata, message):

    try:
        _on_message_inner(
            client,
            userdata,
            message
        )

    except Exception as e:

        print(
            f"ERROR in on_message (unhandled): {e}",
            flush=True
        )

        import traceback
        traceback.print_exc()


def _on_message_inner(client, userdata, message):

    conn = sqlite3.connect(DBPATH)

    try:

        global firstmessage

        if not firstmessage:

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

                print(
                    "Getting image for event: "
                    + frigate_event,
                    flush=True
                )

                print(
                    "Here's the URL: "
                    + snapshot_url,
                    flush=True
                )

                params = {
                    "crop": 1,
                    "quality": 95
                }

                print(
                    "Fetching snapshot...",
                    flush=True
                )

                try:

                    response = requests.get(
                        snapshot_url,
                        params=params,
                        timeout=2
                    )

                except requests.exceptions.RequestException as e:

                    print(
                        f"Error: Could not retrieve the image (request error): {e}",
                        flush=True
                    )

                    return

                print(
                    f"Snapshot HTTP {response.status_code}",
                    flush=True
                )

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

                    print(
                        f"Image shape: {np_arr.shape} "
                        f"dtype: {np_arr.dtype}",
                        flush=True
                    )

                    print(
                        "Classifying...",
                        flush=True
                    )

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

                    print(
                        result_text,
                        flush=True
                    )

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
                            SELECT *
                            FROM detections
                            WHERE frigate_event = ?
                            """,
                            (frigate_event,)
                        )

                        result = cursor.fetchone()

                        if result is None:

                            print(
                                "No record yet for this event. Storing.",
                                flush=True
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
                                    camera_name
                                )
                                VALUES (?, ?, ?, ?, ?, ?, ?)
                                """,
                                (
                                    formatted_start_time,
                                    index,
                                    score,
                                    display_name,
                                    category_name,
                                    frigate_event,
                                    after_data['camera']
                                )
                            )

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

                                print(
                                    f"New species detected for the first time: {common_name}",
                                    flush=True
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

                            print(
                                "There is already a record for this event. Checking score",
                                flush=True
                            )

                            existing_score = result[3]

                            if score > existing_score:

                                print(
                                    "New score is higher. Updating record with higher score.",
                                    flush=True
                                )

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

                                print(
                                    "New score is lower.",
                                    flush=True
                                )

                        conn.commit()

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

                                print(
                                    f"WAMF below threshold; using Frigate sub_label: "
                                    f"{frigate_common} "
                                    f"(WAMF score: {frigate_score:.2f})",
                                    flush=True
                                )

                                cursor = conn.cursor()

                                cursor.execute(
                                    """
                                    SELECT *
                                    FROM detections
                                    WHERE frigate_event = ?
                                    """,
                                    (frigate_event,)
                                )

                                result = cursor.fetchone()

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

                                        print(
                                            f"New species (via Frigate sub_label): "
                                            f"{frigate_common}",
                                            flush=True
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

                                conn.commit()

                else:

                    print(
                        f"Error: Could not retrieve the image. "
                        f"Status code: {response.status_code}",
                        flush=True
                    )

        else:

            firstmessage = False

            print(
                "skipping first message",
                flush=True
            )

    finally:
        conn.close()


def setupdb():

    conn = sqlite3.connect(DBPATH)

    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS detections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            detection_time TIMESTAMP NOT NULL,
            detection_index INTEGER NOT NULL,
            score REAL NOT NULL,
            display_name TEXT NOT NULL,
            category_name TEXT NOT NULL,
            frigate_event TEXT NOT NULL UNIQUE,
            camera_name TEXT NOT NULL
        )
    """)

    conn.commit()

    conn.close()


def load_config():

    global config

    file_path = './config/config.yml'

    with open(file_path, 'r') as config_file:

        config = yaml.safe_load(
            config_file
        )


def run_webui():

    print(
        "Starting flask app",
        flush=True
    )

    app.run(
        debug=False,
        host=config['webui']['host'],
        port=config['webui']['port']
    )


def run_mqtt_client():

    global classifier

    base_options = core.BaseOptions(
        file_name=config['classification']['model'],
        use_coral=False,
        num_threads=4
    )

    classification_options = processor.ClassificationOptions(
        max_results=5,
        score_threshold=0.05
    )

    options = vision.ImageClassifierOptions(
        base_options=base_options,
        classification_options=classification_options
    )

    classifier = vision.ImageClassifier.create_from_options(
        options
    )

    print(
        "Classifier initialized in MQTT subprocess",
        flush=True
    )

    print(
        "Starting MQTT client. Connecting to: "
        + config['frigate']['mqtt_server'],
        flush=True
    )

    now = datetime.now()

    current_time = now.strftime(
        "%Y%m%d%H%M%S"
    )

    client = mqtt.Client(
        "birdspeciesid" + current_time
    )

    client.on_message = on_message
    client.on_disconnect = on_disconnect
    client.on_connect = on_connect

    if config['frigate']['mqtt_auth']:

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

    now = datetime.now()

    current_time = now.strftime(
        '%Y-%m-%d %H:%M:%S.%f'
    )[:-3]

    print(
        "Time: " + current_time,
        flush=True
    )

    print(
        "Python version",
        flush=True
    )

    print(
        sys.version,
        flush=True
    )

    print(
        "Version info.",
        flush=True
    )

    print(
        sys.version_info,
        flush=True
    )

    load_config()

    setupdb()

    print(
        "Starting threads for Flask and MQTT",
        flush=True
    )

    flask_process = multiprocessing.Process(
        target=run_webui
    )

    mqtt_process = multiprocessing.Process(
        target=run_mqtt_client
    )

    flask_process.start()
    mqtt_process.start()

    # Watchdog
    while flask_process.is_alive():

        mqtt_process.join(timeout=30)

        if not mqtt_process.is_alive():

            print(
                "WARNING: MQTT subprocess exited unexpectedly — restarting",
                flush=True
            )

            mqtt_process = multiprocessing.Process(
                target=run_mqtt_client
            )

            mqtt_process.start()

    flask_process.join()
    mqtt_process.join()


if __name__ == '__main__':

    print(
        "Calling Main",
        flush=True
    )

    main()