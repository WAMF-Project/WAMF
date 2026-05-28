import sqlite3
from collections import defaultdict
from datetime import datetime
from pathlib import Path


DBPATH = './data/speciesid.db'
NAMEDBPATH = './birdnames.db'


def get_scientific_name(common_name):
    conn = sqlite3.connect(NAMEDBPATH)
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
    conn = sqlite3.connect(NAMEDBPATH)
    cursor = conn.cursor()

    cursor.execute("SELECT common_name FROM birdnames WHERE scientific_name = ?", (scientific_name,))
    result = cursor.fetchone()

    conn.close()

    if result:
        return result[0]
    else:
        print(f"No common name for: {scientific_name}", flush=True)
        return None


def recent_detections(num_detections):
    conn = sqlite3.connect(DBPATH)
    cursor = conn.cursor()

    cursor.execute("SELECT * FROM detections ORDER BY detection_time DESC LIMIT ?", (num_detections,))
    results = cursor.fetchall()

    conn.close()

    formatted_results = []
    for result in results:
        detection = {
            'id': result[0],
            'detection_time': result[1],
            'detection_index': result[2],
            'score': result[3],
            'display_name': result[4],
            'category_name': result[5],
            'frigate_event': result[6],
            'camera_name': result[7],
            'common_name': get_common_name(result[4])
        }
        formatted_results.append(detection)

    return formatted_results


def get_daily_summary(date):
    date_str = date.strftime('%Y-%m-%d')
    conn = sqlite3.connect(DBPATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    query = '''  
        SELECT display_name,  
               COUNT(*) AS total_detections,  
               STRFTIME('%H', detection_time) AS hour,  
               COUNT(*) AS hourly_detections  
        FROM (  
            SELECT *  
            FROM detections  
            WHERE DATE(detection_time) = ?  
        ) AS subquery  
        GROUP BY display_name, hour  
        ORDER BY total_detections DESC, display_name, hour  
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
        summary[display_name]['common_name'] = get_common_name(display_name)
        summary[display_name]['total_detections'] += row['hourly_detections']
        summary[display_name]['hourly_detections'][int(row['hour'])] = row['hourly_detections']

    conn.close()
    return dict(summary)


def get_records_for_date_hour(date, hour):
    conn = sqlite3.connect(DBPATH)
    conn.row_factory = sqlite3.Row  # Set the row factory to sqlite3.Row
    cursor = conn.cursor()

    # The SQL query to fetch records for the given date and hour, sorted by detection_time
    query = '''    
        SELECT *    
        FROM detections    
        WHERE strftime('%Y-%m-%d', detection_time) = ? AND strftime('%H', detection_time) = ?    
        ORDER BY detection_time    
    '''

    cursor.execute(query, (date, str(hour).zfill(2)))
    records = cursor.fetchall()

    # Append the common name for each record
    result = []
    for record in records:
        common_name = get_common_name(record['display_name'])  # Access the field by name
        record_dict = dict(record)  # Convert the record to a dictionary
        record_dict['common_name'] = common_name  # Add the 'common_name' key to the record dictionary
        result.append(record_dict)

    conn.close()

    return result


def get_records_for_scientific_name_and_date(scientific_name, date):
    conn = sqlite3.connect(DBPATH)
    conn.row_factory = sqlite3.Row  # Set the row factory to sqlite3.Row
    cursor = conn.cursor()

    # The SQL query to fetch records for the given display_name and date, sorted by detection_time
    query = '''    
        SELECT *    
        FROM detections    
        WHERE display_name = ? AND strftime('%Y-%m-%d', detection_time) = ?    
        ORDER BY detection_time    
    '''

    cursor.execute(query, (scientific_name, date))
    records = cursor.fetchall()

    # Append the common name for each record
    result = []
    for record in records:
        common_name = get_common_name(record['display_name'])  # Access the field by name
        record_dict = dict(record)  # Convert the record to a dictionary
        record_dict['common_name'] = common_name  # Add the 'common_name' key to the record dictionary
        result.append(record_dict)

    conn.close()

    return result


def get_earliest_detection_date():
    conn = sqlite3.connect(DBPATH)
    cursor = conn.cursor()
    cursor.execute("SELECT MIN(date(detection_time)) FROM detections")
    earliest_date = cursor.fetchone()[0]
    conn.close()
    if earliest_date:
        return earliest_date
    else:
        return None


def get_activity_by_hour(date_str):
    conn = sqlite3.connect(DBPATH)
    conn.row_factory = sqlite3.Row

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
    conn = sqlite3.connect(DBPATH)
    conn.row_factory = sqlite3.Row

    conn.execute("ATTACH DATABASE 'birdnames.db' AS birdnames_db")

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
    conn = sqlite3.connect(DBPATH)
    conn.row_factory = sqlite3.Row

    conn.execute("ATTACH DATABASE 'birdnames.db' AS birdnames_db")

    row = conn.execute("""
        SELECT
            detections.display_name AS scientific_name,           
            detections.*,

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

    conn = sqlite3.connect(DBPATH)
    conn.row_factory = sqlite3.Row

    conn.execute("ATTACH DATABASE 'birdnames.db' AS birdnames_db")

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

    conn = sqlite3.connect(DBPATH)
    conn.row_factory = sqlite3.Row

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

def get_species_activity_by_hour(scientific_name):

    conn = sqlite3.connect(DBPATH)
    conn.row_factory = sqlite3.Row

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

    conn = sqlite3.connect(DBPATH)
    conn.row_factory = sqlite3.Row
    
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