#!/bin/bash

sqlite3 data/speciesid.db <<EOF
UPDATE detections
SET detection_time = datetime('now');
EOF

echo "WAMF detection timestamps refreshed."