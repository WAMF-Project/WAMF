# Docker installation and beta testing

This setup keeps the WAMF config, SQLite database, snapshots, and clips on the
host. Container paths are deployment-neutral and do not depend on Bird-Lab.

## Install

From a WAMF checkout on the Docker host:

```bash
cp config/config.docker.yml.example config/config.yml
mkdir -p data media
```

Edit `config/config.yml` before starting. At minimum, set:

- `frigate.frigate_url`, `frigate.mqtt_server`, and `frigate.camera`
- MQTT credentials/TLS options when the broker requires them
- `admin.session_secret` and `admin.password_hash`
- `api.token_hash` if API token authentication will be used
- `camera.live_view_url` when a live view is available; an empty value disables it

Generate the two admin values with:

```bash
python -c "import secrets; print(secrets.token_hex(32))"
python -c "from werkzeug.security import generate_password_hash; print(generate_password_hash('change-me'))"
```

Use addresses reachable from inside Docker; `127.0.0.1` refers to the WAMF
container itself. Then build and start the local checkout:

```bash
docker compose -f docker-compose.yml.example up -d --build
docker compose -f docker-compose.yml.example logs -f
```

Open `http://<docker-host>:7766`.

## Volume layout

```text
./config/config.yml  -> /app/config/config.yml
./data/              -> /data/
./media/             -> /media/
```

The Docker config stores the database at `/data/speciesid.db`, snapshots at
`/media/wamf/snapshots`, and clips at `/media/wamf/clips`. WAMF creates the
database parent and both media directories on startup when they are missing.

## Basic beta checklist

- `docker compose -f docker-compose.yml.example ps` shows the service running.
- The logs show Flask and MQTT startup without config or permission errors.
- `data/speciesid.db`, `media/wamf/snapshots`, and `media/wamf/clips` exist on the host.
- The UI and admin login load at port 7766.
- Frigate/MQTT connectivity is healthy and a test bird event is received.
- A detection survives `docker compose -f docker-compose.yml.example restart`.
- A captured snapshot/clip is written below `media/wamf/` and remains after restart.

Stop the test deployment without deleting persistent host data:

```bash
docker compose -f docker-compose.yml.example down
```
