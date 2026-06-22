"""Admin page routes for dashboard, species tools, config, password, and API tokens."""

import secrets

import yaml
from flask import Blueprint, flash, redirect, render_template, request, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from app.config_editor import (
    get_config_file_metadata,
    get_config_path,
    strip_admin_config_block,
)
from app.system_events import log_system_event
from app.process_control import schedule_restart


admin_bp = Blueprint('admin', __name__)


@admin_bp.route('/admin')
def admin_dashboard():
    import webui

    stats = webui.get_admin_stats()
    health = webui.get_system_health()
    events = webui.get_recent_system_events()
    retention_status = webui.get_retention_status()

    return render_template(
        'admin.html',
        stats=stats,
        health=health,
        events=events,
        retention_status=retention_status
    )


@admin_bp.route('/admin/logs')
def admin_logs():
    import webui

    event_type = request.args.get("filter")
    logs = webui.get_recent_system_events(
        100,
        event_type
    )

    return render_template(
        'admin_logs.html',
        logs=logs,
        current_filter=event_type
    )


@admin_bp.route('/admin/species')
def admin_species():
    import webui

    species = webui.get_all_species_info()
    species_count = len(species)
    missing_description = sum(
        1
        for s in species
        if not s["description"]
    )
    missing_thumbnail = sum(
        1
        for s in species
        if not s["thumbnail_url"]
    )
    latest_update = None

    if species:
        latest_update = max(
            s["last_updated"]
            for s in species
            if s["last_updated"]
        )

    return render_template(
        'admin_species.html',
        species=species,
        species_count=species_count,
        missing_description=missing_description,
        missing_thumbnail=missing_thumbnail,
        latest_update=latest_update
    )


def refresh_species_metadata(scientific_name):
    import webui

    webui.refresh_species_metadata_task(scientific_name)


@admin_bp.route('/admin/species/refresh/<path:scientific_name>', methods=['POST'])
def refresh_species(scientific_name):
    refresh_species_metadata(scientific_name)

    log_system_event(
        "INFO",
        "SPECIES",
        f"Refreshed metadata for {scientific_name}"
    )

    return redirect(url_for('admin.admin_species'))


@admin_bp.route('/admin/species/refresh-missing', methods=['POST'])
def refresh_missing_species():
    import webui

    species = webui.get_all_species_info()
    refreshed = 0

    for item in species:
        if (
            not item["description"]
            or not item["thumbnail_url"]
        ):
            refresh_species_metadata(
                item["scientific_name"]
            )
            refreshed += 1

    log_system_event(
        "INFO",
        "SPECIES",
        f"Refreshed metadata for {refreshed} species with missing data"
    )

    return redirect(url_for('admin.admin_species'))


@admin_bp.route('/admin/species/refresh-all', methods=['POST'])
def refresh_all_species():
    import webui

    species = webui.get_all_species_info()

    for item in species:
        refresh_species_metadata(
            item["scientific_name"]
        )

    log_system_event(
        "INFO",
        "SPECIES",
        f"Refreshed metadata for {len(species)} species"
    )

    return redirect(url_for('admin.admin_species'))


@admin_bp.route('/admin/config')
def admin_config():
    with open(
        get_config_path(),
        'r'
    ) as config_file:
        config_content = strip_admin_config_block(config_file.read())

    metadata = get_config_file_metadata()

    return render_template(
        'admin_config.html',
        config_content=config_content,
        config_path=metadata['config_path'],
        file_size=metadata['file_size'],
        last_modified=metadata['last_modified'],
        backup_count=metadata['backup_count']
    )


@admin_bp.route('/admin/config/save', methods=['POST'])
def save_config():
    import webui

    data = request.get_json()
    config_content = data.get(
        'config_content',
        ''
    )

    try:
        webui.write_config_preserving_admin(
            config_content,
            reload_callback=webui.load_config
        )

        log_system_event(
            "INFO",
            "CONFIG",
            "Configuration updated via admin editor"
        )

        return {
            "success": True,
            "message": "Configuration updated"
        }

    except yaml.YAMLError as e:
        return {
            "success": False,
            "error": str(e)
        }


@admin_bp.route('/admin/config/restart', methods=['POST'])
def restart_wamf():
    log_system_event(
        "INFO",
        "SYSTEM",
        "WAMF restart requested via admin editor"
    )
    schedule_restart()

    return {
        "success": True,
        "message": "Restart requested. WAMF will be available again shortly."
    }


@admin_bp.route('/admin/config/save-and-restart', methods=['POST'])
def save_and_restart_wamf():
    import webui

    data = request.get_json() or {}
    config_content = data.get('config_content', '')

    try:
        webui.write_config_preserving_admin(config_content)

        log_system_event(
            "INFO",
            "CONFIG",
            "Configuration updated; WAMF restart requested"
        )
        schedule_restart()

        return {
            "success": True,
            "message": "Configuration saved. WAMF will restart shortly."
        }
    except yaml.YAMLError as e:
        return {
            "success": False,
            "error": str(e)
        }


@admin_bp.route('/admin/password', methods=['GET', 'POST'])
def change_password():
    import webui

    error = None

    if request.method == 'POST':
        current_password = request.form.get('current_password', '')
        new_password = request.form.get('new_password', '')
        confirm_password = request.form.get('confirm_password', '')

        if not check_password_hash(webui.get_admin_password_hash() or '', current_password):
            error = 'Password change failed.'
        elif len(new_password) < 8:
            error = 'New password must be at least 8 characters.'
        elif new_password != confirm_password:
            error = 'New passwords do not match.'
        else:
            webui.update_admin_password_hash(
                generate_password_hash(new_password),
                reload_callback=webui.load_config
            )
            flash('Admin password updated.')
            return redirect(url_for('admin.change_password'))

    return render_template(
        'admin_password.html',
        error=error
    )


@admin_bp.route('/admin/api-token', methods=['GET', 'POST'])
def admin_api_token():
    import webui

    generated_token = None

    if request.method == 'POST':
        generated_token = secrets.token_urlsafe(32)
        webui.update_api_token_hash(
            generate_password_hash(generated_token),
            reload_callback=webui.load_config
        )
        flash('API token generated. Store it now; it cannot be recovered later.')

    return render_template(
        'admin_api_token.html',
        token_auth_enabled=webui.is_api_token_auth_enabled(),
        token_configured=bool(webui.get_api_token_hash()),
        generated_token=generated_token
    )
