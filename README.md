#  WAMF (Wildlife Activity Monitoring Framework)

> A wildlife observatory platform built around Frigate NVR, bird species identification, feeder activity analytics, and live wildlife monitoring.

---

## Project Background

WAMF is a personal fork of:

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

TO BE ADDED SOON

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

/whosatmyfeeder/
├── docker-compose.yml
├── config/
│   └── config.yml
└── data/

```

---

# Configuration

Copy:

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
  port: 7766
  host: 0.0.0.0
```

# Docker Compose

```yaml
version: "3.6"

services:
  whosatmyfeeder:
    container_name: whosatmyfeeder

    restart: unless-stopped

    image: ghcr.io/k1n6b0b/whosatmyfeeder:latest

    volumes:
      - ./config:/config
      - ./data:/data

    ports:
      - 7766:7766

    environment:
      - TZ=Europe/London
```

# Run

```bash

docker compose up -d

```

The observatory UI will be available at:

```text

http://<server-ip>:7766

```

---

# Development

```bash

python3 -m venv .venv

source .venv/bin/activate

pip install -r requirements/base.txt

```

Run Flask:

```bash

python webui.py

```

Run tests:

```bash
pytest tests/ -v

```

---

# Development Notes

## Bridge events

When enabled, the Bridge integration emits events for newly committed
observations and for overall health state changes. Health events are sent only
when WAMF moves between healthy, degraded, and unhealthy states. Delivery is
best effort; Bridge outages never interrupt WAMF processing or health checks.

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
