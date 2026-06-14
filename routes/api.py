"""Public API routes that intentionally stay outside admin authentication."""

from flask import Blueprint, jsonify


api_bp = Blueprint('api', __name__)


@api_bp.route('/api/detections/recent')
def api_recent_detections():
    from flask import request
    import webui

    limit = request.args.get('limit', 5, type=int)
    records = webui.recent_detections(min(limit, 20))  # cap at 20
    return jsonify(records)
