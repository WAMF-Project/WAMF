"""Detection mutation routes, including durable grouped-species suppression."""

import sqlite3

from flask import Blueprint, jsonify

from app import review_repository as repository
from app.archive_media import unreferenced_archive_paths
from app.db import connect_db
from app.media_coordination import media_activity_guard

detections_bp = Blueprint('detections', __name__)


@detections_bp.route('/detections/<frigate_event>', methods=['DELETE'])
def delete_detection(frigate_event):
    import webui

    if not frigate_event:
        return jsonify({'success': False, 'message': 'Missing detection identifier.'}), 400
    conn = None
    deleted_media = []
    try:
        with media_activity_guard(webui.DBPATH):
            conn = connect_db(webui.DBPATH)
            conn.execute('BEGIN IMMEDIATE')
            rows = conn.execute('SELECT * FROM detections WHERE frigate_event=?', (frigate_event,)).fetchall()
            if not rows:
                return jsonify({'success': False, 'message': 'Detection not found.'}), 404
            for row in rows:
                if row['frigate_review_id'] is not None:
                    repository.suppress_species(conn, row['frigate_review_id'], row['display_name'])
                conn.execute('UPDATE frigate_review_events SET observation_id=NULL WHERE observation_id=?', (row['id'],))
            conn.execute('DELETE FROM detections WHERE frigate_event=?', (frigate_event,))
            conn.commit()
            candidates = [row[column] for row in rows for column in ('wamf_snapshot_path', 'wamf_clip_path')]
            unreferenced = unreferenced_archive_paths(conn, candidates)
            for row in rows:
                paths = [row[column] if row[column] in unreferenced else None
                         for column in ('wamf_snapshot_path', 'wamf_clip_path')]
                deleted_media.extend(webui.delete_wamf_media_files(*paths))
    except sqlite3.Error:
        webui.logger.warning('Detection deletion failed; committed suppression is retained if present')
        return jsonify({'success': False, 'message': 'Unable to delete detection.'}), 500
    except OSError:
        return jsonify({'success': False, 'message': 'Unable to delete detection media.'}), 500
    finally:
        if conn:
            conn.close()
    return jsonify({'success': True, 'message': 'Detection deleted.',
                    'frigate_event': frigate_event, 'deleted_media': deleted_media}), 200
