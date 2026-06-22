"""Media routes for Frigate proxy responses and archived WAMF files."""

from flask import Blueprint, send_from_directory

from app.frigate_proxy import proxy_frigate_media
from wamf_paths import get_clips_path, get_snapshots_path


media_bp = Blueprint('media', __name__)


@media_bp.route('/frigate/<frigate_event>/thumbnail.jpg')
def frigate_thumbnail(frigate_event):
    import webui

    return proxy_frigate_media(
        webui.config['frigate']['frigate_url'],
        frigate_event,
        'thumbnail.jpg',
        timeout=5
    )


@media_bp.route('/frigate/<frigate_event>/snapshot.jpg')
def frigate_snapshot(frigate_event):
    import webui

    return proxy_frigate_media(
        webui.config['frigate']['frigate_url'],
        frigate_event,
        'snapshot.jpg',
        timeout=5
    )


@media_bp.route('/frigate/<frigate_event>/clip.mp4')
def frigate_clip(frigate_event):
    import webui

    return proxy_frigate_media(
        webui.config['frigate']['frigate_url'],
        frigate_event,
        'clip.mp4',
        timeout=30
    )


@media_bp.route('/wamf/snapshot/<path:filename>')
@media_bp.route('/media/snapshots/<path:filename>')
def wamf_snapshot(filename):
    return send_from_directory(
        get_snapshots_path(),
        filename
    )


@media_bp.route('/wamf/clip/<path:filename>')
@media_bp.route('/media/clips/<path:filename>')
def wamf_clip(filename):
    return send_from_directory(
        get_clips_path(),
        filename
    )
