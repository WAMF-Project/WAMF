# Docker installation and testing

Run these commands from a WAMF checkout on the Docker host:

```bash
docker compose -f docker-compose.yml.example up -d --build
docker compose -f docker-compose.yml.example logs -f
```

The example Compose file builds this checkout. `docker-compose.yml` uses its
configured registry image; that image must include these packaging changes before
it can provide the same behaviour.

Open `http://<docker-host>:7767/login`. On first startup WAMF creates missing
configuration and generates a session secret and temporary admin password using
its existing bootstrap. The password is printed once to the container logs;
sign in and change it. Valid existing credentials are preserved.

Incomplete Frigate/MQTT settings start the Admin UI in setup mode without the
detection worker. Use Configuration to enter the Frigate URL, MQTT host/topic,
cameras, and any credentials/TLS options. Use addresses reachable from the
container: `127.0.0.1` refers to WAMF's container, not the Docker host. Certificate
paths must likewise exist inside the container (for example under `/app/config`).
Keep `webui.port: 7767` and `webui.host: 0.0.0.0` for these Compose mappings.

Save stores changes only. Restart and Save & Restart stop/join the workers and
re-execute the parent; the browser waits for the new Admin UI instance. You can
also edit the YAML manually. Optional API tokens and live view can be configured
through the existing mechanisms. Bridge remains disabled by default.

## Persistent directories

Both Compose files mount:

```text
./config/  -> /app/config/
./data/    -> /app/data/
./media/   -> /app/media/
```

The relative database and media paths in both config examples resolve beneath
`/app`, matching these mounts. WAMF creates missing database/media directories and
the SQLite schema. The entire config directory persists generated credentials,
configuration edits, and timestamped config backups.

For an empty config mount, the Docker entrypoint copies a bundled example into
`/app/config/config.yml.example` only when that example is absent. Python then
performs the normal bootstrap. Existing examples and configuration are not
replaced. Actual host configuration, credentials, and backups are excluded from
the image build context.

The image currently runs as root. Generated config files use mode `0600`, so host
inspection/editing may require elevated permissions. If running with a custom
UID, give that user write access to the three mounted directories first.

## Existing Docker installations

Back up existing configuration, database, and media before changing the mounts.
Older Docker config examples used `/data/speciesid.db` and `/media/wamf/...`.
Update those settings to `./data/speciesid.db`, `./media/wamf/snapshots`, and
`./media/wamf/clips` (or the equivalent `/app/...` paths). Mount changes do not
rewrite existing configuration. Update an existing `webui.port: 7766` to `7767`.

The database may contain absolute archive paths beginning `/media/`. Those rows
retain their old paths; either migrate them to the new locations with a backed-up,
reviewed database migration, or retain an additional `/media` compatibility mount
for that installation. A fresh installation needs only the supplied mounts.

## Validation checklist

- Build succeeds and `docker compose -f docker-compose.yml.example ps` shows WAMF running.
- Starting with an empty config directory creates the example and configuration,
  prints a temporary password, and serves the setup/admin UI on port 7767.
- Login, password changes, config saves/backups, and Admin Restart work.
- After configuration, logs report `MQTT event subscription ready`; the first
  qualifying live bird event is classified and appears in the UI.
- `data/speciesid.db`, `media/wamf/snapshots`, and `media/wamf/clips` exist on the host.
- Credentials, detections, and archived files survive container recreation.
- `docker compose -f docker-compose.yml.example stop` cleanly stops all workers.
- `retention.py` is present for separately scheduled maintenance; it is not
  automatically scheduled by the container.

Stop and remove the container while preserving the host directories:

```bash
docker compose -f docker-compose.yml.example down
```
