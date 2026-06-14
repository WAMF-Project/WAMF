"""Detection mutation routes, including admin-protected delete behavior."""

import sqlite3

from flask import Blueprint, jsonify

from app.db import connect_db


detections_bp = Blueprint('detections', __name__)


@detections_bp.route('/detections/<frigate_event>', methods=['DELETE'])
def delete_detection(frigate_event):
    import webui

    if not frigate_event:
        return jsonify({"success": False, "message": "Missing detection identifier."}), 400

    conn = None

    try:
        conn = connect_db(webui.DBPATH)
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT wamf_snapshot_path, wamf_clip_path
            FROM detections
            WHERE frigate_event = ?
            """,
            (frigate_event,)
        )
        detection = cursor.fetchone()

        if detection is None:
            return jsonify({"success": False, "message": "Detection not found."}), 404

        deleted_media = webui.delete_wamf_media_files(
            detection["wamf_snapshot_path"],
            detection["wamf_clip_path"]
        )

        cursor.execute(
            "DELETE FROM detections WHERE frigate_event = ?",
            (frigate_event,)
        )
        conn.commit()

    except sqlite3.Error as e:
        webui.logger.warning("Error deleting detection %s: %s", frigate_event, e)
        return jsonify({"success": False, "message": "Unable to delete detection."}), 500

    except OSError as e:
        webui.logger.warning("Error deleting media for detection %s: %s", frigate_event, e)
        return jsonify({"success": False, "message": "Unable to delete detection media."}), 500

    finally:
        if conn:
            conn.close()

    return jsonify({
        "success": True,
        "message": "Detection deleted.",
        "frigate_event": frigate_event,
        "deleted_media": deleted_media
    }), 200
