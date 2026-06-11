
from datetime import datetime, timedelta
from pathlib import Path
from system_events import log_system_event
import sqlite3
import yaml

DB_PATH = "data/speciesid.db"


def load_config():

    with open("config/config.yml", "r") as f:
        return yaml.safe_load(f)


def get_retention_days(config, species_name, media_type):

    overrides = config["retention"].get("species_overrides", {})

    if species_name in overrides:

        key = f"{media_type}_days"

        if key in overrides[species_name]:
            return overrides[species_name][key]

    return config["retention"][f"{media_type}_days"]


def dry_run_retention():

    config = load_config()

    if not config["retention"].get("enabled", True):

        log_system_event("INFO", "RETENTION", "Retention disabled")

        return

    log_system_event("INFO", "RETENTION", "Retention scan started")

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    cursor = conn.cursor()

    cursor.execute("""
        SELECT
            id,
            detection_time,
            display_name,
            wamf_snapshot_path,
            wamf_clip_path
        FROM detections
    """)

    rows = cursor.fetchall()

    now = datetime.now()

    delete_media = config["retention"].get("delete_media", False)

    for row in rows:

        species = row["display_name"]

        detection_time = datetime.fromisoformat(row["detection_time"])

        age_days = (now - detection_time).days

        snapshot_retention = get_retention_days(config, species, "snapshots")

        clip_retention = get_retention_days(config, species, "clips")

        if row["wamf_snapshot_path"] and age_days > snapshot_retention:

            path = Path(row["wamf_snapshot_path"])

            if delete_media:

                if path.exists():

                    path.unlink()

                    log_system_event("INFO", "RETENTION", f"Deleted snapshot: {path}")

                cursor.execute(
                    """
                    UPDATE detections
                    SET wamf_snapshot_path = NULL
                    WHERE id = ?
                    """,
                    (row["id"],),
                )

            else:

                print(f"[DRY RUN] Would delete snapshot: {path}")

                log_system_event("INFO", "RETENTION", f"Deleted snapshot: {path}")

        if row["wamf_clip_path"] and age_days > clip_retention:

            path = Path(row["wamf_clip_path"])

            if delete_media:

                if path.exists():

                    path.unlink()

                    log_system_event("INFO", "RETENTION", f"Deleted clip: {path}")

                cursor.execute(
                    """
                   UPDATE detections
                   SET wamf_clip_path = NULL
                   WHERE id = ?
                   """,
                    (row["id"],),
                )

            else:

                print(f"[DRY RUN] Would delete clip: {path}")

    print("\nRetention scan complete")
    print(f"Scanned rows: {len(rows)}")

    conn.commit()
    conn.close()


def scan_for_orphans():

    config = load_config()

    delete_orphaned_media = config["retention"].get(
        "delete_orphaned_media",
        False,
    )

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    cursor = conn.cursor()

    cursor.execute("""
        SELECT
            wamf_snapshot_path,
            wamf_clip_path
        FROM detections
    """)

    rows = cursor.fetchall()

    referenced_files = set()

    for row in rows:

        if row["wamf_snapshot_path"]:
            referenced_files.add(row["wamf_snapshot_path"])

        if row["wamf_clip_path"]:
            referenced_files.add(row["wamf_clip_path"])

    media_dirs = [Path("media/wamf/snapshots"), Path("media/wamf/clips")]

    orphan_count = 0
    missing_count = 0

    for media_dir in media_dirs:

        for file_path in media_dir.glob("*"):

            file_str = str(file_path)

            if file_str not in referenced_files:

                print(f"[ORPHAN] {file_str}")

                log_system_event("WARN", "RETENTION", f"Orphan detected: {file_str}")

                orphan_count += 1

                if delete_orphaned_media and file_path.is_file():

                    file_path.unlink()

                    log_system_event(
                        "INFO",
                        "RETENTION",
                        f"Deleted orphan media: {file_str}",
                    )

    for file_path in referenced_files:

        if not Path(file_path).exists():

            print(f"[MISSING] {file_path}")

            missing_count += 1

    print("\nOrphan scan complete")
    print(f"Orphans found: {orphan_count}")
    print(f"Missing files: {missing_count}")

    log_system_event(
        "INFO",
        "RETENTION",
        f"Retention scan complete. "
        f"Scanned {len(rows)} rows. "
        f"Orphans found: {orphan_count}. "
        f"Missing files: {missing_count}",
    )

    cursor.execute("""
    DELETE FROM retention_status
""")

    cursor.execute(
        """
        INSERT INTO retention_status (
            last_run,
            rows_scanned,
            orphan_count,
            missing_count
        )
        VALUES (?, ?, ?, ?)
    """,
        (datetime.now().isoformat(), len(rows), orphan_count, missing_count),
    )

    conn.commit()

    conn.close()


if __name__ == "__main__":

    dry_run_retention()

    print()

    scan_for_orphans()
