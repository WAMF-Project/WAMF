# Changelog

All notable changes to WAMF will be documented in this file.

This project follows the principles of Keep a Changelog and uses semantic
versioning for releases.

## [Unreleased]

### Added

### Changed

### Fixed
---
## [0.5.0 - "Kingfisher" ] - Unreleased

### Changed

- Migrated the species classifier from the deprecated `tflite-support` runtime to LiteRT (`ai-edge-litert`), enabling WAMF to run natively on Python 3.12.
- Updated classifier model loading and inference while preserving the existing model, labels, confidence thresholds, and species-name database mappings.
- Corrected classifier image preprocessing to resize Frigate object snapshots directly to the model's required 224×224 input instead of preserving aspect ratio with black letterboxing.

### Fixed

- Fixed a long-standing image preprocessing issue where black padding could dominate the classifier input and cause valid bird detections to be classified as `__background__`.
- Added regression coverage for the complete event preprocessing path to prevent letterboxed classifier input from being reintroduced.

---

## [0.4.0] - 2026-09-18

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