"""Admin API routes that accept session auth or a configured API token."""

from flask import Blueprint, jsonify

from version import VERSION


admin_api_bp = Blueprint('admin_api', __name__)


@admin_api_bp.route('/admin/api/health')
def admin_health():
    import webui

    health = webui.get_system_health()
    retention = webui.get_retention_status()

    return jsonify({
        "version": VERSION,

        "frigate": health["frigate_online"],
        "mqtt": health["mqtt_online"],
        "database": health["database_healthy"],
        "archive_storage": health["archive_writable"],
        "disk_used_percent": health["disk_used_percent"],

        "last_retention_run": (
            retention["last_run"]
            if retention else None
        ),

        "orphan_count": (
            retention["orphan_count"]
            if retention else None
        ),

        "missing_count": (
            retention["missing_count"]
            if retention else None
        )
    })
