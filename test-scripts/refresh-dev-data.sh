#!/bin/bash

DB_PATH="$(.venv311/bin/python -c 'from wamf_paths import get_database_path; print(get_database_path())')"

sqlite3 "$DB_PATH" <<EOF
UPDATE detections
SET detection_time = datetime('now');
EOF

echo "WAMF detection timestamps refreshed."
