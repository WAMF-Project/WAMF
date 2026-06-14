"""Media routes for Frigate proxy responses and archived WAMF files."""

from flask import Blueprint, send_file

from app.frigate_proxy import proxy_frigate_media


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
def wamf_snapshot(filename):
    return send_file(
        f"media/wamf/snapshots/{filename}"
    )


@media_bp.route('/wamf/clip/<path:filename>')
def wamf_clip(filename):
    return send_file(
        f"media/wamf/clips/{filename}"
    )
