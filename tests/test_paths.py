from pathlib import Path

import yaml

import wamf_paths


def _write_config(path, database_path, snapshots_path, clips_path):
    path.write_text(
        yaml.safe_dump({
            "storage": {"database_path": str(database_path)},
            "media": {
                "snapshots_path": str(snapshots_path),
                "clips_path": str(clips_path),
            },
        }),
        encoding="utf-8",
    )


def test_configured_paths_are_loaded_and_created(monkeypatch, tmp_path):
    config_path = tmp_path / "config.yml"
    database_path = tmp_path / "database" / "wamf.db"
    snapshots_path = tmp_path / "archive" / "snapshots"
    clips_path = tmp_path / "archive" / "clips"
    _write_config(config_path, database_path, snapshots_path, clips_path)
    monkeypatch.setenv("WHOSATMYFEEDER_CONFIG", str(config_path))

    assert wamf_paths.get_database_path() == database_path
    assert wamf_paths.get_snapshots_path() == snapshots_path
    assert wamf_paths.get_clips_path() == clips_path

    wamf_paths.ensure_storage_paths()

    assert database_path.parent.is_dir()
    assert snapshots_path.is_dir()
    assert clips_path.is_dir()


def test_missing_config_entries_use_repo_local_defaults(monkeypatch, tmp_path):
    config_path = tmp_path / "config.yml"
    config_path.write_text("webui: {}\n", encoding="utf-8")
    monkeypatch.setenv("WHOSATMYFEEDER_CONFIG", str(config_path))

    assert wamf_paths.get_database_path() == wamf_paths.DEFAULT_DATABASE_PATH
    assert wamf_paths.get_snapshots_path() == wamf_paths.DEFAULT_SNAPSHOTS_PATH
    assert wamf_paths.get_clips_path() == wamf_paths.DEFAULT_CLIPS_PATH


def test_legacy_media_values_resolve_under_configured_roots(monkeypatch, tmp_path):
    config_path = tmp_path / "config.yml"
    snapshots_path = tmp_path / "snapshots"
    clips_path = tmp_path / "clips"
    _write_config(config_path, tmp_path / "wamf.db", snapshots_path, clips_path)
    monkeypatch.setenv("WHOSATMYFEEDER_CONFIG", str(config_path))

    assert wamf_paths.resolve_media_path(
        "media/wamf/snapshots/event.jpg", "snapshots"
    ) == snapshots_path / "event.jpg"
    assert wamf_paths.resolve_media_path(
        "event.mp4", "clips"
    ) == clips_path / "event.mp4"

    absolute = tmp_path / "legacy" / "event.jpg"
    assert wamf_paths.resolve_media_path(absolute, "snapshots") == absolute
