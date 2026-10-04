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

### Added

- Added species profiles with cached descriptions and reference images, first/last sightings, detection totals, active days, camera counts, and hourly activity. Species links throughout the interface now lead to these profiles.
- Added paginated Recent Feed and all-history species detection views for browsing older observations.
- Added historical Activity navigation and previous/next recorded-day navigation in Activity and Daily Summary, with hourly charts, species summaries, and links to matching detections.
- Added a native retention service for expired snapshots and clips, orphan detection and optional deletion, missing-media diagnostics, and system-event pruning while preserving a configurable minimum number of recent events. Media deletion remains opt-in, with report-only modes for expired and orphaned media.
- Added durable retention status recording the latest completed attempt, last successful completion, trigger, timing, phase outcomes, cleanup counts, and bounded, redacted diagnostics. Legacy orphan-scan status is preserved through a transactional, idempotent database migration.
- Added optional native daily retention scheduling with a configurable local time and IANA timezone. Scheduling is disabled by default, skips nonexistent DST times, runs once at the earlier occurrence of repeated times, and does not replay schedules missed while WAMF was offline. Schedule changes require a restart.
- Added Admin Settings retention controls for global snapshot/clip defaults, cleanup and orphan handling, system-event retention, and automatic scheduling. Species overrides use cached Species Metadata for selection, match scientific names case-insensitively, and let snapshots and clips independently inherit their global defaults. Timezone suggestions support geographic zones and UTC while accepting any valid IANA zone.
- Added an accessible, dismissible Settings save-confirmation toast that preserves the current section.
- Added developer Makefile targets for systemd service management, journal viewing, tests, and validation using `.venv312`.

### Changed

- Migrated the species classifier from the deprecated `tflite-support` runtime to LiteRT (`ai-edge-litert`), enabling WAMF to run natively on Python 3.12.
- Updated classifier model loading and inference while preserving the existing model, labels, confidence thresholds, and species-name database mappings.
- Corrected classifier image preprocessing to resize Frigate object snapshots directly to the model's required 224×224 input instead of preserving aspect ratio with black letterboxing.
- Replaced the legacy Home Assistant presentation with a shared responsive WAMF interface, desktop sidebar, and keyboard-accessible mobile navigation.
- Redesigned Overview, Recent Feed, Daily Summary, Activity, and detection results with consistent cards, confidence and camera details, hourly activity displays, and clearer empty states.
- Replaced the Admin YAML editor with structured Settings sections for General, Storage & Retention, MQTT, Frigate, Classification, Live View, Bridge / Perch, and Admin & API. Added field-level validation, contextual help, dependent controls, and persistent Save/Restart actions while retaining the existing JSON configuration endpoints and partial Settings POST semantics.
- Reorganized Administration navigation around Settings, Species Metadata, System Health, and Logs. Improved health/archive summaries, metadata refresh controls, and log filtering, and moved the service-status summary into the authenticated Admin sidebar.
- Introduced versioned configuration migration to `config_version: 2`, moving legacy Frigate MQTT settings into the dedicated `mqtt` section while preserving explicitly configured canonical values. MQTT runtime consumers now use the canonical settings; configuration normalization also resolves Bridge / Perch, health interval, and Live View aliases consistently.
- Centralized startup and Admin configuration validation, distinguishing invalid settings from incomplete setup. Structurally valid incomplete settings can be saved, while restart actions reject configurations that are not ready to run detection.
- Separated MQTT credentials, Admin password/session secrets, and API token hashes into `config/secrets.yml`, with automatic migration and owner-only configuration, secrets, locks, and backups. Credentials are saved before removal from ordinary configuration; migration backups may retain the previous embedded credentials until normal backup pruning removes them.
- Improved configuration persistence with process-safe locking, atomic replacement, durable writes, and retained backups shared by bootstrap, Settings, password changes, and API token updates.
- Standardized Frigate HTTP access and completed the transition to WAMF-owned archived media in the interface, removing the legacy `/frigate/...` media proxy routes and closing streamed clip responses reliably.
- Made the retention scheduler a dedicated WAMF Core supervisor-owned process, with recovery after unexpected exits, capped restart backoff, and bounded shutdown/restart handling. Local scheduled cleanup can run while incomplete Frigate/MQTT setup disables detection. Added APScheduler and bundled timezone data for scheduling; one Core supervisor per database is the supported deployment model.

### Fixed

- Fixed a long-standing image preprocessing issue where black padding could dominate the classifier input and cause valid bird detections to be classified as `__background__`.
- Added regression coverage for the complete event preprocessing path to prevent letterboxed classifier input from being reintroduced.
- Fixed date-specific species activity charts using all-history data, Activity species counts being limited to the top-species list, and invalid or future Activity/Daily Summary dates producing misleading results.
- Fixed MQTT health checks ignoring configured authentication and TLS settings by sharing connection settings with the detection worker.
- Prevented overlapping retention executions and coordinated cleanup scans with archive writes through database commit, protecting in-flight media and newly recorded references. Cleanup enforces archive-root containment, including symlink targets, and preserves detection history when media expires.
- Improved retention failure handling so one failed phase does not prevent independent cleanup phases from running, partial failures remain visible in diagnostics, and unsuccessful attempts preserve the last-success timestamp.
- Preserved omitted Settings fields, unknown configuration keys, existing species overrides, and blank MQTT credentials during saves. Stored secrets are not rendered back into Settings, and Settings remains usable when invalid configuration prevents the sidebar health summary from loading.
- Added regression coverage for configuration migration and persistence failures, secrets preservation, historical navigation, archive/retention coordination, durable status, DST scheduling, and supervisor shutdown/restart behaviour.

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
