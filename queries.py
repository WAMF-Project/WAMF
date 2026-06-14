from collections import defaultdict
from datetime import datetime
from pathlib import Path

from db import (
    attach_names_db,
    connect_db,
    connect_names_db,
    DB_PATH as DEFAULT_DB_PATH,
    NAMES_DB_PATH as DEFAULT_NAMES_DB_PATH,
    DETECTION_SELECT,
)


DBPATH = DEFAULT_DB_PATH
NAMEDBPATH = DEFAULT_NAMES_DB_PATH


def get_scientific_name(common_name):
    conn = connect_names_db(NAMEDBPATH)
    cursor = conn.cursor()
    # Try exact match first
    cursor.execute(
        "SELECT scientific_name FROM birdnames WHERE common_name = ?", (common_name,)
    )
    result = cursor.fetchone()
    if result:
        conn.close()
        return result[0]
    # If string is exactly 20 chars it may be truncated — try prefix match
    if len(common_name) == 20:
        cursor.execute(
            "SELECT scientific_name FROM birdnames WHERE common_name LIKE ?",
            (common_name + '%',)
        )
        rows = cursor.fetchall()
        conn.close()
        # Only use prefix match if exactly one species starts with this prefix
        return rows[0][0] if len(rows) == 1 else None
    conn.close()
    return None


def get_common_name(scientific_name):
    conn = connect_names_db(NAMEDBPATH)
    cursor = conn.cursor()

    cursor.execute("SELECT common_name FROM birdnames WHERE scientific_name = ?", (scientific_name,))
    result = cursor.fetchone()

    conn.close()

    if result:
        return result[0]
    else:
        print(f"No common name for: {scientific_name}", flush=True)
        return None

def save_species_info(
    scientific_name,
    common_name=None,
    description=None,
    wikipedia_url=None,
    ebird_url=None,
    inaturalist_url=None,
    gbif_url=None,
    thumbnail_url=None
):

    conn = connect_db(DBPATH)

    cursor = conn.cursor()

    cursor.execute(
        """
        INSERT OR REPLACE INTO species_info (
            scientific_name,
            common_name,
            description,
            wikipedia_url,
            ebird_url,
            inaturalist_url,
            gbif_url,
            last_updated,
            thumbnail_url
        )
        VALUES (
            ?, ?, ?, ?, ?, ?, ?, datetime('now'), ?
        )
        """,
        (
            scientific_name,
            common_name,
            description,
            wikipedia_url,
            ebird_url,
            inaturalist_url,
            gbif_url,
            thumbnail_url
        )
    )

    conn.commit()

    conn.close()

def get_species_info(
    scientific_name
):

    conn = connect_db(DBPATH)

    
    cursor = conn.cursor()

    cursor.execute(
        """
        SELECT
            scientific_name,
            common_name,
            description,
            wikipedia_url,
            ebird_url,
            inaturalist_url,
            gbif_url,
            last_updated,
            thumbnail_url
        FROM species_info
        WHERE scientific_name = ?
        """,
        (
            scientific_name,
        )
    )

    result = cursor.fetchone()

    conn.close()

    if result:
        return dict(result)

    return None

def get_all_species_info():

    conn = connect_db(DBPATH)

    
    cursor = conn.cursor()

    cursor.execute(
        """
        SELECT
            scientific_name,
            common_name,
            description,
            wikipedia_url,
            ebird_url,
            inaturalist_url,
            gbif_url,
            last_updated,
            thumbnail_url
        FROM species_info
        ORDER BY common_name
        """
    )

    results = cursor.fetchall()

    conn.close()

    return [
        dict(row)
        for row in results
    ]

def detection_row_to_dict(row):
    detection = dict(row)
    detection['confidence_percent'] = int(detection['score'] * 100)
    detection['snapshot_file'] = (
        Path(detection['wamf_snapshot_path']).name
        if detection['wamf_snapshot_path']
        else None
    )
    detection['clip_file'] = (
        Path(detection['wamf_clip_path']).name
        if detection['wamf_clip_path']
        else None
    )
    detection['common_name'] = (
        detection.get('common_name')
        or get_common_name(detection['display_name'])
    )
    return detection


def recent_detections(num_detections):

    conn = connect_db(DBPATH)
    attach_names_db(conn, NAMEDBPATH)

    cursor = conn.cursor()

    cursor.execute(
        f"""
        SELECT
            {DETECTION_SELECT},
            COALESCE(
                birdnames_db.birdnames.common_name,
                detections.display_name
            ) AS common_name
        FROM detections
        LEFT JOIN birdnames_db.birdnames
        ON detections.display_name = birdnames_db.birdnames.scientific_name
        ORDER BY detections.detection_time DESC
        LIMIT ?
        """,
        (num_detections,)
    )

    results = cursor.fetchall()

    conn.close()

    return [
        detection_row_to_dict(row)
        for row in results
    ]


def get_daily_summary(date):
    date_str = date.strftime('%Y-%m-%d')
    conn = connect_db(DBPATH)
    cursor = conn.cursor()

    attach_names_db(conn, NAMEDBPATH)

    query = '''
        SELECT
            detections.display_name,
            COALESCE(
                birdnames_db.birdnames.common_name,
                detections.display_name
            ) AS common_name,
            STRFTIME('%H', detections.detection_time) AS hour,
            COUNT(*) AS hourly_detections
        FROM detections
        LEFT JOIN birdnames_db.birdnames
        ON detections.display_name = birdnames_db.birdnames.scientific_name
        WHERE DATE(detections.detection_time) = ?
        GROUP BY detections.display_name, common_name, hour
        ORDER BY detections.display_name, hour
    '''

    cursor.execute(query, (date_str,))
    rows = cursor.fetchall()

    summary = defaultdict(lambda: {
        'scientific_name': '',
        'common_name': '',
        'total_detections': 0,
        'hourly_detections': [0] * 24
    })

    for row in rows:
        display_name = row['display_name']
        summary[display_name]['scientific_name'] = display_name
        summary[display_name]['common_name'] = row['common_name']
        summary[display_name]['total_detections'] += row['hourly_detections']
        summary[display_name]['hourly_detections'][int(row['hour'])] = row['hourly_detections']

    conn.close()
    return dict(summary)


def get_records_for_date_hour(date, hour):
    conn = connect_db(DBPATH)
    attach_names_db(conn, NAMEDBPATH)
    cursor = conn.cursor()

    query = f'''
        SELECT
            {DETECTION_SELECT},
            COALESCE(
                birdnames_db.birdnames.common_name,
                detections.display_name
            ) AS common_name
        FROM detections
        LEFT JOIN birdnames_db.birdnames
        ON detections.display_name = birdnames_db.birdnames.scientific_name
        WHERE strftime('%Y-%m-%d', detections.detection_time) = ?
        AND strftime('%H', detections.detection_time) = ?
        ORDER BY detections.detection_time
    '''

    cursor.execute(query, (date, str(hour).zfill(2)))
    records = cursor.fetchall()
    conn.close()

    return [
        detection_row_to_dict(record)
        for record in records
    ]


def get_records_for_scientific_name_and_date(
    scientific_name,
    date,
    page,
    per_page
):

    conn = connect_db(DBPATH)
    attach_names_db(conn, NAMEDBPATH)
    cursor = conn.cursor()

    offset = (
        page - 1
    ) * per_page

    query = f"""
        SELECT
            {DETECTION_SELECT},
            COALESCE(
                birdnames_db.birdnames.common_name,
                detections.display_name
            ) AS common_name
        FROM detections
        LEFT JOIN birdnames_db.birdnames
        ON detections.display_name = birdnames_db.birdnames.scientific_name
        WHERE detections.display_name = ?
        AND strftime('%Y-%m-%d', detections.detection_time) = ?
        ORDER BY detections.detection_time DESC
        LIMIT ?
        OFFSET ?
    """

    cursor.execute(
        query,
        (
            scientific_name,
            date,
            per_page,
            offset
        )
    )

    records = cursor.fetchall()
    conn.close()

    return [
        detection_row_to_dict(record)
        for record in records
    ]

def get_detection_count_for_scientific_name_and_date(
    scientific_name,
    date
):

    conn = connect_db(DBPATH)

    cursor = conn.cursor()

    cursor.execute(
        """
        SELECT COUNT(*)
        FROM detections
        WHERE display_name = ?
        AND strftime('%Y-%m-%d', detection_time) = ?
        """,
        (
            scientific_name,
            date
        )
    )

    count = cursor.fetchone()[0]

    conn.close()

    return count

def get_earliest_detection_date():
    conn = connect_db(DBPATH)
    cursor = conn.cursor()
    cursor.execute("SELECT MIN(date(detection_time)) FROM detections")
    earliest_date = cursor.fetchone()[0]
    conn.close()
    if earliest_date:
        return earliest_date
    else:
        return None


def get_activity_by_hour(date_str):
    conn = connect_db(DBPATH)
    
    rows = conn.execute("""
        SELECT
            strftime('%H', detection_time) as hour,
            COUNT(*) as total
        FROM detections
        WHERE date(detection_time) = ?
        GROUP BY hour
        ORDER BY hour
    """, (date_str,)).fetchall()

    conn.close()

    return rows


def get_top_species(date_str):
    conn = connect_db(DBPATH)
    
    attach_names_db(conn, NAMEDBPATH)

    rows = conn.execute("""
        SELECT
            detections.display_name AS scientific_name,            
            COALESCE(
                birdnames_db.birdnames.common_name,
                detections.display_name
            ) AS common_name,

            COUNT(*) as total

        FROM detections

        LEFT JOIN birdnames_db.birdnames
        ON detections.display_name =
           birdnames_db.birdnames.scientific_name

        WHERE detection_time LIKE ?

        GROUP BY detections.display_name

        ORDER BY total DESC

        LIMIT 5
    """, (f"{date_str}%",)).fetchall()

    conn.close()

    return rows


def get_latest_visitor():
    conn = connect_db(DBPATH)
    
    attach_names_db(conn, NAMEDBPATH)

    row = conn.execute("""
        SELECT
            detections.display_name AS scientific_name,
            detections.id,
            detections.detection_time,
            detections.detection_index,
            detections.score,
            detections.display_name,
            detections.category_name,
            detections.frigate_event,
            detections.camera_name,
            detections.wamf_snapshot_path,
            detections.wamf_clip_path,

            COALESCE(
                birdnames_db.birdnames.common_name,
                detections.display_name
            ) AS common_name

        FROM detections

        LEFT JOIN birdnames_db.birdnames
        ON detections.display_name =
           birdnames_db.birdnames.scientific_name

        ORDER BY detection_time DESC

        LIMIT 1
    """).fetchone()

    conn.close()

    return row

def get_species_peak_hours(date_str):

    conn = connect_db(DBPATH)
    
    attach_names_db(conn, NAMEDBPATH)

    rows = conn.execute("""

        SELECT
            common_name,
            hour,
            total

        FROM (

            SELECT

                COALESCE(
                    birdnames_db.birdnames.common_name,
                    detections.display_name
                ) AS common_name,

                strftime('%H', detection_time) AS hour,

                COUNT(*) AS total,

                ROW_NUMBER() OVER (
                    PARTITION BY detections.display_name
                    ORDER BY COUNT(*) DESC
                ) AS rn

            FROM detections

            LEFT JOIN birdnames_db.birdnames
            ON detections.display_name =
               birdnames_db.birdnames.scientific_name

            WHERE detection_time LIKE ?

            GROUP BY detections.display_name, hour

        )

        WHERE rn = 1

        ORDER BY total DESC

    """, (f"{date_str}%",)).fetchall()

    conn.close()

    return rows

def get_species_stats(scientific_name):

    conn = connect_db(DBPATH)
    
    row = conn.execute("""

        SELECT

            COUNT(*) AS total_detections,

            MIN(detection_time) AS first_seen,

            MAX(detection_time) AS last_seen,

            strftime(
                '%H',
                detection_time
            ) AS peak_hour

        FROM detections

        WHERE display_name = ?

    """, (scientific_name,)).fetchone()

    conn.close()

    return row

def get_species_stats_for_date(
    scientific_name,
    date
):

    conn = connect_db(DBPATH)

    
    row = conn.execute(
        """

        SELECT

            COUNT(*) AS total_detections,

            MIN(detection_time) AS first_seen,

            MAX(detection_time) AS last_seen,

            strftime(
                '%H',
                detection_time
            ) AS peak_hour

        FROM detections

        WHERE display_name = ?
        AND strftime(
            '%Y-%m-%d',
            detection_time
        ) = ?

    """,
        (
            scientific_name,
            date
        )
    ).fetchone()

    conn.close()

    return row

def get_species_activity_by_hour(scientific_name):

    conn = connect_db(DBPATH)
    
    rows = conn.execute("""

        SELECT

            strftime('%H', detection_time) AS hour,

            COUNT(*) AS total

        FROM detections

        WHERE display_name = ?

        GROUP BY hour

        ORDER BY hour

    """, (scientific_name,)).fetchall()

    conn.close()

    return rows

def get_admin_stats():

    conn = connect_db(DBPATH)
        
    total_detections = conn.execute("""
        SELECT COUNT(*) AS count
        FROM detections
    """).fetchone()["count"]

    archived_snapshots = conn.execute("""
        SELECT COUNT(*) AS count
        FROM detections
        WHERE wamf_snapshot_path IS NOT NULL
    """).fetchone()["count"]

    archived_clips = conn.execute("""
        SELECT COUNT(*) AS count
        FROM detections
        WHERE wamf_clip_path IS NOT NULL
    """).fetchone()["count"]

    conn.close()

    archive_dirs = [
        Path("media/wamf/snapshots"),
        Path("media/wamf/clips")
    ]

    total_size = 0

    for archive_dir in archive_dirs:

        if archive_dir.exists():

            total_size += sum(
                f.stat().st_size
                for f in archive_dir.glob("**/*")
                if f.is_file()
            )

    archive_size_mb = round(
        total_size / (1024 * 1024),
        2
    )

    return {
        "total_detections": total_detections,
        "archived_snapshots": archived_snapshots,
        "archived_clips": archived_clips,
        "archive_size_mb": archive_size_mb
    }

def get_recent_system_events(limit=100, event_type=None):

    conn = connect_db(DBPATH)
    
    
    cursor = conn.cursor()

    if event_type:

        cursor.execute("""
            SELECT
                timestamp,
                severity,
                event_type,
                message
            FROM system_events
            WHERE event_type = ?
            ORDER BY id DESC
            LIMIT ?
        """, (
            event_type,
            limit
        ))

    else:

        cursor.execute("""
            SELECT
                timestamp,
                severity,
                event_type,
                message
            FROM system_events
            ORDER BY id DESC
            LIMIT ?
        """, (
            limit,
        ))

    rows = cursor.fetchall()

    formatted_rows = []

    for row in rows:

        row = dict(row)

        dt = datetime.fromisoformat(
            row["timestamp"]
        )

        row["timestamp"] = dt.strftime(
            "%Y-%m-%d %H:%M:%S"
        )

        formatted_rows.append(row)

    conn.close()

    return formatted_rows


def get_retention_status():

    conn = connect_db(DBPATH)
    
    cursor = conn.cursor()

    cursor.execute("""
        SELECT
            last_run,
            rows_scanned,
            orphan_count,
            missing_count
        FROM retention_status
        LIMIT 1
    """)

    row = cursor.fetchone()

    conn.close()

    if row:

        row = dict(row)

        dt = datetime.fromisoformat(
            row["last_run"]
        )

        row["last_run"] = dt.strftime(
            "%d %b %Y %H:%M"
        )

        return row

    return None
   