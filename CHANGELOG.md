# Changelog

All notable changes to WAMF will be documented in this file.

This project follows the principles of Keep a Changelog and uses semantic
versioning for releases.

## [Unreleased]

### Added

### Changed

### Fixed

---

## [0.4.3] - Pending release

Security maintenance release for existing WAMF v0.4.x installations.

### Security

- Validate embedded-navigation URLs with the browser URL API and permit only
  HTTP/HTTPS destinations. Remove unsafe or malformed link destinations while
  preserving internal, fragment, external HTTP/HTTPS and Home Assistant navigation.

### Testing and CI

- Add security regression coverage for URL formats, daily-summary navigation,
  login redirects and browser history, including Home Assistant embed parameters.
- Install a pinned Chromium headless shell in CI and prefer it in the affected
  browser tests, retaining security assertions and existing timeouts.
- Replace regex-based HTML extraction with standard-library HTMLParser helpers
  and case-variation tests, resolving the test-only CodeQL findings.

### Compatibility

- Retain Python 3.11 and all application dependency pins, configuration, database
  and media formats. See [v0.4.3 release notes](docs/release-0.4.3.md) for upgrade
  guidance, retained security risks and publication requirements.

---

## [0.4.2] - Pending release

Final planned v0.4 maintenance release.

### Security

- Prevent stored DOM XSS in detection cards and validate Frigate event identifiers
  before ingestion, media requests and archiving.
- Prevent forwarded-header login rate-limit bypasses and preserve limits across
  concurrent attempts and overflow-bucket transitions.
- Harden Frigate media proxying, login redirects, Wikipedia summary URLs and
  YAML/exception/log diagnostics.
- Serve Monaco Editor 0.52.2 locally with licence/provenance records; add SHA-384
  integrity checks to the remaining external Bootstrap and jQuery assets.

### Setup and interface

- Replace first-run temporary-password output with a private, single-use setup
  link that expires after 15 minutes. Preserve existing valid credentials.
- Pause operational workers until administrator setup completes; use locked,
  mode-0600 atomic configuration replacement.
- Restore the mobile navigation hamburger and collapse controls.

### Dependencies and operations

- Update pytest to 9.0.3; retain the constrained Python 3.11 runtime and existing
  Docker base, dependency checks and packaging-tool removal.
- Carry forward the documented protobuf compatibility deferral and scoped F-006
  packaging-tool/F-007 setup-token risk acceptances, including their conditions
  and limitations. Final F-006/F-007 acceptance was recorded on 2026-10-27.
- See [v0.4.2 release notes](docs/release-0.4.2.md) for upgrade guidance and
  remaining release conditions. Current-tree Docker build/runtime verification
  remains outstanding.

---

## [0.4.1] - 2026-10-26

Security maintenance release for existing WAMF 0.4.0 installations.

### Security

- Update Flask, Werkzeug, Requests, Pillow, certifi and urllib3, and constrain
  all Python runtime dependencies for Python 3.11.
- Pin the Docker base to the reviewed Python 3.11.17 slim-trixie image digest
  and apply available Debian package updates during the build.
- Remove unused system FFmpeg, multimedia/graphics dependencies and OpenCV.
- Remove pip, setuptools, wheel, packaging and ensurepip from the final runtime
  image, retaining application dependencies and their required metadata.

### Fixed

- Restore verification of historical Werkzeug password hashes and API-token
  wrappers with an explicit compatibility allowlist and constant-time comparisons.
- Prevent unsupported legacy API-token hash methods from causing HTTP 500 errors.
- Preserve existing password hashes, API-token hashes and session secrets during
  startup. Reject malformed or unsupported configured hashes without replacing
  credentials, while retaining first-run provisioning for fresh installations.
- Preserve the historical 600,000-iteration default when verifying shortened
  PBKDF2-SHA256 descriptors. Verification does not automatically rehash credentials.

### Compatibility and operations

- Retain Python 3.11, NumPy 1.26.4, TFLite Support 0.4.4, protobuf 3.20.3 and
  Paho MQTT 1.6.1. The existing TFLite classifier remains in use.
- Preserve configuration, database and media formats, volume conventions,
  sessions, CSRF handling and first-run behaviour. No migration is required
  for the tested v0.4.0 upgrade and rollback paths.
- Reduce the local amd64 image from approximately 1.52 GB before runtime
  reduction (Stage B) to 621 MB. The published v0.4.0 baseline was 1.505 GB.
  Exact candidate measurements are in the release validation report.

### Known security limitations

- protobuf CVE-2025-4565 and CVE-2026-0994 remain because upstream fixes conflict
  with TFLite Support 0.4.4's protobuf constraint.
- Debian scanner matches remain without reported fixed versions. Stage F and G1
  had no critical matches; this does not make the image vulnerability-free.
- See [v0.4.1 release notes](docs/release-0.4.1.md) for upgrade guidance,
  remaining risks and links to the final candidate results.

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
