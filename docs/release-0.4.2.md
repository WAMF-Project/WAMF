# WAMF v0.4.2 maintenance release

Published as [v0.4.2](https://github.com/WAMF-Project/WAMF/releases/tag/v0.4.2)
on 2026-10-27. Originally planned as the final v0.4 maintenance release;
superseded by [v0.4.3](release-0.4.3.md) on 2026-10-28.
Retains Python 3.11, the existing CPU TFLite classifier and AMD64
deployment scope. No Kingfisher features or unrelated improvements are included.

## Security fixes

- **F-001:** render detection values as DOM/text data rather than executable HTML
  or inline handlers; validate Frigate event identifiers before ingestion,
  requests, publication and archiving.
- **F-004:** ignore spoofable forwarding headers for login throttling. Reserve
  attempts before password verification and retain concurrent failures and active
  overflow limits when dedicated capacity changes.
- Harden Frigate media proxy paths, redirects, streaming cleanup and forwarded
  headers; validate internal login destinations and bounded Wikipedia redirects.
- Keep YAML source excerpts, credential-bearing exception text and unescaped
  control characters out of the corrected responses and diagnostic logs.

## First-run setup and interface

Fresh authentication-enabled installations print a private, single-use
`/setup#token=...` link instead of a temporary administrator password. Choose a
password within 15 minutes. Restarting pending setup invalidates the old link;
HTTP requests cannot renew it. Existing valid administrator/API credentials and
session secrets are preserved; malformed authentication configuration fails closed.

Operational workers pause until administrator setup completes and resume when
Frigate configuration is ready. Configuration updates use a lock and mode-0600
atomic replacement. Docker requires a writable configuration-directory mount.
The mobile navigation hamburger and collapse controls are restored.

## Dependencies, Docker and browser assets

pytest is updated to 9.0.3. This release retains the constrained Python 3.11
runtime and Docker hardening from v0.4.1: reviewed base-image digest, build-time
Debian updates, dependency checking, removal of unused multimedia/OpenCV
components, and removal of pip, setuptools, wheel, packaging and ensurepip from
the merged runtime filesystem. These are carried-forward measures, not newly
verified v0.4.2 image results; removal does not erase inherited image layers.

**F-005:** Monaco Editor 0.52.2 is served locally, including its workers, with
the original MIT licence, third-party notices and checksum/provenance manifest.
The remaining external Bootstrap and jQuery tags use SHA-384 SRI and anonymous
CORS. See [asset integrity details](cdn-integrity.md). Integrity does not provide
CDN availability or certify approved library versions as vulnerability-free.

## Upgrade and retained risks

Stop WAMF cleanly and back up configuration, database and media together. Retain
the previous image and reuse existing mounts, ports and session-secret settings.
Existing provisioned v0.4.0/v0.4.1 installations require no data-format migration
or credential reset. Check login, API access, media, MQTT detection and restart
persistence after upgrading. See [Docker upgrade instructions](docker-testing.md#upgrading-v040-or-v041-to-v042).

- **Protobuf:** retain the documented compatibility deferral for protobuf 3.20.3
  and CVE-2025-4565/CVE-2026-0994. TFLite Support 0.4.4 requires protobuf below 4;
  the trusted bundled-model boundary does not remove those vulnerabilities.
  The existing [risk decision](release-0.4.1.md#known-security-limitations) remains
  unchanged; no isolated major dependency upgrade is included.
- **F-006:** retain the scoped [local packaging-tool risk acceptance](security-0.4.2-f006.md),
  finalized for v0.4.2 on 2026-10-27. Local pip/setuptools advisories remain relevant
  to package-management operations; no normal WAMF runtime path was identified.
- **F-007:** retain the conditional [setup-token risk acceptance](security-0.4.2-f007.md),
  finalized for v0.4.2 on 2026-10-27. Anyone with the unused startup link and network
  access can set the administrator password. Restrict startup/centralized logs to
  trusted administrators, disable setup request-body capture, and use HTTPS or
  a trusted tunnel for remote setup. The printed loopback URL is not an access
  control.
- Existing root-container/Flask runtime, legacy credential compatibility and
  Debian/native-library limitations remain; this is not a vulnerability-free
  release claim. Historical image findings are documented in the
  [v0.4.1 security limitations](release-0.4.1.md#known-security-limitations).

## Validation and release conditions

The following is the historical release-preparation record, retained to document
its validation scope and the conditions outstanding at that time. It does not
describe the current publication status or incorporate later v0.4.3 validation.

The consolidated pre-metadata audit passed 1,344 full-suite tests and 1,220
overlapping targeted security tests using `.venv311`, with no failures, skips or
warnings reported. It found no new actionable vulnerability or failing security
regression. Those results describe the audited source tree, not a newly built
v0.4.2 container.

Final release review must retain the F-006/F-007 acceptance conditions and include
the necessary currently untracked source, assets, tests and records in the release
artifact. Current-tree Docker build/runtime and image-security verification remain
outstanding. Registry tag `0.4.2` is usable only after publication; preparing these
notes and version metadata does not publish or approve the release.
