"""Focused coverage for the forms-based Administration Settings UI."""

import re

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
        b"Advanced",
    ):
        assert heading in response.data
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

    assert len(labels) == 11
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
    assert submitted_secret.encode() not in response.data
    assert config_path.read_bytes() == original_config
    assert secrets_path.read_bytes() == original_secrets


def test_advanced_species_override_error_preserves_safe_text_only(
    flask_client, tmp_path, monkeypatch
):
    config_path, _ = configure_paths(tmp_path, monkeypatch)
    disable_runtime_reload(monkeypatch)
    original = config_path.read_bytes()

    response = flask_client.post(
        "/admin/config/save",
        data={"retention_species_overrides": "[not: a: mapping]"},
    )

    assert response.status_code == 400
    assert b"Species overrides must be valid YAML." in response.data
    assert b"[not: a: mapping]" in response.data
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


def test_settings_section_navigation_has_nine_ordered_targets(
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
        "advanced",
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
        "advanced-heading",
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
    assert "Media &amp; retention" not in general
    assert "Storage paths" in storage
    assert "Media &amp; retention" in storage
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
    assert storage.count('class="settings-toggle-item"') == 4
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
        ("snapshots_days", "Number of days to retain archived snapshots."),
        ("clips_days", "Number of days to retain archived clips."),
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
    assert re.search(r"MQTT</dt>\s*<dd[^>]*>Offline</dd>", html)
    assert re.search(r"Frigate</dt>\s*<dd[^>]*>Connected</dd>", html)
    assert re.search(r"Bridge / Perch</dt><dd>Enabled</dd>", html)
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
