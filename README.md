#  WAMF (Wildlife Activity Monitoring Framework)

> A wildlife observatory platform built around Frigate NVR, bird species identification, feeder activity analytics, and live wildlife monitoring.

---

## Project Background

WAMF is a fork of:

- https://github.com/k1n6b0b/whosatmyfeeder

Which itself is a fork of the original project:

- https://github.com/mmcc-xx/WhosAtMyFeeder

All original work and credit belongs to the original authors and contributors.

This fork has evolved beyond the original sidecar classifier concept and is now focused on building a modern wildlife observatory platform around:

- Frigate integration
- Species analytics
- Activity visualisation
- Observatory-style UI/UX
- Live detection feeds
- Species exploration
- Behaviour tracking

while remaining compatible with the original Frigate-based bird detection workflow.

---

# Observatory Features

## Detection Pipeline

- Bird species classification from Frigate snapshots
- SQLite-backed event storage
- MQTT integration
- Frigate sub-label support
- Confidence scoring
- Species taxonomy lookup database

## Observatory UI

- Live recent detections feed
- Daily summaries
- Hourly activity views
- Species exploration pages
- Activity analytics dashboard
- Interactive navigation between detections and species
- Mobile-friendly responsive interface
- Thumbnail abstraction layer for Frigate or development media

## Activity Analytics

- Activity-by-hour visualisation
- Species peak activity tracking
- Detection timelines
- Top visitor statistics
- Behaviour-focused observatory views

## Infrastructure

- MQTT TLS support
- GitHub Container Registry images
- Automated CI pipeline
- Vulnerability scanning
- Python 3.11 environment
- Modern Flask-based web UI

---

# Architecture

WAMF combines Frigate detections, species classification, and observatory analytics into a unified wildlife monitoring platform.

```text
Frigate → MQTT/Event Detection
        ↓
Species Classification
        ↓
SQLite Detection Store
        ↓
birdnames.db Taxonomy Lookup
        ↓
Flask Observatory UI
```

---

# Features Added In Previous Forks

## From k1n6b0b fork

- MQTT TLS support
- MQTT detection publish support
- MQTT new species alerts
- Frigate sub-label fallback support
- Python 3.11 base image
- GitHub Container Registry publishing
- CI pipeline improvements

## Features Added In This Fork

- Observatory dashboard redesign
- Live recent detection feed
- Activity analytics page
- Species activity tracking
- Interactive species navigation
- Thumbnail abstraction system
- Modernised observatory UI
- Improved responsive layouts
- Development thumbnail support
- Fake detection seeding workflows (For development only)
- Refactored analytics and summary views

---

# Screenshots

## Dashboard

An overview of recent wildlife observations and activity recorded by WAMF.

![WAMF dashboard overview](docs/assets/dashboard-overview.png)

## Recent Observations

Recent bird detections with species identification and observation media.

![WAMF recent observations](docs/assets/dashboard-recent-feed.png)

## Activity

Bird activity over time, showing when wildlife is most active.

![WAMF activity view](docs/assets/activity.png)

---

# Prerequisites

- A working Frigate installation
- An MQTT broker connected to Frigate
- Frigate configured to detect the bird object
- Snapshots enabled in Frigate

---

# Frigate Configuration

## Frigate must be configured to:

- detect birds
- generate snapshots
- publish MQTT events

Example configuration:

```yaml
mqtt:
  host: <your-mqtt-host>
  port: 1883
  topic_prefix: frigate
  user: mqtt_username_here
  password: mqtt_password_here

detectors:
  coral:
    type: edgetpu
    device: usb

objects:
  track:
    - bird

snapshots:
  enabled: true

cameras:
  birdcam:
    record:
      enabled: true
      events:
        objects:
          - bird

    ffmpeg:
      inputs:
        - path: rtsp://<camera-ip>:8554/cam
          roles:
            - detect
            - record
```

---

# Setup

## Directory Structure

```text

/WAMF/
├── docker-compose.yml
├── config/
│   └── config.yml
└── data/

```

---

# Configuration

Native startup creates `config/config.yml` from the example when absent. You can
also prepare it beforehand by copying:

```text

config/config.yml.example

```

to:

```text

config/config.yml

```

# Example:

```yaml
frigate:
  frigate_url: http://<frigate-ip>:5000

  mqtt_server: <mqtt-host>
  mqtt_auth: false
  mqtt_port: 1883

  main_topic: frigate

  camera:
    - birdcam

  object: bird

classification:
  model: model.tflite
  threshold: 0.7

webui:
  port: 7767
  host: 0.0.0.0
```

# Docker Compose

Latest stable: [v0.4.3](https://github.com/WAMF-Project/WAMF/releases/tag/v0.4.3),
published 2026-10-28.

For v0.4.3 upgrade guidance and known security limitations, see the
[security maintenance release notes](docs/release-0.4.3.md).

For a registry installation, set the `wamf` service's `image` in your deployment's
`docker-compose.yml` to `ghcr.io/wamf-project/wamf:0.4.3`. The supplied file uses
`latest`, which the release publishing workflow does not update. Existing
installations should first follow the [stop and backup steps](docs/docker-testing.md#upgrading-v042-to-v043).
Then pull and start the published image:

```bash
docker compose -f docker-compose.yml pull wamf
docker compose -f docker-compose.yml up -d wamf
docker compose -f docker-compose.yml logs -f wamf
```

Alternatively, build and run the current checkout:

```bash
docker compose -f docker-compose.yml.example up -d --build
docker compose -f docker-compose.yml.example logs -f
```

First startup creates configuration and prints a private administrator setup link.
Open that link within 15 minutes to choose a password, then sign in and configure
Frigate/MQTT through the Admin UI. Remote password setup requires HTTPS or a trusted tunnel.
Config, database, and media persist in `./config`, `./data`, and `./media`, mounted
beneath `/app`. See [Docker installation and testing](docs/docker-testing.md) for
setup, existing-install migration, and validation details.

---

# Development

```bash

python3.11 -m venv .venv

source .venv/bin/activate

pip install -r requirements.txt

```

Run the native application:

```bash
python speciesid.py
```

On first native start, WAMF creates `config/config.yml` from the native example
if it is missing. If Frigate, MQTT, or camera settings are incomplete, WAMF
starts the web/admin UI in setup mode on port 7767 (or your configured port).
The detection worker stays disabled, and health reports setup required instead
of treating placeholder settings as service outages.

Sign in and open Configuration to enter the Frigate URL, MQTT broker/topic,
camera names, and any required MQTT credentials/TLS options. Save & Restart
saves the configuration, stops and joins the workers, and re-executes the current
Python command in the same parent process. Restart applies the existing saved
configuration the same way. Both actions work directly in a terminal and under
systemd, without requiring an external supervisor to relaunch WAMF. The browser
waits for the new application instance, then reloads the Admin UI. Saving alone
shows a restart-required message and does not start detection. Advanced users can still edit the YAML file directly.
Structurally invalid configuration or invalid web bind settings remain startup errors.

On a genuinely unprovisioned installation with administrator authentication enabled,
WAMF prints one private `/setup#token=...` link to the startup console. It expires
after 15 minutes and can create the administrator password once. The page requires
JavaScript, removes the token from the address bar, and sends it only in the setup
POST body. Choose and confirm a password of at least eight characters, then use
the normal login page. Only the token digest and password hash are persisted.
Treat the startup log as sensitive: anyone with the unused link can complete setup.

Operational workers remain paused until administrator setup succeeds. They resume
automatically once setup completes if Frigate configuration is ready. Incomplete
Frigate/MQTT configuration retains the existing Configuration / Save & Restart flow.
Restarting while administrator setup is pending invalidates the old link and prints
a fresh 15-minute link. HTTP requests cannot refresh or regenerate it.

Existing valid administrator passwords, session secrets and API credentials are
preserved; existing installations receive no setup link. Malformed or ambiguous
authentication configuration fails closed instead of resetting credentials.
Authentication-disabled installations retain their existing behaviour.

Optionally set `webui.public_base_url` to the externally reachable HTTP/HTTPS origin
(for example `https://wamf.example` or `http://localhost:17767`). It must not contain
credentials, a query, a fragment or a path prefix. WAMF never derives it from request
headers. Without it, the printed link uses `127.0.0.1` and the configured web port;
replace the host and published port for access from another machine. Use HTTPS or
a trusted tunnel for remote setup. Subpath proxy deployments are not supported by
this setting. Configure secure session cookies when serving through HTTPS.

Login throttling uses the actual connection peer and ignores `X-Forwarded-For`,
`Forwarded` and `X-Real-IP`. WAMF does not currently configure trusted-proxy
address parsing; `public_base_url` only controls the printed setup link. Clients
behind the same reverse proxy or NAT therefore share its five-failure,
five-minute login bucket. Login attempts are held in memory, limited to 1,024
client buckets plus one shared overflow bucket. Active buckets are not evicted.
Untracked clients continue sharing active overflow state even if dedicated
capacity opens; dedicated allocation resumes only when the overflow bucket drains.
Password verification reserves an attempt atomically, then runs outside the
limiter lock. Pending attempts retain their slots until they finish, even beyond
five minutes; completed failures expire five minutes after verification finishes.
Success clears only failures already completed when that attempt was admitted;
concurrent failures and reservations remain counted. Overflow successes release
only their own reservation. Restarting the web process clears this in-memory state.

Configuration updates use a lock and mode-0600 atomic replacement. Writable Docker
directory mounts are supported; read-only configurations and single-file bind
mounts fail safely. Mount the configuration directory rather than the individual
file. Provisioning runs through `python speciesid.py`, not ordinary WSGI imports.
Run one native WAMF parent per configuration; setup deadlines use the existing
Linux/Python 3.11 fork-based worker model, not independent WSGI replicas.
Initial setup requires an explicit administrator mapping without YAML anchors;
ambiguous duplicate authentication keys are rejected. Existing valid anchored
credentials remain unchanged. Invalid setup-token attempts share a ten-per-minute
limit; they cannot block valid-token redemption. Failed attempts do not regenerate
or permanently invalidate the token.

The native default port is 7767; existing configured ports are preserved. The
config, bundled model, and taxonomy database resolve from the application
location. `WHOSATMYFEEDER_CONFIG` can select another existing file (relative paths
are relative to the caller's working directory); a missing override is an error.
`WAMF_SECRET_KEY`, when set to a valid value, continues to override the configured
session secret. Runtime storage directories and the SQLite schema are created
automatically. The runtime account needs write access for bootstrap and admin
configuration changes.

Wait for `MQTT event subscription ready` in the console before testing a new
bird event. This message follows the broker subscription acknowledgement.
MQTT retained deliveries are skipped explicitly. The first live `new` bird event
on the configured topic and camera is processed, including after a cold start.

Run tests:

```bash
pytest tests/ -v

```

---

# Development Notes

## Bridge events

When enabled, the Bridge integration emits events for newly committed
observations and for overall health state changes. Health events are sent only
when WAMF's background monitor detects a move between healthy, degraded, and
unhealthy states. The monitor runs every 60 seconds by default, including when
Bridge delivery is disabled. Delivery is best effort; Bridge outages never
interrupt WAMF processing or health checks.

## WAMF currently supports:

- Frigate-backed thumbnails
- Static development thumbnails
- Fake detection seeding for UI testing
- SQLite development workflows

This allows rapid UI and analytics development without requiring a live Frigate deployment during testing.

---

# Roadmap

## Planned and experimental observatory features include:

- Real-time Frigate event streaming
- Species heatmaps
- Dawn/dusk activity overlays
- Seasonal behaviour analysis
- Weather integration
- Multi-camera observatories
- Notification and alerting systems
- Long-term wildlife trend analysis

---

# Attribution

Original project:

https://github.com/mmcc-xx/WhosAtMyFeeder

Intermediate fork:

https://github.com/k1n6b0b/whosatmyfeeder

This repository continues to build upon both projects while evolving toward a broader wildlife observatory platform.

## License

This project is licensed under the MIT License.  
See the LICENSE file for details.
