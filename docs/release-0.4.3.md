# WAMF v0.4.3 security maintenance release

[v0.4.3](https://github.com/WAMF-Project/WAMF/releases/tag/v0.4.3) was published
on 2026-10-28 and is the latest stable release. The release tag points to
`eb6d298357fd481f4c82257adc41c211d6e6fa1c`, incorporating the security fixes in
PR #4 and release preparation in PR #5. Retains Python 3.11, the CPU TFLite
classifier, application dependency pins and AMD64 deployment scope.

## Changes

- **Embedded navigation (CodeQL #15):** parse link destinations with the browser
  URL API and allow HTTP/HTTPS only. Remove unsafe or malformed destinations so
  native clicks cannot execute them. Preserve relative/internal links, fragments,
  external HTTP/HTTPS links and Home Assistant `embed=ha` navigation.
- **Security regression tests:** cover rejected and permitted URL formats,
  daily-summary/date navigation, login redirects, embed mode and browser history.
- **Chromium CI reliability:** install pinned Playwright 1.63.0's Chromium
  headless shell and prefer it in the affected command-line browser tests.
  Security assertions and timeouts are preserved.
- **Test HTML parsing (CodeQL #16–#18):** use Python's standard-library HTMLParser
  instead of regex extraction, with tests for tag/attribute case variations,
  comments, quoted delimiters and unchanged inline JavaScript text.

## Upgrade

Stop WAMF cleanly, back up configuration/database/media together and retain the
previous image or checkout for rollback. Preserve existing mounts, ports,
credentials and session secrets. No configuration, database or media migration
is introduced. See [Docker upgrade guidance](docker-testing.md#upgrading-v042-to-v043).

The local-build Compose example names `wamf:0.4.3`. The published registry image
is `ghcr.io/wamf-project/wamf:0.4.3`. Select that explicit version in your
deployment's Compose file. The supplied registry Compose file uses `latest`,
which the release publishing workflow does not update.

## Retained security risks

The release review found no open GitHub CodeQL, Dependabot or secret-scanning
alerts on 2026-10-28. CodeQL #15 is closed. This does not establish that the
release or its dependencies are vulnerability-free:

- **Protobuf 3.20.3:** CVE-2025-4565 and CVE-2026-0994 remain. GitHub records their
  alerts as dismissed for tolerable risk, not patched. TFLite Support 0.4.4's
  protobuf `<4` constraint prevents an isolated compatible upgrade. Retain the
  [existing compatibility risk decision](release-0.4.1.md#known-security-limitations).
- Retain the scoped [packaging-tool guidance](security-0.4.2-f006.md) and
  conditional [setup-token operational requirements](security-0.4.2-f007.md).
  Restrict startup-log access, disable setup request-body capture, and use HTTPS
  or a trusted tunnel for remote setup.
- Existing root-container/Flask runtime and Debian/native-library limitations
  remain. Historical image scans do not validate a new v0.4.3 image.

## Validation and publication

### Release-preparation record

Post-merge pytest and CodeQL checks passed for PR #4. Local release-preparation
verification on 2026-10-28 passed all 1,860 tests with `.venv311` (Python 3.11),
including browser tests using the existing Playwright and verified CDN assets;
there were no skips. Version metadata agreed, dependency declarations were
unchanged, and `git diff --check` passed. That preparation step did not build,
smoke-test, scan or publish a v0.4.3 image.

### Release validation and publication

The [published release record](https://github.com/WAMF-Project/WAMF/releases/tag/v0.4.3)
records a successful isolated Linux/AMD64 candidate build and smoke test:
OCI labels, runtime, Admin UI and health API reported version `0.4.3`; the web
interface and authenticated health endpoint returned HTTP 200, while
unauthenticated health access returned HTTP 401. Database and archive storage
health checks passed with no unexpected startup errors. The isolated smoke test
did not exercise live Frigate or MQTT connectivity and did not establish a
separate comprehensive image-security scan.

All release PR and post-merge pytest and CodeQL checks passed. The
[tag-triggered Docker publishing run](https://github.com/WAMF-Project/WAMF/actions/runs/40775804843)
for source `eb6d298357fd481f4c82257adc41c211d6e6fa1c` successfully built the image,
ran the configured Trivy scan and published the versioned image tags. The scan
gate covered CRITICAL severity with `ignore-unfixed: true`; passing it does not
establish that lower-severity or unfixed vulnerabilities are absent. Retained
security risks above remain applicable.

The tag and GitHub release are published as `v0.4.3`. The publishing workflow
derives image versions from the release tag and does not update `latest`.
Manual workflow dispatch remains intentionally pinned to the immutable v0.4.2
source and must not be used to publish v0.4.3.
