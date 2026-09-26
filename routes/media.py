"""Media routes for archived WAMF files."""

from flask import Blueprint, send_from_directory

from wamf_paths import get_clips_path, get_snapshots_path

media_bp = Blueprint('media', __name__)


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
