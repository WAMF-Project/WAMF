#!/bin/sh
set -eu

# A bind mount hides image files at /app/config. Seed only the example;
# Python remains responsible for config creation, credentials, and preflight.
mkdir -p /app/config
if [ ! -e /app/config/config.yml.example ]; then
    cp /usr/local/share/wamf/config.yml.example /app/config/config.yml.example
fi

exec "$@"
