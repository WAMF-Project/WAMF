from pathlib import Path
import logging
import sqlite3

import requests
import yaml
import paho.mqtt.client as mqtt
import shutil
from app.db import connect_db, DB_PATH as DEFAULT_DB_PATH

DB_PATH = DEFAULT_DB_PATH
logger = logging.getLogger(__name__)

def load_config():

    with open("config/config.yml", "r") as f:
        return yaml.safe_load(f)

def get_system_health():

    health = {}

    config = load_config()

    mqtt_host = config["frigate"]["mqtt_server"]
    mqtt_port = config["frigate"]["mqtt_port"]

    frigate_url = config["frigate"]["frigate_url"]

    # Frigate connectivity
    try:

        response = requests.get(
            f"{frigate_url}/api/version",
            timeout=5
        )

        health["frigate_online"] = (
            response.status_code == 200
        )

    except requests.exceptions.RequestException as exc:
        logger.debug("Frigate health check failed: %s", exc)

        health["frigate_online"] = False

    # Frigate media storage
    try:

        total, used, free = shutil.disk_usage(
            "/media/frigate"
        )

        frigate_used_percent = round(
            (used / total) * 100,
            1
        )

        health["frigate_disk_percent"] = (
            frigate_used_percent
        )

    except OSError as exc:
        logger.debug("Frigate disk usage check failed: %s", exc)

        health["frigate_disk_percent"] = None
        # MQTT connectivity
    try:

        client = mqtt.Client()

        client.connect(
            mqtt_host,
            mqtt_port,
            5
        )

        client.disconnect()

        health["mqtt_online"] = True

    except OSError as exc:
        logger.debug("MQTT health check failed: %s", exc)

        health["mqtt_online"] = False

    # Database connectivity
    try:

        conn = connect_db(DB_PATH, row_factory=False)

        conn.execute("SELECT 1")

        conn.close()

        health["database_healthy"] = True

    except sqlite3.Error as exc:
        logger.debug("Database health check failed: %s", exc)

        health["database_healthy"] = False

    

        # Disk usage
    total, used, free = shutil.disk_usage("/")

    used_percent = round(
        (used / total) * 100,
        1
    )

    health["disk_used_percent"] = used_percent

    # Archive directories
    snapshot_dir = Path(
        "media/wamf/snapshots"
    )

    clip_dir = Path(
        "media/wamf/clips"
    )

    health["archive_writable"] = (
        snapshot_dir.exists()
        and clip_dir.exists()
    )

    return health
