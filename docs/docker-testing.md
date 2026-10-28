# Docker installation and testing

Run these commands from a WAMF checkout on the Docker host:

```bash
docker compose -f docker-compose.yml.example up -d --build
docker compose -f docker-compose.yml.example logs -f
```

The example Compose file builds this checkout. `docker-compose.yml` uses its
configured registry image; that image must include these packaging changes before
it can provide the same behaviour.

On first startup WAMF creates missing configuration and prints a private
`/setup#token=...` link once to the container logs. Open it within 15 minutes,
choose a password, then use the normal login page. Operational workers remain
paused until administrator setup succeeds. Valid existing credentials are preserved.
Restarting before completion replaces the link; old links stop working.

Set the optional `webui.public_base_url` to the external origin, such as
`https://wamf.example`, to print a directly usable link. Otherwise replace the
printed loopback host and port with the Docker host and published port. Remote
password setup requires HTTPS or a trusted tunnel. Protect the startup log and
disable proxy request-body logging for setup. The page requires JavaScript and
does not put its token in HTTP request URLs or browser storage.

Mount a writable configuration directory, as in the Compose examples. Read-only
configuration and single-file config mounts cannot support atomic replacement;
WAMF fails safely without falling back to an in-place write.

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

<a id="upgrading-v040-to-v041"></a>

### Upgrading v0.4.0 or v0.4.1 to v0.4.2

Stop WAMF cleanly and back up the complete configuration, database and media
directories together before upgrading. Keep the existing image available for
rollback. Recreate the container with the v0.4.2 image and the same mounts,
configuration, port mapping and `WAMF_SECRET_KEY` environment value, if used.
For a local checkout build, use the example Compose commands above. Registry
tag `0.4.2` is usable only after the release has been published.

No configuration, database or media migration is required for existing v0.4.0
or v0.4.1 installations with valid credentials. Preserve
any additional `/data` or `/media` compatibility mounts already in use. Existing
credentials are verified without automatic rehashing, including supported legacy
Werkzeug formats. Malformed or unsupported configured hashes stop startup with a
configuration error and are never replaced automatically. Only a genuinely
unconfigured installation receives the private first-run setup link.

After upgrading, check login, API access, archived media, MQTT detection and
restart persistence. To roll back, stop the candidate cleanly and recreate the
previous container using the same mounts and session-secret environment value.
Avoid simultaneous writers to the database.
See [v0.4.2 release notes](release-0.4.2.md) for validation and security limitations.

### Older path conventions

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
  prints a single-use setup link, and serves the setup page on port 7767.
- Before password setup, operational workers remain paused; successful setup
  starts them automatically when Frigate configuration is already complete.
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
