# WAMF v0.4.3 security maintenance release (candidate)

Pending release review and publication. Prepared from PR #4's merge into main
(`f397eea`). Retains Python 3.11, the CPU TFLite classifier, application dependency
pins and AMD64 deployment scope.

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

The local-build Compose example names `wamf:0.4.3`. The registry image
`ghcr.io/wamf-project/wamf:0.4.3` is usable only after publication; the existing
registry Compose file's `latest` tag is not a guarantee of this version.

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

Post-merge pytest and CodeQL checks passed for PR #4. Local release-preparation
verification on 2026-10-28 passed all 1,860 tests with `.venv311` (Python 3.11),
including browser tests using the existing Playwright and verified CDN assets;
there were no skips. Version metadata agrees, dependency declarations are
unchanged, and `git diff --check` passes. No v0.4.3 image build, runtime smoke
test or image-security scan has been performed during this preparation.

Review the retained risks and validate any release image before publication.
The tag-triggered publishing workflow derives the version from the new tag;
manual workflow dispatch remains intentionally pinned to the immutable v0.4.2
source and must not be used to publish v0.4.3. No tag, release or image has been
created or published by preparing these files.
