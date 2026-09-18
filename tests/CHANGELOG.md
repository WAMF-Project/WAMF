# Changelog

All notable changes to WAMF will be documented in this file.

This project follows the principles of Keep a Changelog and uses semantic
versioning for releases.

## [Unreleased]

### Added

### Changed

### Fixed

---

## [0.4.0] - Unreleased

### Added

- Docker deployment support for WAMF.
- First-run bootstrap and setup mode.
- Automatic generation of initial administrator credentials.
- Admin configuration interface.
- Controlled application restart from the Admin UI.
- Persistent configuration, database, snapshots, and clips in Docker deployments.
- WAMF health monitoring and system health reporting.
- Proactive Bridge health monitoring.
- Optional Bridge observation and health-state events.
- Frigate snapshot and clip archiving.
- Frigate sublabel updates following successful species classification.
- Docker packaging regression tests.

### Changed

- Default WAMF web interface port is now 7767.
- Application startup now validates required external configuration before
  starting MQTT and classification workers.
- Application paths are resolved consistently for native and Docker deployments.
- Shutdown and restart handling now coordinates child processes before exit or
  re-execution.
- Docker configuration now uses persistent `/app/config`, `/app/data`, and
  `/app/media` paths.
- Docker runtime targets Python 3.11.
- Deployment documentation updated for native and Docker installations.

### Fixed

- Foreground and systemd restart handling.
- MQTT connection readiness reporting.
- Retained MQTT messages being incorrectly treated as new detections.
- Docker images missing runtime modules and integration resources.
- Docker port mismatch between the application and deployment configuration.
- Docker bind mounts not matching WAMF's resolved storage paths.
- First-run Docker configuration being hidden by an empty bind mount.
- Test dependency installation drift.

### Compatibility

- Verified with Frigate 0.17.2.
- Verified with Frigate 0.18.0.
- Verified native installation on Ubuntu 24.04.
- Verified Docker cold bootstrap, live detection processing, media archiving,
  Admin restart, and persistence across container recreation.

---

## Earlier development

WAMF originated from the Who's At My Feeder project. Releases prior to the
current WAMF release series predate this changelog and are not reconstructed
here.