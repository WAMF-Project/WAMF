"""Focused coverage for the forms-based Administration Settings UI."""

from pathlib import Path
import re

import pytest
import yaml


def ready_config():
    return {
        "config_version": 2,
        "frigate": {
            "frigate_url": "http://frigate:5000",
            "camera": ["garden", "perch"],
            "object": "bird",
        },
        "mqtt": {
            "host": "broker.local",
            "port": 1883,
            "topic_prefix": "frigate",
            "authentication": {"enabled": True},
            "tls": {"enabled": False, "insecure": False},
        },
        "classification": {"model": "model.tflite", "threshold": 0.7},
        "bridge": {
            "enabled": False,
            "events_url": "http://bridge:5000/api/events",
            "timeout_seconds": 1,
            "health_check_interval_seconds": 60,
        },
        "webui": {"host": "0.0.0.0", "port": 7767},
        "storage": {"database_path": "./data/speciesid.db"},
        "media": {
            "snapshots_path": "./media/wamf/snapshots",
            "clips_path": "./media/wamf/clips",
        },
        "camera": {"live_view_url": "https://camera.example/live"},
        "retention": {
            "enabled": True,
            "snapshots_days": 90,
            "clips_days": 30,
            "delete_media": False,
            "orphan_scan_enabled": True,
            "delete_orphaned_media": False,
            "system_events_days": 90,
            "system_events_min_rows": 1000,
            "config_backups_max_files": 10,
            "species_overrides": {},
        },
        "admin": {
            "auth_enabled": False,
            "session_cookie_samesite": "Lax",
            "session_cookie_secure": False,
        },
        "api": {"token_auth_enabled": True},
    }


def configure_paths(tmp_path, monkeypatch, *, config=None, secrets=None):
    config_path = tmp_path / "config.yml"
    config_path.write_text(yaml.safe_dump(config or ready_config(), sort_keys=False))
    secrets_path = tmp_path / "secrets.yml"
    secrets_path.write_text(
        yaml.safe_dump(secrets or {"secrets_version": 1}, sort_keys=False)
    )
    monkeypatch.setenv("WHOSATMYFEEDER_CONFIG", str(config_path))
    return config_path, secrets_path


def disable_runtime_reload(monkeypatch):
    import webui

    monkeypatch.setattr(webui, "load_config", lambda: None)
    monkeypatch.setattr(
        webui,
        "config",
        {"admin": {"auth_enabled": False}},
    )


def test_settings_page_renders_canonical_values_and_sections(
    flask_client, tmp_path, monkeypatch
):
    configure_paths(tmp_path, monkeypatch)
    disable_runtime_reload(monkeypatch)

    response = flask_client.get("/admin/config")

    assert response.status_code == 200
    for heading in (
        b"General",
        b"Storage &amp; Retention",
        b"MQTT",
        b"Frigate",
        b"Classification",
        b"Live View",
        b"Bridge / Perch",
        b"Admin &amp; API",
    ):
        assert heading in response.data
    assert b"Advanced" not in response.data
    assert b'name="mqtt_host"' in response.data
    assert b'value="broker.local"' in response.data
    assert b"garden\nperch" in response.data
    assert b'value="0.7"' in response.data


def test_frigate_fields_render_refined_labels_and_help(
    flask_client, tmp_path, monkeypatch
):
    configure_paths(tmp_path, monkeypatch)
    disable_runtime_reload(monkeypatch)

    html = flask_client.get("/admin/config").get_data(as_text=True)
    frigate = html.split('id="frigate"', 1)[1].split("</section>", 1)[0]

    assert '<label for="frigate_url">Frigate URL</label>' in frigate
    assert 'name="frigate_url" type="url"' in frigate
    assert 'aria-describedby="frigate_url_help"' in frigate
    assert (
        '<small id="frigate_url_help">Frigate server URL used by WAMF '
        'for API requests and media retrieval.</small>'
    ) in frigate
    assert '<label for="frigate_cameras">Monitored cameras</label>' in frigate
    assert '<label for="frigate_cameras">Camera names</label>' not in frigate
    assert 'name="frigate_cameras" rows="4"' in frigate
    assert (
        '<small id="frigate_cameras_help">Enter one Frigate camera name per '
        'line.</small>'
    ) in frigate
    assert '<label for="frigate_object">Tracked object</label>' in frigate
    assert 'name="frigate_object" type="text"' in frigate
    assert 'aria-describedby="frigate_object_help"' in frigate
    assert (
        '<small id="frigate_object_help">Frigate object label WAMF processes '
        'from monitored cameras.</small>'
    ) in frigate


def test_settings_page_never_renders_secret_values_or_secret_storage_fields(
    flask_client, tmp_path, monkeypatch
):
    sentinels = (
        "SECRET-MQTT-USER",
        "SECRET-MQTT-PASSWORD",
        "SECRET-ADMIN-HASH",
        "SECRET-SESSION",
        "SECRET-TOKEN-HASH",
    )
    configure_paths(
        tmp_path,
        monkeypatch,
        secrets={
            "secrets_version": 1,
            "mqtt": {"username": sentinels[0], "password": sentinels[1]},
            "admin": {"password_hash": sentinels[2], "session_secret": sentinels[3]},
            "api": {"token_hash": sentinels[4]},
        },
    )
    disable_runtime_reload(monkeypatch)

    response = flask_client.get("/admin/config")
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert all(sentinel not in html for sentinel in sentinels)
    assert "password_hash" not in html
    assert "session_secret" not in html
    assert "token_hash" not in html
    assert html.count("Configured") >= 4
    assert 'name="mqtt_username" type="password" value=""' in html
    assert 'name="mqtt_password" type="password" value=""' in html


def test_settings_secret_state_reports_not_configured(
    flask_client, tmp_path, monkeypatch
):
    configure_paths(tmp_path, monkeypatch)
    disable_runtime_reload(monkeypatch)

    html = flask_client.get("/admin/config").get_data(as_text=True)

    assert html.count("Not configured") >= 4


def test_mqtt_helpers_and_dependency_states_match_current_configuration(
    flask_client, tmp_path, monkeypatch
):
    config = ready_config()
    config["mqtt"]["topic_prefix"] = "bird-lab"
    config["mqtt"]["authentication"]["enabled"] = True
    config["mqtt"]["tls"] = {
        "enabled": True,
        "insecure": True,
        "ca_certs": "/certs/broker-ca.pem",
    }
    configure_paths(
        tmp_path,
        monkeypatch,
        config=config,
        secrets={
            "secrets_version": 1,
            "mqtt": {
                "username": "SECRET-MQTT-USER",
                "password": "SECRET-MQTT-PASSWORD",
            },
        },
    )
    disable_runtime_reload(monkeypatch)

    html = flask_client.get("/admin/config").get_data(as_text=True)
    mqtt = html.split('id="mqtt"', 1)[1].split("</section>", 1)[0]

    assert "SECRET-MQTT-USER" not in html
    assert "SECRET-MQTT-PASSWORD" not in html
    assert "Leave blank to keep the stored username." in mqtt
    assert "Leave blank to keep the stored password." in mqtt
    assert "Enter the broker username." not in mqtt
    assert "Enter the broker password." not in mqtt
    assert (
        "Frigate MQTT topic prefix. WAMF listens for events on "
        "<code>bird-lab/events</code>."
    ) in mqtt
    assert (
        "Optional path to a CA certificate used to verify the broker."
        in mqtt
    )
    assert re.search(
        r'<fieldset class="settings-dependent-fields" '
        r'data-settings-dependency="mqtt_authentication_enabled"[^>]*>',
        mqtt,
    ).group(0).endswith('>')
    assert re.search(
        r'<fieldset class="settings-dependent-fields" '
        r'data-settings-dependency="mqtt_tls_enabled"[^>]*>',
        mqtt,
    ).group(0).endswith('>')
    assert not re.search(
        r'data-settings-dependency="(?:mqtt_authentication_enabled|mqtt_tls_enabled)"'
        r'[^>]* disabled',
        mqtt,
    )


def test_mqtt_unconfigured_dependencies_render_inactive_without_secret_values(
    flask_client, tmp_path, monkeypatch
):
    config = ready_config()
    config["mqtt"]["authentication"]["enabled"] = False
    config["mqtt"]["tls"]["enabled"] = False
    configure_paths(tmp_path, monkeypatch, config=config)
    disable_runtime_reload(monkeypatch)

    html = flask_client.get("/admin/config").get_data(as_text=True)
    mqtt = html.split('id="mqtt"', 1)[1].split("</section>", 1)[0]

    assert "Enter the broker username." in mqtt
    assert "Enter the broker password." in mqtt
    assert "Leave blank to keep the stored username." not in mqtt
    assert "Leave blank to keep the stored password." not in mqtt
    for controller in ("mqtt_authentication_enabled", "mqtt_tls_enabled"):
        fieldset = re.search(
            rf'<fieldset class="settings-dependent-fields" '
            rf'data-settings-dependency="{controller}"[^>]*>',
            mqtt,
        ).group(0)
        assert " disabled" in fieldset
    assert "group.disabled = !controller.checked;" in html


def test_inactive_mqtt_fields_can_be_omitted_without_clearing_stored_values(
    flask_client, tmp_path, monkeypatch
):
    config = ready_config()
    config["mqtt"]["authentication"]["enabled"] = False
    config["mqtt"]["tls"] = {
        "enabled": False,
        "insecure": True,
        "ca_certs": "/certs/preserved-ca.pem",
    }
    config_path, secrets_path = configure_paths(
        tmp_path,
        monkeypatch,
        config=config,
        secrets={
            "secrets_version": 1,
            "mqtt": {
                "username": "preserved-user",
                "password": "preserved-password",
            },
        },
    )
    disable_runtime_reload(monkeypatch)

    response = flask_client.post(
        "/admin/config/save",
        data={
            "mqtt_authentication_enabled": "0",
            "mqtt_tls_enabled": "0",
            "active_section": "mqtt",
        },
    )

    assert response.status_code == 302
    persisted = yaml.safe_load(config_path.read_text())
    secrets = yaml.safe_load(secrets_path.read_text())
    assert persisted["mqtt"]["authentication"]["enabled"] is False
    assert persisted["mqtt"]["tls"] == {
        "enabled": False,
        "insecure": True,
        "ca_certs": "/certs/preserved-ca.pem",
    }
    assert secrets["mqtt"] == {
        "username": "preserved-user",
        "password": "preserved-password",
    }


def test_all_settings_switches_limit_labels_to_control_and_name(
    flask_client, tmp_path, monkeypatch
):
    configure_paths(tmp_path, monkeypatch)
    disable_runtime_reload(monkeypatch)

    html = flask_client.get("/admin/config").get_data(as_text=True)
    labels = re.findall(
        r'<label class="settings-switch-label"[^>]*>(.*?)</label>',
        html,
        re.DOTALL,
    )

    assert len(labels) == 12
    assert 'class="settings-switch"' not in html
    assert all("<small" not in label for label in labels)
    for field_name in (
        "mqtt_authentication_enabled",
        "mqtt_tls_enabled",
        "mqtt_tls_insecure",
        "bridge_enabled",
        "admin_auth_enabled",
        "admin_session_cookie_secure",
        "api_token_auth_enabled",
    ):
        assert html.count(f'name="{field_name}"') == 2
        assert f'id="{field_name}" type="checkbox"' in html
        assert f'for="{field_name}"' in html


def test_admin_session_cookie_samesite_has_concise_help(
    flask_client, tmp_path, monkeypatch
):
    configure_paths(tmp_path, monkeypatch)
    disable_runtime_reload(monkeypatch)

    html = flask_client.get("/admin/config").get_data(as_text=True)
    admin_api = html.split('id="admin-api"', 1)[1].split("</section>", 1)[0]

    assert '<label for="admin_session_cookie_samesite">' in admin_api
    assert 'name="admin_session_cookie_samesite"' in admin_api
    assert 'aria-describedby="admin_session_cookie_samesite_help"' in admin_api
    assert (
        '<small id="admin_session_cookie_samesite_help">Controls when the '
        'browser sends the administration session cookie with cross-site '
        'requests.</small>'
    ) in admin_api


def test_blank_mqtt_secret_submission_preserves_stored_values(
    flask_client, tmp_path, monkeypatch
):
    _, secrets_path = configure_paths(
        tmp_path,
        monkeypatch,
        secrets={
            "secrets_version": 1,
            "mqtt": {"username": "stored-user", "password": "stored-password"},
        },
    )
    disable_runtime_reload(monkeypatch)

    response = flask_client.post(
        "/admin/config/save",
        data={"mqtt_username": "", "mqtt_password": "", "active_section": "mqtt"},
    )

    assert response.status_code == 302
    assert yaml.safe_load(secrets_path.read_text())["mqtt"] == {
        "username": "stored-user",
        "password": "stored-password",
    }


def test_nonblank_mqtt_secret_submission_replaces_values_and_not_config(
    flask_client, tmp_path, monkeypatch
):
    config_path, secrets_path = configure_paths(
        tmp_path,
        monkeypatch,
        secrets={
            "secrets_version": 1,
            "mqtt": {"username": "old-user", "password": "old-password"},
        },
    )
    disable_runtime_reload(monkeypatch)

    response = flask_client.post(
        "/admin/config/save",
        data={
            "mqtt_username": "replacement-user",
            "mqtt_password": "replacement-password",
            "active_section": "mqtt",
        },
    )

    assert response.status_code == 302
    secrets = yaml.safe_load(secrets_path.read_text())
    persisted = yaml.safe_load(config_path.read_text())
    assert secrets["mqtt"] == {
        "username": "replacement-user",
        "password": "replacement-password",
    }
    assert "username" not in persisted["mqtt"]["authentication"]
    assert "password" not in persisted["mqtt"]["authentication"]
    assert persisted["config_version"] == 2
    assert secrets["secrets_version"] == 1


def test_ordinary_form_update_persists_through_shared_editor(
    flask_client, tmp_path, monkeypatch
):
    config_path, _ = configure_paths(tmp_path, monkeypatch)
    disable_runtime_reload(monkeypatch)

    response = flask_client.post(
        "/admin/config/save",
        data={
            "webui_port": "8877",
            "classification_threshold": "0.82",
            "live_view_url": "https://new-camera.example/live",
            "active_section": "classification",
        },
    )

    assert response.status_code == 302
    assert response.headers["Location"].endswith("#classification")
    persisted = yaml.safe_load(config_path.read_text())
    assert persisted["webui"]["port"] == 8877
    assert persisted["classification"]["threshold"] == 0.82
    assert persisted["camera"]["live_view_url"] == "https://new-camera.example/live"


def test_invalid_form_value_does_not_persist_and_error_is_safe(
    flask_client, tmp_path, monkeypatch
):
    config_path, secrets_path = configure_paths(
        tmp_path,
        monkeypatch,
        secrets={
            "secrets_version": 1,
            "mqtt": {"username": "old-user", "password": "old-password"},
        },
    )
    disable_runtime_reload(monkeypatch)
    with flask_client.session_transaction() as session:
        session.clear()
    original_config = config_path.read_bytes()
    original_secrets = secrets_path.read_bytes()
    submitted_secret = "SUBMITTED-SECRET-MUST-NOT-ECHO"

    response = flask_client.post(
        "/admin/config/save",
        data={
            "classification_threshold": "2",
            "mqtt_password": submitted_secret,
            "active_section": "classification",
        },
    )

    assert response.status_code == 400
    assert b"classification.threshold: must be a number from 0 to 1" in response.data
    assert b'class="settings-notice settings-notice-error" role="alert" tabindex="-1" id="settings-errors"' in response.data
    assert b'href="#classification">Review the affected section</a>' in response.data
    html = response.get_data(as_text=True)
    assert 'id="active-section" value="classification"' in html
    assert "const errors = document.getElementById('settings-errors');" in html
    assert "if (errors && errors.focus) errors.focus();" in html
    assert 'class="settings-toast"' not in html
    assert submitted_secret.encode() not in response.data
    assert config_path.read_bytes() == original_config
    assert secrets_path.read_bytes() == original_secrets


def test_invalid_structured_species_override_does_not_persist(
    flask_client, tmp_path, monkeypatch
):
    config_path, _ = configure_paths(tmp_path, monkeypatch)
    disable_runtime_reload(monkeypatch)
    original = config_path.read_bytes()

    response = flask_client.post(
        "/admin/config/save",
        data={
            "retention_species_overrides_present": "1",
            "retention_species_row": "0",
            "retention_species_scientific_name_0": "Turdus migratorius",
            "retention_species_snapshots_days_0": "-1",
            "retention_species_clips_days_0": "",
        },
    )

    assert response.status_code == 400
    assert b"Enter zero or a positive whole number" in response.data
    assert b"Turdus migratorius" in response.data
    assert config_path.read_bytes() == original


def test_legacy_mqtt_aliases_are_not_exposed_or_accepted_as_form_fields(
    flask_client, tmp_path, monkeypatch
):
    config = ready_config()
    config["frigate"]["mqtt_server"] = "legacy-broker-sentinel"
    config_path, _ = configure_paths(tmp_path, monkeypatch, config=config)
    disable_runtime_reload(monkeypatch)

    response = flask_client.get("/admin/config")
    assert b"legacy-broker-sentinel" not in response.data
    assert b'name="mqtt_server"' not in response.data

    response = flask_client.post(
        "/admin/config/save",
        data={"mqtt_server": "attempted-alias", "mqtt_host": "canonical-broker"},
    )
    assert response.status_code == 302
    persisted = yaml.safe_load(config_path.read_text())
    assert persisted["mqtt"]["host"] == "canonical-broker"
    assert "attempted-alias" not in config_path.read_text()


def test_settings_links_to_existing_password_and_token_workflows(
    flask_client, tmp_path, monkeypatch
):
    configure_paths(tmp_path, monkeypatch)
    disable_runtime_reload(monkeypatch)

    response = flask_client.get("/admin/config")

    assert b'href="/admin/password"' in response.data
    assert b'href="/admin/api-token"' in response.data
    assert flask_client.get("/admin/password").status_code == 200
    assert flask_client.get("/admin/api-token").status_code == 200


def test_settings_form_keeps_existing_csrf_protection(
    flask_client, tmp_path, monkeypatch
):
    from werkzeug.security import generate_password_hash
    import webui

    config_path, _ = configure_paths(tmp_path, monkeypatch)
    original = config_path.read_bytes()
    monkeypatch.setattr(
        webui,
        "config",
        {
            "admin": {
                "auth_enabled": True,
                "session_secret": "test-secret",
                "password_hash": generate_password_hash("secret"),
            }
        },
    )
    webui.app.secret_key = "test-secret"
    with flask_client.session_transaction() as session:
        session["admin_authenticated"] = True
        session["csrf_token"] = "expected-token"

    response = flask_client.post(
        "/admin/config/save",
        data={"webui_port": "8877"},
    )

    assert response.status_code == 400
    assert config_path.read_bytes() == original


def test_settings_navigation_preserves_required_admin_destinations(
    flask_client, tmp_path, monkeypatch
):
    configure_paths(tmp_path, monkeypatch)
    disable_runtime_reload(monkeypatch)
    with flask_client.session_transaction() as session:
        session["admin_authenticated"] = True

    response = flask_client.get("/admin/config")
    html = response.get_data(as_text=True)
    admin_nav = re.search(
        r'<nav class="app-nav app-admin-nav" aria-label="Admin navigation">(.*?)</nav>',
        html,
        re.DOTALL,
    ).group(1)

    assert "Integrations" not in admin_nav
    assert "Cameras" not in admin_nav
    assert "Media &amp; Retention" not in admin_nav
    assert admin_nav.index("Settings") < admin_nav.index("Species Metadata")
    assert admin_nav.index("Species Metadata") < admin_nav.index("System Health")
    assert admin_nav.index("System Health") < admin_nav.index("Logs")


def test_species_metadata_navigation_has_active_state(
    flask_client, tmp_path, monkeypatch
):
    configure_paths(tmp_path, monkeypatch)
    disable_runtime_reload(monkeypatch)

    html = flask_client.get("/admin/species").get_data(as_text=True)
    assert re.search(
        r'class="app-nav-link active"\s+href="/admin/species"\s+'
        r'aria-current="page"[^>]*>Species Metadata</a>',
        html,
    )


def test_settings_section_navigation_has_eight_ordered_targets(
    flask_client, tmp_path, monkeypatch
):
    configure_paths(tmp_path, monkeypatch)
    disable_runtime_reload(monkeypatch)

    html = flask_client.get("/admin/config").get_data(as_text=True)
    section_nav = re.search(
        r'<nav class="settings-section-nav" aria-label="Settings sections">(.*?)</nav>',
        html,
        re.DOTALL,
    ).group(1)
    targets = re.findall(r'href="#([^"]+)"', section_nav)

    assert targets == [
        "general",
        "storage-retention",
        "mqtt",
        "frigate",
        "classification",
        "live-view",
        "bridge-perch",
        "admin-api",
    ]
    heading_ids = [
        "general-heading",
        "storage-retention-heading",
        "mqtt-heading",
        "frigate-heading",
        "classification-heading",
        "live-view-heading",
        "bridge-heading",
        "admin-api-heading",
    ]
    for section_id, heading_id in zip(targets, heading_ids, strict=True):
        assert html.count(f'id="{section_id}"') == 1
        assert f'aria-labelledby="{heading_id}"' in html
        assert html.count(f'<h2 id="{heading_id}">') == 1


def test_storage_retention_controls_are_moved_without_duplication(
    flask_client, tmp_path, monkeypatch
):
    configure_paths(tmp_path, monkeypatch)
    disable_runtime_reload(monkeypatch)

    html = flask_client.get("/admin/config").get_data(as_text=True)
    general = html.split('id="general"', 1)[1].split("</section>", 1)[0]
    storage = html.split('id="storage-retention"', 1)[1].split("</section>", 1)[0]

    assert "Storage paths" not in general
    assert "Media cleanup" not in general
    assert "Storage paths" in storage
    assert "Media cleanup" in storage
    for control_id in (
        "database_path",
        "snapshots_path",
        "clips_path",
        "snapshots_days",
        "clips_days",
    ):
        assert html.count(f'id="{control_id}"') == 1
    assert 'name="retention_enabled"' not in general
    assert storage.count('name="retention_enabled"') == 2


def test_storage_paths_are_readonly_and_survive_settings_save(
    flask_client, tmp_path, monkeypatch
):
    expected_paths = {
        "database_path": str(tmp_path / "storage" / "current.db"),
        "snapshots_path": str(tmp_path / "media" / "snapshots"),
        "clips_path": str(tmp_path / "media" / "clips"),
    }
    config = ready_config()
    config["storage"]["database_path"] = expected_paths["database_path"]
    config["media"]["snapshots_path"] = expected_paths["snapshots_path"]
    config["media"]["clips_path"] = expected_paths["clips_path"]
    config_path, _ = configure_paths(
        tmp_path,
        monkeypatch,
        config=config,
    )
    disable_runtime_reload(monkeypatch)

    html = flask_client.get("/admin/config").get_data(as_text=True)

    for field_name, path in expected_paths.items():
        assert re.search(
            rf'id="{field_name}" name="{field_name}" type="text" '
            rf'value="{re.escape(path)}" readonly required',
            html,
        )

    response = flask_client.post(
        "/admin/config/save",
        data={**expected_paths, "active_section": "storage-retention"},
    )

    assert response.status_code == 302
    persisted = yaml.safe_load(config_path.read_text())
    assert persisted["storage"]["database_path"] == expected_paths["database_path"]
    assert persisted["media"]["snapshots_path"] == expected_paths["snapshots_path"]
    assert persisted["media"]["clips_path"] == expected_paths["clips_path"]


def test_retention_controls_use_compact_explicit_labels_and_accurate_help(
    flask_client, tmp_path, monkeypatch
):
    config = ready_config()
    config["retention"].update({
        "enabled": True,
        "delete_media": True,
        "orphan_scan_enabled": False,
        "delete_orphaned_media": False,
    })
    configure_paths(tmp_path, monkeypatch, config=config)
    disable_runtime_reload(monkeypatch)

    html = flask_client.get("/admin/config").get_data(as_text=True)
    storage = html.split('id="storage-retention"', 1)[1].split("</section>", 1)[0]

    assert storage.count('class="settings-toggle-grid"') == 1
    assert storage.count('class="settings-toggle-item"') == 5
    for field_name, checked in (
        ("retention_enabled", True),
        ("delete_media", True),
        ("orphan_scan_enabled", False),
        ("delete_orphaned_media", False),
    ):
        checkbox = re.search(
            rf'<input class="settings-switch-input" id="{field_name}"[^>]+>',
            storage,
        ).group(0)
        assert f'name="{field_name}"' in checkbox
        assert (' checked' in checkbox) is checked
        assert f'<label class="settings-switch-label" for="{field_name}">' in storage
        assert storage.count(f'name="{field_name}"') == 2

    assert "<small" not in "".join(
        re.findall(
            r'<label class="settings-switch-label"[^>]*>(.*?)</label>',
            storage,
            re.DOTALL,
        )
    )
    for field_name, help_text in (
        (
            "snapshots_days",
            "Keep snapshots for this many days unless a species override applies. "
            "Zero is allowed.",
        ),
        (
            "clips_days",
            "Keep clips for this many days unless a species override applies. "
            "Zero is allowed.",
        ),
        ("system_events_days", "Number of days to retain system log events."),
        (
            "system_events_min_rows",
            "Minimum number of recent system events to keep regardless of age.",
        ),
        (
            "config_backups_max_files",
            "Number of previous configuration backups to retain.",
        ),
    ):
        assert f'aria-describedby="{field_name}_help"' in storage
        assert f'<small id="{field_name}_help">{help_text}</small>' in storage


def test_retention_policy_and_schedule_wording_are_distinct(
    flask_client, tmp_path, monkeypatch
):
    configure_paths(tmp_path, monkeypatch)
    disable_runtime_reload(monkeypatch)

    html = flask_client.get("/admin/config").get_data(as_text=True)
    storage = html.split('id="storage-retention"', 1)[1].split("</section>", 1)[0]

    assert "Apply retention policy" in storage
    assert "Default snapshot retention (days)" in storage
    assert "Default clip retention (days)" in storage
    assert "Run scheduled retention cleanup." not in storage
    assert "Run cleanup automatically" in storage
    assert "Schedule changes take effect after WAMF is restarted." in storage
    assert "does not run cleanup immediately" in storage
    assert "runs missed while WAMF is offline are not replayed" in storage


def test_schedule_controls_render_configured_values_and_defaults(
    flask_client, tmp_path, monkeypatch
):
    config = ready_config()
    config["retention"]["schedule"] = {
        "enabled": True,
        "time": "04:15",
        "timezone": "Europe/London",
    }
    configure_paths(tmp_path, monkeypatch, config=config)
    disable_runtime_reload(monkeypatch)

    html = flask_client.get("/admin/config").get_data(as_text=True)
    assert re.search(
        r'id="retention_schedule_enabled"[^>]* checked', html
    )
    assert 'name="retention_schedule_time" type="time" step="60" value="04:15"' in html
    assert 'name="retention_schedule_timezone" type="text"' in html
    assert 'value="Europe/London"' in html
    schedule_group = re.search(
        r'data-settings-dependency="retention_schedule_enabled"[^>]*>', html
    ).group(0)
    assert " disabled" not in schedule_group

    config["retention"].pop("schedule")
    configure_paths(tmp_path, monkeypatch, config=config)
    html = flask_client.get("/admin/config").get_data(as_text=True)
    assert not re.search(
        r'id="retention_schedule_enabled"[^>]* checked', html
    )
    assert 'name="retention_schedule_time" type="time" step="60" value="03:00"' in html
    assert 'value="UTC"' in html
    schedule_group = re.search(
        r'data-settings-dependency="retention_schedule_enabled"[^>]*>', html
    ).group(0)
    assert " disabled" in schedule_group


def test_retention_dependencies_render_without_cross_coupling(
    flask_client, tmp_path, monkeypatch
):
    config = ready_config()
    config["retention"].update(
        enabled=False,
        delete_media=True,
        orphan_scan_enabled=False,
        delete_orphaned_media=True,
        schedule={
            "enabled": True,
            "time": "03:00",
            "timezone": "UTC",
        },
    )
    configure_paths(tmp_path, monkeypatch, config=config)
    disable_runtime_reload(monkeypatch)

    html = flask_client.get("/admin/config").get_data(as_text=True)
    for controller, disabled in (
        ("retention_enabled", True),
        ("orphan_scan_enabled", True),
        ("retention_schedule_enabled", False),
    ):
        fieldset = re.search(
            rf'data-settings-dependency="{controller}"[^>]*>', html
        ).group(0)
        assert (" disabled" in fieldset) is disabled
    assert re.search(r'id="orphan_scan_enabled"[^>]*', html)
    assert re.search(r'id="retention_schedule_enabled"[^>]* checked', html)

    expired_group = html.split(
        'data-settings-dependency="retention_enabled"', 1
    )[1].split("</fieldset>", 1)[0]
    orphan_group = html.split(
        'data-settings-dependency="orphan_scan_enabled"', 1
    )[1].split("</fieldset>", 1)[0]
    assert 'type="hidden" name="delete_media" value="0"' in expired_group
    assert 'type="hidden" name="delete_orphaned_media" value="0"' in orphan_group


def test_disabled_retention_fields_are_preserved_on_partial_post(
    flask_client, tmp_path, monkeypatch
):
    config = ready_config()
    config["retention"].update(
        enabled=True,
        delete_media=True,
        orphan_scan_enabled=True,
        delete_orphaned_media=True,
        schedule={
            "enabled": True,
            "time": "04:15",
            "timezone": "Europe/London",
        },
    )
    config_path, _ = configure_paths(tmp_path, monkeypatch, config=config)
    disable_runtime_reload(monkeypatch)

    response = flask_client.post(
        "/admin/config/save",
        data={
            "retention_enabled": "0",
            "orphan_scan_enabled": "0",
            "retention_schedule_enabled": "0",
            "active_section": "storage-retention",
        },
    )

    assert response.status_code == 302
    retention = yaml.safe_load(config_path.read_text())["retention"]
    assert retention["enabled"] is False
    assert retention["delete_media"] is True
    assert retention["orphan_scan_enabled"] is False
    assert retention["delete_orphaned_media"] is True
    assert retention["schedule"] == {
        "enabled": False,
        "time": "04:15",
        "timezone": "Europe/London",
    }


def test_schedule_timezone_is_derived_rendered_saved_and_validated(
    flask_client, tmp_path, monkeypatch
):
    import routes.admin as admin_routes

    config_path, _ = configure_paths(tmp_path, monkeypatch)
    disable_runtime_reload(monkeypatch)
    monkeypatch.setattr(
        admin_routes,
        "available_timezones",
        lambda: {
            "Europe/London",
            "Europe/Paris",
            "America/New_York",
            "Australia/Sydney",
            "Etc/UTC",
            "posix/Europe/London",
            "right/Europe/London",
            "US/Eastern",
            "GMT",
        },
    )

    html = flask_client.get("/admin/config").get_data(as_text=True)
    datalist = html.split('<datalist id="retention-timezones">', 1)[1].split(
        "</datalist>", 1
    )[0]
    assert 'value="UTC"' in datalist
    assert 'value="Europe/London"' in datalist
    assert 'value="Europe/Paris"' in datalist
    assert 'value="America/New_York"' in datalist
    assert 'value="Australia/Sydney"' in datalist
    assert "Etc/UTC" not in datalist
    assert "posix/" not in datalist
    assert "right/" not in datalist
    assert "US/Eastern" not in datalist
    assert 'value="GMT"' not in datalist

    response = flask_client.post(
        "/admin/config/save",
        data={
            "retention_schedule_enabled": "1",
            "retention_schedule_time": "05:45",
            "retention_schedule_timezone": "Etc/UTC",
        },
    )
    assert response.status_code == 302
    assert yaml.safe_load(config_path.read_text())["retention"]["schedule"] == {
        "enabled": True,
        "time": "05:45",
        "timezone": "Etc/UTC",
    }

    original = config_path.read_bytes()
    response = flask_client.post(
        "/admin/config/save",
        data={"retention_schedule_timezone": "Not/A_Zone"},
    )
    assert response.status_code == 400
    assert b"must be a valid IANA timezone identifier" in response.data
    assert b"Not/A_Zone" in response.data
    assert config_path.read_bytes() == original


def test_timezone_suggestion_failure_keeps_get_and_valid_submission_available(
    flask_client, tmp_path, monkeypatch
):
    import routes.admin as admin_routes

    config = ready_config()
    config["retention"]["schedule"] = {
        "enabled": True,
        "time": "04:15",
        "timezone": "Europe/London",
    }
    config_path, _ = configure_paths(tmp_path, monkeypatch, config=config)
    disable_runtime_reload(monkeypatch)

    def unavailable_timezones():
        raise OSError("timezone catalog unavailable")

    monkeypatch.setattr(admin_routes, "available_timezones", unavailable_timezones)

    response = flask_client.get("/admin/config")
    html = response.get_data(as_text=True)
    datalist = html.split('<datalist id="retention-timezones">', 1)[1].split(
        "</datalist>", 1
    )[0]
    assert response.status_code == 200
    assert datalist == ""
    assert 'value="Europe/London"' in html

    response = flask_client.post(
        "/admin/config/save",
        data={
            "retention_schedule_enabled": "1",
            "retention_schedule_time": "05:45",
            "retention_schedule_timezone": "America/New_York",
        },
    )
    assert response.status_code == 302
    assert yaml.safe_load(config_path.read_text())["retention"]["schedule"] == {
        "enabled": True,
        "time": "05:45",
        "timezone": "America/New_York",
    }


def test_timezone_suggestion_failure_does_not_break_validation_rerender(
    flask_client, tmp_path, monkeypatch
):
    import routes.admin as admin_routes

    configure_paths(tmp_path, monkeypatch)
    disable_runtime_reload(monkeypatch)
    monkeypatch.setattr(
        admin_routes,
        "available_timezones",
        lambda: (_ for _ in ()).throw(OSError("timezone catalog unavailable")),
    )

    response = flask_client.post(
        "/admin/config/save",
        data={
            "webui_port": "not-a-number",
            "retention_schedule_timezone": "Europe/London",
        },
    )

    assert response.status_code == 400
    assert b"Enter a whole number." in response.data
    assert b'Europe/London' in response.data


def test_species_editor_uses_cached_metadata_and_renders_uncached_overrides(
    flask_client, tmp_path, monkeypatch
):
    config = ready_config()
    config["retention"]["species_overrides"] = {
        "Turdus migratorius": {"snapshots_days": 45},
        "Legacy species": {"clips_days": 7},
    }
    configure_paths(tmp_path, monkeypatch, config=config)
    disable_runtime_reload(monkeypatch)

    html = flask_client.get("/admin/config").get_data(as_text=True)
    editor = html.split('id="retention-species-editor"', 1)[1].split(
        '</div>\n      <div class="settings-card">', 1
    )[0]

    assert "American Robin — Turdus migratorius" in editor
    assert "Legacy species" in editor
    assert "Metadata not currently cached" in editor
    assert '<option value="Cyanocitta cristata">Blue Jay — Cyanocitta cristata</option>' in editor
    assert '<option value="Turdus migratorius">' not in editor
    assert 'aria-label="Remove retention override for Turdus migratorius"' in editor
    assert 'name="retention_species_overrides"' not in html
    assert 'id="advanced"' not in html
    assert 'href="#advanced"' not in html


def test_species_override_inherit_placeholders_use_configured_global_defaults(
    flask_client, tmp_path, monkeypatch
):
    config = ready_config()
    config["retention"]["snapshots_days"] = 91
    config["retention"]["clips_days"] = 37
    config["retention"]["species_overrides"] = {
        "Turdus migratorius": {}
    }
    configure_paths(tmp_path, monkeypatch, config=config)
    disable_runtime_reload(monkeypatch)

    html = flask_client.get("/admin/config").get_data(as_text=True)
    editor = html.split('id="retention-species-editor"', 1)[1].split(
        '</div>\n      <div class="settings-card">', 1
    )[0]

    assert "Set optional snapshot and clip retention periods for known species." in editor
    assert 'data-snapshot-default="91"' in editor
    assert 'data-clip-default="37"' in editor
    assert (
        'name="retention_species_snapshots_days_0" type="number" min="0" '
        'step="1" value="" placeholder="Inherit (91 days)"'
    ) in editor
    assert (
        'name="retention_species_clips_days_0" type="number" min="0" '
        'step="1" value="" placeholder="Inherit (37 days)"'
    ) in editor
    assert "input.placeholder = 'Inherit (' + defaultDays + ' days)';" in html


def test_species_inherit_placeholders_remain_configured_values_on_rerender(
    flask_client, tmp_path, monkeypatch
):
    config = ready_config()
    config["retention"]["snapshots_days"] = 91
    config["retention"]["clips_days"] = 37
    config["retention"]["species_overrides"] = {
        "Turdus migratorius": {}
    }
    configure_paths(tmp_path, monkeypatch, config=config)
    disable_runtime_reload(monkeypatch)

    response = flask_client.post(
        "/admin/config/save",
        data={
            "webui_port": "not-a-number",
            "snapshots_days": "12",
            "clips_days": "13",
            "retention_species_overrides_present": "1",
            "retention_species_row": "0",
            "retention_species_scientific_name_0": "Turdus migratorius",
            "retention_species_snapshots_days_0": "",
            "retention_species_clips_days_0": "",
        },
    )
    html = response.get_data(as_text=True)

    assert response.status_code == 400
    assert 'placeholder="Inherit (91 days)"' in html
    assert 'placeholder="Inherit (37 days)"' in html
    assert 'id="snapshots_days" name="snapshots_days" type="number" min="0" step="1" value="12"' in html
    assert 'id="clips_days" name="clips_days" type="number" min="0" step="1" value="13"' in html


def test_species_metadata_failure_preserves_existing_overrides_on_get_and_post(
    flask_client, tmp_path, monkeypatch
):
    import webui

    config = ready_config()
    config["retention"]["species_overrides"] = {
        "Uncached species": {"snapshots_days": 17}
    }
    config_path, _ = configure_paths(tmp_path, monkeypatch, config=config)
    disable_runtime_reload(monkeypatch)

    def unavailable_metadata():
        raise RuntimeError("metadata unavailable")

    monkeypatch.setattr(webui, "get_all_species_info", unavailable_metadata)

    response = flask_client.get("/admin/config")
    assert response.status_code == 200
    assert b"Uncached species" in response.data
    assert b"Metadata not currently cached" in response.data
    assert b'id="retention_species_add"' in response.data
    assert b'id="retention_species_add" aria-describedby=' in response.data
    assert b'id="retention_species_add" aria-describedby="retention_species_add_help" disabled' in response.data

    response = flask_client.post(
        "/admin/config/save", data={"webui_port": "8877"}
    )
    assert response.status_code == 302
    persisted = yaml.safe_load(config_path.read_text())
    assert persisted["webui"]["port"] == 8877
    assert persisted["retention"]["species_overrides"] == {
        "Uncached species": {"snapshots_days": 17}
    }

    response = flask_client.post(
        "/admin/config/save",
        data={
            "retention_species_overrides_present": "1",
            "retention_species_row": "0",
            "retention_species_scientific_name_0": "Uncached species",
            "retention_species_snapshots_days_0": "18",
            "retention_species_clips_days_0": "",
        },
    )
    assert response.status_code == 302
    assert yaml.safe_load(config_path.read_text())["retention"][
        "species_overrides"
    ] == {"Uncached species": {"snapshots_days": 18}}


def test_species_metadata_catalog_is_normalized_deterministic_and_unique(
    flask_client, tmp_path, monkeypatch
):
    import webui
    from app.config_forms import normalized_species_catalog

    config_path, _ = configure_paths(tmp_path, monkeypatch)
    disable_runtime_reload(monkeypatch)
    records = [
        {
            "scientific_name": " turdus migratorius ",
            "common_name": "Whitespace Robin",
        },
        {
            "scientific_name": "turdus MIGRATORIUS",
            "common_name": "Lowercase Robin",
        },
        {
            "scientific_name": "Turdus migratorius",
            "common_name": "Canonical Robin",
        },
    ]
    assert normalized_species_catalog(records) == normalized_species_catalog(
        reversed(records)
    )
    monkeypatch.setattr(webui, "get_all_species_info", lambda: records)

    html = flask_client.get("/admin/config").get_data(as_text=True)
    selector = html.split('<select id="retention_species_add"', 1)[1].split(
        "</select>", 1
    )[0]
    assert selector.count("<option value=") == 2
    assert 'value="Turdus migratorius"' in selector
    assert "Canonical Robin" in selector
    assert "Whitespace Robin" not in selector
    assert "Lowercase Robin" not in selector

    response = flask_client.post(
        "/admin/config/save",
        data={
            "retention_species_overrides_present": "1",
            "retention_species_row": "0",
            "retention_species_scientific_name_0": "Turdus migratorius",
            "retention_species_snapshots_days_0": "12",
            "retention_species_clips_days_0": "",
        },
    )
    assert response.status_code == 302
    assert yaml.safe_load(config_path.read_text())["retention"][
        "species_overrides"
    ] == {"Turdus migratorius": {"snapshots_days": 12}}

    selector = flask_client.get("/admin/config").get_data(as_text=True).split(
        '<select id="retention_species_add"', 1
    )[1].split("</select>", 1)[0]
    assert "migratorius" not in selector.casefold()


def test_structured_species_override_persists_scientific_key_inheritance_and_zero(
    flask_client, tmp_path, monkeypatch
):
    config_path, _ = configure_paths(tmp_path, monkeypatch)
    disable_runtime_reload(monkeypatch)

    response = flask_client.post(
        "/admin/config/save",
        data={
            "retention_species_overrides_present": "1",
            "retention_species_row": "0",
            "retention_species_scientific_name_0": "Cyanocitta cristata",
            "retention_species_snapshots_days_0": "",
            "retention_species_clips_days_0": "0",
        },
    )

    assert response.status_code == 302
    overrides = yaml.safe_load(config_path.read_text())["retention"][
        "species_overrides"
    ]
    assert overrides == {"Cyanocitta cristata": {"clips_days": 0}}
    assert "Blue Jay" not in overrides

    response = flask_client.post(
        "/admin/config/save",
        data={
            "retention_species_overrides_present": "1",
            "retention_species_row": "0",
            "retention_species_scientific_name_0": "Cyanocitta cristata",
            "retention_species_snapshots_days_0": "0",
            "retention_species_clips_days_0": "12",
        },
    )
    assert response.status_code == 302
    assert yaml.safe_load(config_path.read_text())["retention"][
        "species_overrides"
    ] == {
        "Cyanocitta cristata": {"snapshots_days": 0, "clips_days": 12}
    }


def test_species_override_remove_and_uncached_partial_post_behavior(
    flask_client, tmp_path, monkeypatch
):
    config = ready_config()
    config["retention"]["species_overrides"] = {
        "Uncached species": {"snapshots_days": 17}
    }
    config_path, _ = configure_paths(tmp_path, monkeypatch, config=config)
    disable_runtime_reload(monkeypatch)

    response = flask_client.post(
        "/admin/config/save", data={"webui_port": "8877"}
    )
    assert response.status_code == 302
    persisted = yaml.safe_load(config_path.read_text())
    assert persisted["retention"]["species_overrides"] == {
        "Uncached species": {"snapshots_days": 17}
    }

    response = flask_client.post(
        "/admin/config/save",
        data={
            "retention_species_overrides_present": "1",
            "retention_species_row": "0",
            "retention_species_scientific_name_0": "Uncached species",
            "retention_species_snapshots_days_0": "18",
            "retention_species_clips_days_0": "",
        },
    )
    assert response.status_code == 302
    assert yaml.safe_load(config_path.read_text())["retention"][
        "species_overrides"
    ] == {"Uncached species": {"snapshots_days": 18}}

    response = flask_client.post(
        "/admin/config/save",
        data={"retention_species_overrides_present": "1"},
    )
    assert response.status_code == 302
    assert yaml.safe_load(config_path.read_text())["retention"][
        "species_overrides"
    ] == {}


def test_existing_empty_species_override_remains_until_row_is_removed(
    flask_client, tmp_path, monkeypatch
):
    config = ready_config()
    config["retention"]["species_overrides"] = {"Uncached species": {}}
    config_path, _ = configure_paths(tmp_path, monkeypatch, config=config)
    disable_runtime_reload(monkeypatch)

    response = flask_client.post(
        "/admin/config/save",
        data={
            "retention_species_overrides_present": "1",
            "retention_species_row": "0",
            "retention_species_scientific_name_0": "Uncached species",
            "retention_species_snapshots_days_0": "",
            "retention_species_clips_days_0": "",
        },
    )

    assert response.status_code == 302
    assert yaml.safe_load(config_path.read_text())["retention"][
        "species_overrides"
    ] == {"Uncached species": {}}


def test_new_override_must_come_from_cached_species_metadata(
    flask_client, tmp_path, monkeypatch
):
    config_path, _ = configure_paths(tmp_path, monkeypatch)
    disable_runtime_reload(monkeypatch)
    original = config_path.read_bytes()

    response = flask_client.post(
        "/admin/config/save",
        data={
            "retention_species_overrides_present": "1",
            "retention_species_row": "0",
            "retention_species_scientific_name_0": "Invented species",
            "retention_species_snapshots_days_0": "10",
            "retention_species_clips_days_0": "",
        },
    )

    assert response.status_code == 400
    assert b"Choose a species from the cached metadata list." in response.data
    assert config_path.read_bytes() == original


def test_legacy_cased_override_is_preserved_and_duplicate_rows_are_rejected(
    flask_client, tmp_path, monkeypatch
):
    config = ready_config()
    config["retention"]["species_overrides"] = {
        "tUrDuS MiGrAtOrIuS": {"snapshots_days": 21}
    }
    config_path, _ = configure_paths(tmp_path, monkeypatch, config=config)
    disable_runtime_reload(monkeypatch)

    html = flask_client.get("/admin/config").get_data(as_text=True)
    assert "American Robin — tUrDuS MiGrAtOrIuS" in html
    response = flask_client.post(
        "/admin/config/save",
        data={
            "retention_species_overrides_present": "1",
            "retention_species_row": ["0", "1"],
            "retention_species_scientific_name_0": "tUrDuS MiGrAtOrIuS",
            "retention_species_snapshots_days_0": "21",
            "retention_species_clips_days_0": "",
            "retention_species_scientific_name_1": "Turdus migratorius",
            "retention_species_snapshots_days_1": "30",
            "retention_species_clips_days_1": "",
        },
    )
    assert response.status_code == 400
    assert b"already has a retention override" in response.data
    assert yaml.safe_load(config_path.read_text())["retention"][
        "species_overrides"
    ] == {"tUrDuS MiGrAtOrIuS": {"snapshots_days": 21}}


def test_species_row_ids_are_bounded_unique_and_rerendered_safely(
    flask_client, tmp_path, monkeypatch
):
    config_path, _ = configure_paths(tmp_path, monkeypatch)
    disable_runtime_reload(monkeypatch)
    original = config_path.read_bytes()

    for row_ids in (["bad"], ["9" * 100], ["0", "0"]):
        response = flask_client.post(
            "/admin/config/save",
            data={
                "retention_species_overrides_present": "1",
                "retention_species_row": row_ids,
            },
        )
        assert response.status_code == 400
        assert b"Species override row is invalid." in response.data
        assert b'data-next-row-index="0"' in response.data
        assert config_path.read_bytes() == original

    response = flask_client.post(
        "/admin/config/save",
        data={
            "retention_species_overrides_present": "1",
            "retention_species_row": "9999",
            "retention_species_scientific_name_9999": "Turdus migratorius",
            "retention_species_snapshots_days_9999": "-1",
            "retention_species_clips_days_9999": "",
        },
    )
    assert response.status_code == 400
    assert b"Enter zero or a positive whole number" in response.data
    assert b'name="retention_species_row" value="0"' in response.data
    assert b'data-next-row-index="1"' in response.data
    assert config_path.read_bytes() == original


@pytest.mark.parametrize("invalid_field", ["snapshots_days", "clips_days"])
def test_species_field_error_follows_row_after_middle_removal(
    flask_client, tmp_path, monkeypatch, invalid_field
):
    config = ready_config()
    config["retention"]["species_overrides"] = {
        "Turdus migratorius": {"snapshots_days": 10},
        "Middle uncached species": {"clips_days": 20},
        "Last uncached species": {"snapshots_days": 30, "clips_days": 40},
    }
    config_path, secrets_path = configure_paths(tmp_path, monkeypatch, config=config)
    disable_runtime_reload(monkeypatch)
    original_config = config_path.read_bytes()
    original_secrets = secrets_path.read_bytes()
    form = {
        "retention_species_overrides_present": "1",
        "retention_species_row": ["0", "2"],
        "retention_species_scientific_name_0": "Turdus migratorius",
        "retention_species_snapshots_days_0": "0",
        "retention_species_clips_days_0": "",
        "retention_species_scientific_name_2": "Last uncached species",
        "retention_species_snapshots_days_2": "31",
        "retention_species_clips_days_2": "41",
    }
    form[f"retention_species_{invalid_field}_2"] = "-1"

    response = flask_client.post("/admin/config/save", data=form)

    assert response.status_code == 400
    assert config_path.read_bytes() == original_config
    assert secrets_path.read_bytes() == original_secrets
    html = response.get_data(as_text=True)
    rows = html.split('<div class="settings-species-row"')[1:]
    assert len(rows) == 2
    first_row, last_row = rows
    last_row = last_row.split('</div>\n      <div class="settings-card">', 1)[0]
    assert 'data-scientific-name="Turdus migratorius"' in first_row
    assert 'data-scientific-name="Last uncached species"' in last_row
    assert "Middle uncached species" not in html
    assert 'name="retention_species_row" value="1"' in last_row
    field_markup = re.search(
        rf'<div class="settings-field"><label for="retention_species_{invalid_field}_1">'
        rf'.*?</div>',
        last_row,
        re.DOTALL,
    ).group(0)
    error = (
        '<span class="settings-field-error">Enter zero or a positive whole '
        'number, or leave blank.</span>'
    )
    assert f'name="retention_species_{invalid_field}_1"' in field_markup
    assert 'value="-1"' in field_markup
    assert error in field_markup
    assert last_row.count(error) == 1
    assert error not in first_row
    other_field = "clips_days" if invalid_field == "snapshots_days" else "snapshots_days"
    other_value = "41" if other_field == "clips_days" else "31"
    assert re.search(
        rf'name="retention_species_{other_field}_1"[^>]*value="{other_value}"',
        last_row,
    )


def test_multiple_species_save_and_removing_only_middle_species(
    flask_client, tmp_path, monkeypatch
):
    config = ready_config()
    config["retention"]["species_overrides"] = {
        "Turdus migratorius": {"snapshots_days": 10},
        "Middle uncached species": {"clips_days": 20},
        "Cyanocitta cristata": {"snapshots_days": 30, "clips_days": 40},
    }
    config_path, _ = configure_paths(tmp_path, monkeypatch, config=config)
    disable_runtime_reload(monkeypatch)

    response = flask_client.post(
        "/admin/config/save",
        data={
            "retention_species_overrides_present": "1",
            "retention_species_row": ["0", "2"],
            "retention_species_scientific_name_0": "Turdus migratorius",
            "retention_species_snapshots_days_0": "11",
            "retention_species_clips_days_0": "",
            "retention_species_scientific_name_2": "Cyanocitta cristata",
            "retention_species_snapshots_days_2": "31",
            "retention_species_clips_days_2": "41",
        },
    )

    assert response.status_code == 302
    assert yaml.safe_load(config_path.read_text())["retention"][
        "species_overrides"
    ] == {
        "Turdus migratorius": {"snapshots_days": 11},
        "Cyanocitta cristata": {"snapshots_days": 31, "clips_days": 41},
    }


def test_unicode_and_punctuation_metadata_round_trip(
    flask_client, tmp_path, monkeypatch
):
    import webui

    config_path, _ = configure_paths(tmp_path, monkeypatch)
    disable_runtime_reload(monkeypatch)
    monkeypatch.setattr(
        webui,
        "get_all_species_info",
        lambda: [
            {
                "scientific_name": "Sturnus vulgaris × test",
                "common_name": "Étourneau d’Europe — test",
            }
        ],
    )

    html = flask_client.get("/admin/config").get_data(as_text=True)
    assert "Étourneau d’Europe — test" in html
    response = flask_client.post(
        "/admin/config/save",
        data={
            "retention_species_overrides_present": "1",
            "retention_species_row": "0",
            "retention_species_scientific_name_0": "Sturnus vulgaris × test",
            "retention_species_snapshots_days_0": "5",
            "retention_species_clips_days_0": "6",
        },
    )
    assert response.status_code == 302
    assert yaml.safe_load(config_path.read_text())["retention"][
        "species_overrides"
    ] == {
        "Sturnus vulgaris × test": {"snapshots_days": 5, "clips_days": 6}
    }


def test_invalid_existing_species_day_is_not_laundered_through_blank_input(
    flask_client, tmp_path, monkeypatch
):
    for key, invalid_value in (("snapshots_days", None), ("clips_days", True)):
        config = ready_config()
        config["retention"]["species_overrides"] = {
            "Turdus migratorius": {key: invalid_value}
        }
        config_path, _ = configure_paths(tmp_path, monkeypatch, config=config)
        disable_runtime_reload(monkeypatch)
        original = config_path.read_bytes()

        get_response = flask_client.get("/admin/config")
        assert get_response.status_code == 200
        post_response = flask_client.post(
            "/admin/config/save",
            data={
                "retention_species_overrides_present": "1",
                "retention_species_row": "0",
                "retention_species_scientific_name_0": "Turdus migratorius",
                "retention_species_snapshots_days_0": "",
                "retention_species_clips_days_0": "",
            },
        )
        assert post_response.status_code == 400
        assert b"must be a non-negative integer" in post_response.data
        assert config_path.read_bytes() == original


def test_invalid_existing_global_day_requires_explicit_valid_correction(
    flask_client, tmp_path, monkeypatch
):
    for key, invalid_value in (("snapshots_days", None), ("clips_days", True)):
        config = ready_config()
        config["retention"][key] = invalid_value
        config_path, _ = configure_paths(tmp_path, monkeypatch, config=config)
        disable_runtime_reload(monkeypatch)
        original = config_path.read_bytes()

        assert flask_client.get("/admin/config").status_code == 200
        response = flask_client.post("/admin/config/save", data={key: ""})
        assert response.status_code == 400
        assert b"must be a non-negative integer" in response.data
        assert config_path.read_bytes() == original

        response = flask_client.post(
            "/admin/config/save", data={key: "15"}
        )
        assert response.status_code == 302
        assert yaml.safe_load(config_path.read_text())["retention"][key] == 15


def test_unknown_validation_field_has_no_obsolete_advanced_section_link():
    from routes.admin import _settings_section_for_field

    assert _settings_section_for_field("unknown.validation.field") is None


def test_species_metadata_page_remains_retention_free(flask_client):
    html = flask_client.get("/admin/species").get_data(as_text=True)
    assert "retention_species" not in html
    assert "Species overrides" not in html


def test_admin_status_moves_to_authenticated_sidebar_and_uses_real_states(
    flask_client, tmp_path, monkeypatch
):
    import webui
    from version import VERSION

    config = ready_config()
    config["bridge"]["enabled"] = True
    configure_paths(tmp_path, monkeypatch, config=config)
    monkeypatch.setattr(webui, "config", config)
    monkeypatch.setattr(webui, "get_system_health", lambda: {
        "overall_state": "degraded",
        "setup_required": False,
        "frigate_online": True,
        "mqtt_online": False,
        "database_healthy": True,
        "archive_writable": True,
        "disk_used_percent": 20,
    })
    monkeypatch.setattr(webui, "get_retention_status", lambda: None)
    with flask_client.session_transaction() as session:
        session["admin_authenticated"] = True

    html = flask_client.get("/admin/config").get_data(as_text=True)

    assert 'class="admin-system-status"' in html
    assert "Attention needed" in html
    sidebar_status = re.search(
        r'<section class="admin-system-status".*?</section>', html, re.S
    ).group(0)
    assert 'href="/admin"' in sidebar_status
    assert 'aria-labelledby="admin-system-status-heading admin-system-status-text"' in sidebar_status
    assert "MQTT" not in sidebar_status
    assert "Frigate" not in sidebar_status
    assert "Bridge / Perch" not in sidebar_status
    assert "admin-system-checks" not in sidebar_status
    assert f"WAMF {VERSION}" in html
    assert "admin-status-bar" not in html


def test_sidebar_status_is_absent_from_public_and_login_pages(
    flask_client, monkeypatch
):
    import webui

    with flask_client.session_transaction() as session:
        session["admin_authenticated"] = True
    assert b"admin-system-status" not in flask_client.get("/").data

    with flask_client.session_transaction() as session:
        session.clear()
    monkeypatch.setattr(webui, "config", {"admin": {"auth_enabled": True}})
    assert b"admin-system-status" not in flask_client.get("/login").data


def test_settings_has_only_sticky_save_and_restart_actions(
    flask_client, tmp_path, monkeypatch
):
    configure_paths(tmp_path, monkeypatch)
    disable_runtime_reload(monkeypatch)

    html = flask_client.get("/admin/config").get_data(as_text=True)
    page_header = re.search(
        r'<header class="app-page-header">(.*?)</header>', html, re.DOTALL
    ).group(1)
    action_bar = re.search(
        r'<div class="settings-action-bar" role="group" aria-label="Settings actions">(.*?)</div>\s*</div>\s*</div>',
        html,
        re.DOTALL,
    ).group(1)

    assert html.count(">Save settings</button>") == 1
    assert html.count(">Restart WAMF</button>") == 1
    assert "Save settings" not in page_header
    assert "Restart WAMF" not in page_header
    assert "Configure WAMF services, integrations and application behaviour." in page_header
    assert 'class="app-page-description"' in page_header
    assert "Restart WAMF" in action_bar
    assert "Save settings" in action_bar
    assert 'type="submit" form="settings-form"' in action_bar
    assert 'id="restart-btn" type="button"' in action_bar
    assert html.index('</form>') < html.index('class="settings-action-bar"')


@pytest.mark.parametrize(
    ("mqtt_authentication_enabled", "expected_message"),
    [
        (False, "Settings saved. Restart WAMF to apply deployment changes."),
        (
            True,
            "Settings saved, but setup is incomplete. Complete the required "
            "settings before restarting WAMF.",
        ),
    ],
)
def test_successful_settings_save_renders_accessible_dismissible_toast(
    flask_client, tmp_path, monkeypatch, mqtt_authentication_enabled, expected_message
):
    config = ready_config()
    config["mqtt"]["authentication"]["enabled"] = mqtt_authentication_enabled
    configure_paths(tmp_path, monkeypatch, config=config)
    disable_runtime_reload(monkeypatch)
    with flask_client.session_transaction() as session:
        session.clear()

    response = flask_client.post(
        "/admin/config/save",
        data={"webui_port": "8877", "active_section": "general"},
        follow_redirects=True,
    )
    html = response.get_data(as_text=True)
    stylesheet = (
        Path(__file__).resolve().parent.parent / "static/css/styles.css"
    ).read_text()

    assert response.status_code == 200
    assert 'class="settings-toast" id="settings-save-toast"' in html
    assert (
        'class="settings-toast-announcement" role="status" aria-live="polite" '
        'aria-atomic="true"></span>'
    ) in html
    toast_markup = re.search(
        r'<div class="settings-toast"[^>]*>(.*?)</div>', html, re.DOTALL
    ).group(1)
    assert expected_message in toast_markup
    assert (
        '<button class="settings-toast-dismiss" type="button" '
        'aria-label="Dismiss save confirmation">'
    ) in html
    assert "document.getElementById('settings-errors') ||" not in html
    assert "document.getElementById('settings-status')" not in html
    assert "document.getElementById('settings-save-toast').focus" not in html
    toast_script = html.split("const toast =", 1)[1].split("</script>", 1)[0]
    assert ".focus(" not in toast_script
    assert "scrollIntoView" not in toast_script
    notice_styles = stylesheet.split(".settings-notice {", 1)[1].split("}", 1)[0]
    assert "scroll-margin-top" not in notice_styles
    assert re.search(
        r"\.settings-toast\s*\{[^}]*position:\s*fixed;[^}]*",
        stylesheet,
        re.DOTALL,
    )
    assert "const duration = 7000;" in html
    assert "toast.addEventListener('mouseenter', pauseDismissal);" in html
    assert "toast.addEventListener('focusin', pauseDismissal);" in html
    assert "dismissButton.addEventListener('click'" in html
    assert "toast.inert = true;" in html
    assert "prefers-reduced-motion: reduce" in html


def test_successful_save_preserves_section_fragment_without_notification_focus(
    flask_client, tmp_path, monkeypatch
):
    configure_paths(tmp_path, monkeypatch)
    disable_runtime_reload(monkeypatch)

    response = flask_client.post(
        "/admin/config/save",
        data={"webui_port": "8877", "active_section": "classification"},
    )

    assert response.status_code == 302
    assert response.headers["Location"].endswith("#classification")


def test_canonical_perch_and_health_values_use_bridge_perch_controls(
    flask_client, tmp_path, monkeypatch
):
    config = ready_config()
    config.pop("bridge")
    config["perch"] = {
        "enabled": True,
        "events_url": "https://perch.example/events",
        "timeout_seconds": 2.5,
    }
    config["health"] = {"check_interval_seconds": 45}
    config_path, _ = configure_paths(tmp_path, monkeypatch, config=config)
    disable_runtime_reload(monkeypatch)

    response = flask_client.get("/admin/config")
    assert b'value="https://perch.example/events"' in response.data
    assert b'value="2.5"' in response.data
    assert b'value="45"' in response.data

    response = flask_client.post(
        "/admin/config/save",
        data={
            "bridge_events_url": "https://new-perch.example/events",
            "bridge_health_check_interval_seconds": "30",
        },
    )
    assert response.status_code == 302
    persisted = yaml.safe_load(config_path.read_text())
    assert persisted["perch"]["events_url"] == "https://new-perch.example/events"
    assert persisted["health"]["check_interval_seconds"] == 30.0
    assert "bridge" not in persisted
