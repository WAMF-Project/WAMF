#!/usr/bin/env bash
# Rolls back WAMF to the previously deployed image.
# Usage: bash scripts/rollback.sh

set -euo pipefail

DEPLOY_DIR="${DEPLOY_DIR:?DEPLOY_DIR must be set, e.g. export DEPLOY_DIR=/opt/wamf}"
COMPOSE="$DEPLOY_DIR/docker-compose.yml"
LAST_DEPLOYED="$DEPLOY_DIR/.last-deployed"

if [[ ! -f "$LAST_DEPLOYED" ]]; then
  echo "No previous deploy recorded at $LAST_DEPLOYED. Cannot roll back."
  exit 1
fi

previous=$(cat "$LAST_DEPLOYED")
current=$(grep 'image:' "$COMPOSE" | awk '{print $2}')

echo "Current:  $current"
echo "Rolling back to: $previous"

sed -i "s|image: .*|image: $previous|" "$COMPOSE"
docker compose -f "$COMPOSE" up -d

echo "Done. Verify with: curl http://localhost:7766"
