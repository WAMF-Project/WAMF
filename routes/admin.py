"""Admin page routes for dashboard, species tools, config, password, and API tokens."""

import logging
import secrets
import sqlite3
from dataclasses import asdict
from datetime import UTC, datetime, timezone
from zoneinfo import available_timezones

import yaml
from flask import Blueprint, current_app, flash, redirect, render_template, request, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from app.config_editor import (
    get_config_path,
    load_config_from_content,
    strip_sensitive_config_blocks,
)
from app.config_forms import (
    FIELD_BY_NAME,
    SettingsFormError,
    build_settings_candidate,
    normalized_species_catalog,
    settings_species_override_rows,
    settings_values,
)
from app.config_loader import load_persisted_config, load_runtime_config
from app.config_migration import ConfigMigrationError, migrate_config
from app.config_persistence import ConfigPersistenceError
from app.config_secrets import (
    apply_secrets,
    get_secrets_path,
    load_secrets,
    merge_embedded_secrets,
    secret_updates,
)
from app.config_validation import (
    validate_config, validate_mqtt_connection_config, validate_frigate_connection_config,
)
from app.frigate_connection import (
    MESSAGES as FRIGATE_MESSAGES, FrigateProbeResult, probe_frigate_connection,
    DISCOVERY_MESSAGES, FrigateDiscoveryResult, discover_frigate_cameras,
)
from app.frigate_presentation import build_camera_presentation
from app.mqtt_connection import MESSAGES, MqttProbeResult, probe_mqtt_connection
from app.mqtt_settings import mqtt_settings_from_config
from app.mqtt_presentation import build_mqtt_presentation
from app.model_install import install_model, get_install_status
from app.model_manager import build_model_manager, select_model, ModelSelectionError
from app.process_control import INSTANCE_ID, schedule_restart
from app.retention_presentation import (
    build_retention_presentation,
    unavailable_retention_presentation,
)
from app.system_events import log_system_event

admin_bp = Blueprint('admin', __name__)
logger = logging.getLogger(__name__)


GEOGRAPHIC_TIMEZONE_AREAS = frozenset(
    {
        "Africa",
        "America",
        "Antarctica",
        "Arctic",
        "Asia",
        "Atlantic",
        "Australia",
        "Europe",
        "Indian",
        "Pacific",
    }
)


SETTINGS_SECTIONS = {
    "webui": "general",
    "storage": "storage-retention",
    "media": "storage-retention",
    "retention": "storage-retention",
    "mqtt": "mqtt",
    "frigate": "frigate",
    "classification": "classification",
    "camera": "live-view",
    "live_view": "live-view",
    "bridge": "bridge-perch",
    "perch": "bridge-perch",
    "health": "bridge-perch",
    "admin": "admin-api",
    "api": "admin-api",
}

VALIDATION_FIELD_NAMES = {
    "webui.host": "webui_host",
    "webui.port": "webui_port",
    "frigate.frigate_url": "frigate_url",
    "frigate.camera": "frigate_cameras",
    "classification.model": "classification_model",
    "classification.threshold": "classification_threshold",
    "mqtt.host": "mqtt_host",
    "mqtt.port": "mqtt_port",
    "mqtt.topic_prefix": "mqtt_topic_prefix",
    "mqtt.authentication.enabled": "mqtt_authentication_enabled",
    "mqtt.authentication.username": "mqtt_username",
    "mqtt.authentication.password": "mqtt_password",
    "mqtt.tls.enabled": "mqtt_tls_enabled",
    "mqtt.tls.insecure": "mqtt_tls_insecure",
    "mqtt.tls.ca_certs": "mqtt_tls_ca_certs",
    "retention.enabled": "retention_enabled",
    "retention.snapshots_days": "snapshots_days",
    "retention.clips_days": "clips_days",
    "retention.delete_media": "delete_media",
    "retention.orphan_scan_enabled": "orphan_scan_enabled",
    "retention.delete_orphaned_media": "delete_orphaned_media",
    "retention.system_events_days": "system_events_days",
    "retention.system_events_min_rows": "system_events_min_rows",
    "retention.schedule.enabled": "retention_schedule_enabled",
    "retention.schedule.time": "retention_schedule_time",
    "retention.schedule.timezone": "retention_schedule_timezone",
}


def _validation_response(result):
    return {
        "valid": result.is_valid,
        "ready": result.is_ready,
        "validation_errors": [str(issue) for issue in result.errors],
        "readiness_issues": [str(issue) for issue in result.readiness_issues],
        "warnings": list(result.warnings),
    }


def _validate_config_content(config_content):
    config = load_config_from_content(config_content)
    migrated = migrate_config(config).config
    secrets = load_secrets(get_secrets_path(get_config_path()))
    updated_secrets = secret_updates(migrated, secrets)
    sanitized = load_config_from_content(strip_sensitive_config_blocks(config_content))
    runtime = apply_secrets(migrate_config(sanitized).config, updated_secrets)
    return validate_config(runtime)


def _not_ready_response(result):
    details = _validation_response(result)
    details.update(
        success=False,
        error="Configuration is not ready to start WAMF detection.",
    )
    return details, 409


def _secret_is_configured(secrets_config, section, field):
    section_values = secrets_config.get(section)
    return bool(
        isinstance(section_values, dict)
        and section_values.get(field)
    )


def _settings_section_for_field(field):
    if field in FIELD_BY_NAME:
        path = FIELD_BY_NAME[field].path
        return SETTINGS_SECTIONS.get(path[0])
    if field.startswith("retention_species_"):
        return "storage-retention"
    return SETTINGS_SECTIONS.get(field.split(".", 1)[0])


def _known_species():
    import webui

    try:
        return webui.get_all_species_info()
    except Exception:
        logger.warning(
            "Species metadata is unavailable for Administration Settings",
            exc_info=True,
        )
        return []


def _retention_timezones():
    try:
        geographic_timezones = sorted(
            timezone
            for timezone in available_timezones()
            if timezone.partition("/")[0] in GEOGRAPHIC_TIMEZONE_AREAS
        )
        return ["UTC", *geographic_timezones]
    except Exception:
        logger.warning(
            "Timezone suggestions are unavailable for Administration Settings",
            exc_info=True,
        )
        return []


def _species_override_context(config, submitted_form=None):
    known_by_name = normalized_species_catalog(_known_species())
    rows = settings_species_override_rows(config, submitted_form)
    configured_names = set()
    for row in rows:
        scientific_name = row["scientific_name"]
        normalized_name = (
            scientific_name.strip().casefold()
            if isinstance(scientific_name, str)
            else ""
        )
        configured_names.add(normalized_name)
        metadata = known_by_name.get(normalized_name)
        row["common_name"] = metadata.get("common_name") if metadata else None
        row["metadata_cached"] = metadata is not None

    choices = []
    for normalized_name, item in known_by_name.items():
        if normalized_name in configured_names:
            continue
        choices.append(
            {
                "scientific_name": item["scientific_name"],
                "common_name": item.get("common_name"),
            }
        )
    choices.sort(
        key=lambda item: (
            str(item.get("common_name") or "").casefold(),
            item["scientific_name"].casefold(),
            item["scientific_name"],
        )
    )

    return rows, choices


def _settings_context(config, submitted_form=None, errors=(), field_errors=None):
    secrets_path = get_secrets_path(get_config_path())
    extracted = merge_embedded_secrets(config, load_secrets(secrets_path))
    secret_config = extracted.secrets
    configured_settings = settings_values(config)
    error_section = None
    if field_errors:
        error_section = _settings_section_for_field(next(iter(field_errors)))

    species_override_rows, species_override_choices = _species_override_context(
        config, submitted_form
    )
    # Rows are renumbered after validation; move errors with their original fields.
    species_field_names = {}
    for row in species_override_rows:
        if "submitted_row_id" not in row:
            continue
        for field in ("scientific_name", "snapshots_days", "clips_days"):
            species_field_names[f"retention_species_{field}_{row['submitted_row_id']}"] = (
                f"retention_species_{field}_{row['row_id']}"
            )
    field_errors = {
        species_field_names.get(field, field): message
        for field, message in (field_errors or {}).items()
    }

    return {
        "settings": settings_values(config, submitted_form),
        "model_manager": _model_manager_state(config),
        "classification_model_original": (configured_settings['classification_model']
            if isinstance(config.get('classification'), dict) and 'model' in config['classification'] else ''),
        "config_version": migrate_config(config).target_version,
        "errors": list(errors),
        "field_errors": field_errors or {},
        "error_section": error_section,
        "mqtt_username_configured": _secret_is_configured(
            secret_config, "mqtt", "username"
        ),
        "mqtt_password_configured": _secret_is_configured(
            secret_config, "mqtt", "password"
        ),
        "admin_password_configured": _secret_is_configured(
            secret_config, "admin", "password_hash"
        ),
        "session_secret_configured": _secret_is_configured(
            secret_config, "admin", "session_secret"
        ),
        "api_token_configured": _secret_is_configured(
            secret_config, "api", "token_hash"
        ),
        "restart_instance_id": INSTANCE_ID,
        "retention_timezones": _retention_timezones(),
        "species_override_rows": species_override_rows,
        "species_override_choices": species_override_choices,
        "species_override_next_row": len(species_override_rows),
        "species_snapshot_default": configured_settings["snapshots_days"],
        "species_clip_default": configured_settings["clips_days"],
    }


def _render_settings(config, *, submitted_form=None, errors=(), field_errors=None, status=200):
    return (
        render_template(
            "admin_config.html",
            **_settings_context(config, submitted_form, errors, field_errors),
        ),
        status,
    )


def _settings_validation_errors(result):
    messages = [str(issue) for issue in result.errors]
    field_errors = {}
    for issue in result.errors:
        field_name = VALIDATION_FIELD_NAMES.get(issue.field, issue.field)
        if issue.field.startswith("retention.species_overrides"):
            field_name = "retention_species_editor"
        field_errors[field_name] = issue.message
    return messages, field_errors


@admin_bp.route('/admin')
def admin_dashboard():
    import webui

    try:
        stats = webui.get_admin_stats()
    except (OSError, ValueError, TypeError, AttributeError, sqlite3.Error):
        stats = dict(total_detections='Unavailable', archived_snapshots='Unavailable',
                     archived_clips='Unavailable', archive_size_mb='Unavailable')
    health = webui.get_system_health()
    try:
        events = webui.get_recent_system_events()
    except (OSError, ValueError, TypeError, AttributeError, sqlite3.Error):
        events = []
    try:
        retention_status = webui.get_retention_status()
        status_available = True
    except Exception:
        # Do not log raw exceptions: retention diagnostics may contain secrets.
        logger.warning("Retention status is unavailable for System Health")
        retention_status = None
        status_available = False
    try:
        retention_config = load_runtime_config()
    except Exception:
        logger.warning("Retention schedule configuration is unavailable for System Health")
        retention_config = None
    mqtt = health.get('mqtt_runtime') or build_mqtt_presentation(None, retention_config)
    try:
        retention = build_retention_presentation(
            retention_status, retention_config, status_available=status_available
        )
    except Exception:
        logger.warning("Retention presentation is unavailable for System Health")
        retention = unavailable_retention_presentation()

    return render_template(
        'admin.html',
        stats=stats,
        health=health,
        events=events,
        retention=retention,
        mqtt=mqtt,
        cameras=build_camera_presentation(health.get('frigate_cameras', {}), retention_config),
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
    return _render_settings(load_persisted_config())


def _model_manager_state(config=None):
    reader = current_app.extensions.get('mqtt_runtime_status')
    try:
        runtime = reader() if reader is not None else None
    except (EOFError, OSError, RuntimeError, ValueError):
        runtime = None  # Diagnostic loss must be unknown, never inferred.
    try:
        return build_model_manager(load_persisted_config() if config is None else config, runtime)
    except (ValueError, OSError, ConfigMigrationError):
        return {
            "models": [], "running_label": "Unknown — current classifier status is unavailable.",
            "selected_label": "Unavailable", "restart_required": False, "running_known": False,
            "selected_error": "Model catalogue or settings could not be read. Refresh and review configuration.",
            "custom": False,
        }


@admin_bp.route('/admin/config/models')
def model_manager_status():
    return _model_manager_state()


@admin_bp.route('/admin/config/models/install-status')
def model_install_status():
    snapshot = get_install_status()
    operation = asdict(snapshot) if snapshot is not None else None
    if operation is not None:
        operation['percent'] = snapshot.percent
    return {"operation": operation}


@admin_bp.route('/admin/config/models/<model_id>/<version>/install', methods=['POST'])
def install_classifier_model(model_id, version):
    # Request-owned M2 work; another Flask request can poll its shared snapshot.
    # No thread pool, durable queue, config update or automatic selection.
    result = install_model(model_id, version)
    return {"success": result.success, **asdict(result)}, (409 if result.code == 'install_in_progress' else 200)


@admin_bp.route('/admin/config/models/<model_id>/<version>/select', methods=['POST'])
def select_classifier_model(model_id, version):
    import webui

    try:
        select_model(model_id, version, reload_callback=webui.load_config)
    except ModelSelectionError as exc:
        return {"success": False, "code": exc.code, "message": exc.message}, 409
    except ConfigPersistenceError:
        logger.error("Classifier selection could not be persisted")
        return {"success": False, "code": "save_failed", "message": "Model selection could not be saved. The previous selection is unchanged."}, 500
    except (ValueError, OSError, ConfigMigrationError):
        return {"success": False, "code": "configuration_error", "message": "Model selection could not be validated. Review configuration and try again."}, 409
    manager = _model_manager_state()
    message = ("Model selection saved. This model is already running."
               if manager['running_known'] and not manager['restart_required']
               else "Model selection saved. Restart WAMF to apply it.")
    return {"success": True, "message": message, "manager": manager}


def _save_settings_form():
    import webui

    current_config = load_persisted_config()

    try:
        candidate = build_settings_candidate(
            current_config,
            request.form,
            known_species=_known_species(),
        )
        candidate_content = yaml.safe_dump(candidate, sort_keys=False)
        result = _validate_config_content(candidate_content)
        if not result.is_valid:
            messages, field_errors = _settings_validation_errors(result)
            return _render_settings(
                current_config,
                submitted_form=request.form,
                errors=messages,
                field_errors=field_errors,
                status=400,
            )

        def update_saved_settings(current):
            nonlocal result
            fresh = build_settings_candidate(current, request.form, known_species=_known_species())
            result = _validate_config_content(yaml.safe_dump(fresh, sort_keys=False))
            if not result.is_valid:
                raise ValueError("Settings changed during validation")
            return fresh

        webui.write_config_preserving_admin(
            candidate_content, reload_callback=webui.load_config, update=update_saved_settings,
        )
        log_system_event("INFO", "CONFIG", "Settings updated via Administration UI")
        if result.is_ready:
            flash("Settings saved. Restart WAMF to apply deployment changes.")
        else:
            flash(
                "Settings saved, but setup is incomplete. Complete the required "
                "settings before restarting WAMF."
            )
        section = request.form.get("active_section", "general")
        if section not in set(SETTINGS_SECTIONS.values()):
            section = "general"
        return redirect(url_for("admin.admin_config") + f"#{section}")
    except SettingsFormError as exc:
        return _render_settings(
            current_config,
            submitted_form=request.form,
            errors=[exc.message],
            field_errors={exc.field: exc.message},
            status=400,
        )
    except (yaml.YAMLError, ConfigMigrationError, ValueError):
        return _render_settings(
            current_config,
            submitted_form=request.form,
            errors=["Settings could not be validated."],
            status=400,
        )
    except ConfigPersistenceError:
        logger.error("Administration settings save failed during persistence")
        return _render_settings(
            current_config,
            submitted_form=request.form,
            errors=["Settings could not be saved."],
            status=500,
        )


@admin_bp.route('/admin/config/save', methods=['POST'])
def save_config():
    import webui

    if not request.is_json:
        return _save_settings_form()

    data = request.get_json() or {}
    config_content = data.get(
        'config_content',
        ''
    )

    try:
        result = _validate_config_content(config_content)
        if not result.is_valid:
            details = _validation_response(result)
            details.update(
                success=False,
                error="Configuration contains structural validation errors.",
            )
            return details, 400

        webui.write_config_preserving_admin(
            config_content,
            reload_callback=webui.load_config
        )

        log_system_event(
            "INFO",
            "CONFIG",
            "Configuration updated via admin editor"
        )

        response = {
            "success": True,
            "message": "Configuration saved. Restart WAMF to apply deployment changes.",
            "restart_required": True,
        }
        response.update(_validation_response(result))
        if not result.is_ready:
            response["message"] = (
                "Configuration saved, but setup is incomplete. Complete the required "
                "settings before restarting WAMF."
            )
        return response

    except yaml.YAMLError as e:
        return {
            "success": False,
            "error": "Configuration YAML is invalid."
        }, 400
    except ConfigMigrationError as exc:
        return {
            "success": False,
            "error": str(exc),
        }, 400
    except ConfigPersistenceError:
        logger.error("Admin configuration save failed during persistence")
        return {
            "success": False,
            "error": "Configuration could not be saved.",
        }, 500


@admin_bp.route('/admin/config/restart-status')
def restart_status():
    # A new value proves the parent re-executed, rather than just the old UI
    # continuing to answer during the response grace period.
    return {"instance_id": INSTANCE_ID}, 200, {"Cache-Control": "no-store"}


@admin_bp.after_request
def mqtt_test_no_store(response):
    # Includes authentication redirects and CSRF failures from before_request.
    if request.endpoint in {
        'admin.test_mqtt_connection', 'admin.test_frigate_connection',
        'admin.discover_frigate_camera_options',
        'admin.model_manager_status', 'admin.model_install_status',
        'admin.install_classifier_model', 'admin.select_classifier_model',
    }:
        response.headers['Cache-Control'] = 'no-store'
    return response


@admin_bp.route('/admin/config/frigate/cameras/discover', methods=['POST'])
def discover_frigate_camera_options():
    """Discover through the saved connection without consuming form overrides."""
    code = 'configuration_error'
    try:
        config = load_runtime_config()
        if validate_frigate_connection_config(config).is_ready:
            code = 'internal_error'
            return asdict(discover_frigate_cameras(config['frigate']['frigate_url']))
    except Exception:
        pass  # Never expose config, URLs, or remote exception data.
    return asdict(FrigateDiscoveryResult(
        success=False, code=code, safe_message=DISCOVERY_MESSAGES[code], duration_ms=0,
        completed_at=datetime.now(UTC).isoformat(), cameras=None,
    ))


@admin_bp.route('/admin/config/frigate/test', methods=['POST'])
def test_frigate_connection():
    """Test saved settings only; ignore all submitted connection overrides."""
    code = 'configuration_error'
    try:
        config = load_runtime_config()
        if validate_frigate_connection_config(config).is_ready:
            code = 'internal_error'
            return asdict(probe_frigate_connection(config['frigate']['frigate_url']))
    except Exception:
        pass  # URLs, configuration and probe exceptions can contain secrets.
    return asdict(FrigateProbeResult(
        success=False, code=code, safe_message=FRIGATE_MESSAGES[code], duration_ms=0,
        completed_at=datetime.now(UTC).isoformat(),
    ))


@admin_bp.route('/admin/config/mqtt/test', methods=['POST'])
def test_mqtt_connection():
    """Test saved settings only; never consume form/JSON connection overrides."""
    code = 'configuration_error'
    try:
        config = load_runtime_config()
        if validate_mqtt_connection_config(config).is_ready:
            settings = mqtt_settings_from_config(config)
            code = 'internal_error'
            return asdict(probe_mqtt_connection(settings))
    except Exception:
        # Neither configuration nor probe exceptions may disclose secret values.
        pass
    return asdict(MqttProbeResult(
        success=False, code=code, safe_message=MESSAGES[code], duration_ms=0,
        completed_at=datetime.now(timezone.utc).isoformat(),
    ))


@admin_bp.route('/admin/config/restart', methods=['POST'])
def restart_wamf():
    try:
        result = validate_config(load_runtime_config())
    except yaml.YAMLError as exc:
        return {"success": False, "error": "Configuration YAML is invalid."}, 400
    except ConfigMigrationError as exc:
        return {"success": False, "error": str(exc)}, 409
    if not result.is_valid:
        details = _validation_response(result)
        details.update(
            success=False,
            error="Configuration contains structural validation errors.",
        )
        return details, 409
    if not result.is_ready:
        return _not_ready_response(result)

    log_system_event(
        "INFO",
        "SYSTEM",
        "WAMF restart requested via admin editor"
    )
    try:
        schedule_restart()
    except RuntimeError as exc:
        return {"success": False, "error": str(exc)}, 409

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
        result = _validate_config_content(config_content)
        if not result.is_valid:
            details = _validation_response(result)
            details.update(
                success=False,
                error="Configuration contains structural validation errors.",
            )
            return details, 400
        if not result.is_ready:
            return _not_ready_response(result)

        webui.write_config_preserving_admin(config_content)

        log_system_event(
            "INFO",
            "CONFIG",
            "Configuration updated; WAMF restart requested"
        )
        schedule_restart()

        response = {
            "success": True,
            "message": "Configuration saved. WAMF will restart shortly."
        }
        response.update(_validation_response(result))
        return response
    except RuntimeError as exc:
        return {"success": False, "error": str(exc)}, 409
    except yaml.YAMLError as e:
        return {
            "success": False,
            "error": "Configuration YAML is invalid."
        }, 400
    except ConfigMigrationError as exc:
        return {
            "success": False,
            "error": str(exc),
        }, 400
    except ConfigPersistenceError:
        logger.error("Admin save-and-restart failed during persistence")
        return {
            "success": False,
            "error": "Configuration could not be saved.",
        }, 500


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
                generate_password_hash(new_password, method='pbkdf2:sha256:600000'),
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
            generate_password_hash(generated_token, method='pbkdf2:sha256:600000'),
            reload_callback=webui.load_config
        )
        flash('API token generated. Store it now; it cannot be recovered later.')

    return render_template(
        'admin_api_token.html',
        token_auth_enabled=webui.is_api_token_auth_enabled(),
        token_configured=bool(webui.get_api_token_hash()),
        generated_token=generated_token
    )
