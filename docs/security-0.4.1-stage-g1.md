# WAMF v0.4.1 Stage G1: release preparation and final verification

Validated locally on 2026-10-26 in `/home/ian/WAMF-0.4.1`, branch
`maintenance/0.4.1`, using the dedicated `.venv311/bin/python` (Python 3.11.17).

**Recommendation: READY for Stage G2 release approval.** No release blocker was
found. The residual security findings below still require release review. This
report does not authorise publication or execution of Stage G2.

## 1. Complete maintenance diff review

Compared the working tree, including untracked implementation, fixture and
documentation files, against the published `v0.4.0` tag, which peels to commit
`2f3b5ae99ea2c31b26cc5c2cb9cd4e9e26ede153`. HEAD remains that commit; all
maintenance work is uncommitted.

| Area | Reviewed changes | Assessment |
| --- | --- | --- |
| Dependencies | Runtime pins/constraints, frozen and working snapshots, test and UI manifests | Security updates, consistency and removal of unused OpenCV |
| Docker | Reviewed Python 3.11/trixie digest; Debian upgrades; libusb retained; FFmpeg/graphics stack and packaging tools removed | Maintenance scope; runtime contract preserved |
| Authentication | Shared verifier, bootstrap validation, login/API/current-password callers | Required compatibility fixes for the Werkzeug update; no automatic rehash |
| Tests | Packaging expectations, bootstrap preservation, original-image credential vectors, removal of stale cv2 mocks | Direct regression coverage; no feature work |
| Evidence | Stage B/D/E/F security reports, inventories and synthetic upgrade results | Historical evidence retained, including Stage E failures later resolved by F |
| G1 metadata/docs | Version, local Compose build tag, image label, changelog and upgrade/release guidance | Release preparation only |

No unrelated feature change, unexpected runtime dependency, model change,
database/schema change, static asset change or persistent-data format change was
found. No questionable file was removed. `requirements/working.txt` retains
its pre-existing development-only `packaging==26.2` entry; it is not installed
by the runtime manifest. Docker uses packaging 26.3 during building and removes
it from the final runtime. These are distinct uses, not runtime version drift.

## 2. Files changed during G1

- `version.py`: application version 0.4.0 → 0.4.1.
- `Dockerfile`: add `org.opencontainers.image.version="0.4.1"`; no further
  dependency, runtime, port, mount, user or entrypoint change during G1.
- `docker-compose.yml.example`: local-build image name `wamf:0.4.1`.
- `CHANGELOG.md`: pending v0.4.1 security maintenance entry.
- `README.md`: link the v0.4.1 release/upgrade guidance.
- `docs/docker-testing.md`: narrowly scoped v0.4.0 upgrade/rollback instructions.
- `docs/release-0.4.1.md`: candidate release notes and security limitations.
- `docs/security-0.4.1-stage-g1.md`: this report.
- `docs/security-0.4.1-stage-g1-results.json`: final image, package inventories,
  scan summary and sanitised validation results.

All earlier maintenance changes were preserved. G1 made no authentication
implementation changes and no package upgrades. No additional tests were added
for the metadata-only changes; existing full suites and direct container version
checks validated them.

## 3. Version consistency

`version.py` supplies the UI context, admin health API and startup audit message.
All three report 0.4.1 in rc1, and the Docker OCI version label is 0.4.1.
Original/rollback containers report 0.4.0 in the same HTTP/UI/audit checks.

There is no separate WAMF Python package version manifest in this checkout.
Templates consume the shared version rather than carrying another literal.
The local-build Compose example now names 0.4.1 and passed `docker compose
config --quiet`. The registry Compose file retains its existing `latest` policy;
no unpublished registry tag is assumed available. Historical changelog, source
comments and Stage B–F references to v0.4.0 were preserved.

## 4. Dependency and Docker reproducibility

- All **24 runtime distributions** match `requirements/constraints.txt` and
  `requirements/frozen.txt` exactly. The working snapshot has the same runtime
  pins plus its development-only packaging entry. Base, test, UI and top-level
  manifests apply the intended constraints through their include chain.
- Dedicated Python 3.11 host `pip check`: **passed**.
- Fresh Docker resolution/install and build-time `pip check`: **passed**.
- After packaging-tool removal, every active installed `Requires-Dist` was
  checked against the image's actual metadata: **passed**. Pip is intentionally
  absent at runtime, so no runtime `pip check` invocation is claimed.
- Python **3.11.17**, NumPy **1.26.4**, TFLite Support **0.4.4**, protobuf
  **3.20.3** and Paho MQTT **1.6.1** are unchanged.
- Image checks confirmed no cv2/OpenCV files, FFmpeg executable, pip/setuptools/
  wheel/packaging modules or executables, vendored tool directories, ensurepip
  or bootstrap wheel payloads in the merged runtime filesystem. Required
  runtime distributions, metadata, pybind11 and libusb remain.
- The trusted bundled model and all application payload files are identical to
  Stage F except `/app/version.py`. Native inference and shared-library checks
  passed. Image entrypoint, CMD, user, environment, ports and working directory
  also match Stage F.

The clean rebuild independently reproduced the exact runtime Python and Debian
package inventories. This is **version reproducibility**, not a claim of identical
image bytes: PyPI artifacts are not hash-locked; Paho MQTT builds a wheel using
isolated build dependencies; Debian uses live trixie repositories with `apt-get
upgrade`. Isolated build dependencies are not frozen by runtime constraints.
Future builds must be inventoried and rescanned. Removed tools can remain in
inherited/deleted image layers even though they are absent from the runtime view.

## 5. Final local image

| Property | Result |
| --- | --- |
| Local tag | `wamf:0.4.1-rc1` |
| Image ID | `sha256:3bd3e6712ee3932d4b2d49cc23681fc22c8201989049e0dfba1231bc3233a28e` |
| Build | `docker build --no-cache --pull -t wamf:0.4.1-rc1 .` |
| Platform | Linux amd64 |
| Size | **621,275,926 bytes** (621.276 MB decimal; Docker-reported image size) |
| Python / Debian | 3.11.17 / 13.7 (trixie) |
| Runtime inventories | 24 Python distributions; 88 Debian packages |
| Base | `python:3.11.17-slim-trixie` |
| Base index digest | `sha256:b7a9e5f6a93d6d4a31f129bbd4bdee69efccb6ded0fb230d78aafaf37af98377` |
| Reviewed amd64 base manifest | `sha256:b95327ce1439e687237841b0210677aeadd62d55a060a5f6bcb9c15bf6251dd3` |
| Stage F comparison | Same package versions and application payload except version; new version label; reported size 11 bytes smaller |

The small size difference was investigated with file inventories and hashes.
Compared `/app`, `/usr/local`, `/etc`, `/var/lib/dpkg` and `/var/log` trees
contained no added/removed package content or source change beyond the version
constant. Other content differences were regenerated bytecode and apt/dpkg logs;
the compared files retained their sizes. There is no unexpected dependency upgrade.

For context, published v0.4.0 was 1,505,089,400 bytes; Stage B was
1,515,506,378 bytes (approximately 1.52 GB). These figures are image sizes, not
compressed downloads or exclusive disk usage. Complete versioned inventories
are in the accompanying G1 JSON results.

## 6. Final regression validation

| Check performed during G1 | Result |
| --- | --- |
| Complete dedicated host Python 3.11 suite | **411 passed; 0 failed; 0 skipped** |
| Complete suite with rc1 runtime dependencies | **406 passed; 0 failed; 5 skipped** |
| Five in-container skips | Node-dependent browser restart cases; all five passed on host |
| Fresh provisioning and setup-mode startup | PASS; temporary password, config mode 0600, HTTP 7767 |
| Login, CSRF, config save/backups, restart and sessions | PASS |
| Media, SQLite writes, recreation persistence, clean shutdown | PASS |
| Malformed configured admin/API credentials and secret | Five cases rejected twice; no file changes or secret disclosure |
| Real bundled-model inference and Pillow/NumPy preprocessing | PASS for JPEG, PNG and WebP; exact predictions match Stage D baseline |
| Native extension linkage | PASS; no missing shared libraries |
| Compose example and whitespace validation | PASS |

Container pytest used read-only test source/tooling snapshots and temporary
writable test directories; application dependency imports used rc1's installed
runtime packages. Tests include correct/incorrect credentials, Unicode, all
allowlisted historical methods, modern methods, shortened PBKDF2 descriptors,
API-only wrappers, no modern fallback, current-password verification, rate
limiting, CSRF, session handling, bootstrap preservation and malformed formats.
Fixtures contain only synthetic credentials generated by the original image.

## 7. Upgrade/rollback and reused evidence

G1 reran two complete original v0.4.0 → rc1 → original v0.4.0 cycles:

| Installation | Checks | Result |
| --- | --- | --- |
| Current paths, PBKDF2/scrypt | Existing credentials; explicit password/token rotation on rc1; rollback accepts replacements | PASS |
| Current paths, legacy SHA256 admin/API | Original credentials preserved and usable in both directions | PASS |

All **six phases** passed fresh and inherited sessions, signed CSRF tokens,
invalid and valid login/API requests, admin pages, snapshots/clips, HTTP range
requests, Frigate proxy, logout, config saves, Admin Restart, real MQTT-triggered
model inference and clean shutdown (exit 0). UI/API/startup versions were checked
at each phase.

At startup, read-only authentication and shutdown boundaries, config, credentials,
backups and media remained byte-for-byte unchanged, including permissions and
mtime. Business records and schema were unchanged. Expected activity appended
lifecycle audit events, operator-requested saves/backups/rotations and one new
synthetic detection per phase. No migration or credential loss occurred.

The wider Stage F matrix already passed legacy `/data` and `/media` aliases,
environment-provided session secrets, SHA1 and MD5 upgrade/rollback cases. Those
expensive scenarios were not repeated during G1: payload hashing confirmed
identical authentication, database, media and startup code, and all package
versions are unchanged. Their automated credential vectors were rerun in both
full G1 suites. See [Stage F](security-0.4.1-stage-f.md) for those confirmed results.

All G1 integration data and credentials were synthetic, in uniquely named
disposable volumes and an internal network without publicly exposed ports.
All test containers, volumes and networks were cleaned up. No real installation
was mounted. Published v0.4.0 and Stage F image IDs remain unchanged.

## 8. Remaining security findings

Trivy **0.76.1** scanned rc1 at all severities, including unfixed findings, without
an ignore list. Database update: `2026-10-26T07:15:18.945937093Z`.
The complete package/advisory/severity/status/fixed-version signatures match
Stage F, rather than merely matching its total count.

| Target | Matches | Unique advisory IDs | Severity | Fixed versions reported |
| --- | ---: | ---: | --- | ---: |
| Debian | 166 | 73 | 44 high, 60 medium, 61 low, 1 unknown | 0 |
| Python | 2 | 2 | 2 high | 2, incompatible with retained TFLite stack |

**No critical scanner matches.** This does not establish that the image is
vulnerability-free or that all native components have complete scanner coverage.

The two protobuf CVEs, **CVE-2025-4565** and **CVE-2026-0994**, remain present
as dependency findings. Fixes require versions outside TFLite Support 0.4.4's
protobuf `<4` constraint. Trusted bundled-model use limits the relevant exposure
but does not remove the dependency vulnerability.

Debian matches include duplicate advisory IDs across binary packages and retain
Stage D's absent-component/unused-utility applicability qualifications. No
reported fixed version does not mean every match is exploitable or that no
upstream fix exists elsewhere. Libxml2/system multimedia and packaging-vendor
matches removed in Stage D have not reappeared. OpenCV's bundled native libraries
remain absent; remaining TFLite/Pillow native code still has coverage limitations.

Legacy credential formats remain weaker formats accepted for compatibility;
generation remains modern and explicit operator rotation is available. The
existing root user and Flask server are unchanged. The publication workflow
still scans only fixable critical findings and does not enforce review of every
residual advisory. G1 did not modify or execute that workflow.

## 9. Documentation review

The changelog and candidate release notes distinguish updated/removed components
from remaining protobuf and Debian findings. They describe credential work as
compatibility repair, retain first-run provisioning and make no vulnerability-free
claim. README and Docker guidance link these notes and explain same-volume
upgrade/rollback, backup after clean shutdown, preservation of session secrets and
legacy aliases, and fail-closed malformed configuration.

Existing installation instructions, historical release notes, registry `latest`
policy and deployment conventions otherwise remain unchanged. Registry tag
0.4.1 is not represented as already published. Documentation changes are limited
to this maintenance release.

## 10. Limits and release recommendation

Deployment scope clarification for release preparation: WAMF v0.4.1 targets
AMD64 deployments. ARM architectures are not currently supported deployment
targets, and no ARM validation was performed. WAMF does not directly use Coral
acceleration; its classifier sets `use_coral=False`. Frigate may independently
use Coral for object detection. This documents the existing functionality.

**No unresolved release blocker was found. READY for Stage G2 approval**, with
the existing protobuf exceptions and residual Debian/native-library limitations
explicitly carried forward for review. G1 itself does not publish anything.

Not performed: ARM build/execution, physical Coral hardware, production-data
access, an external live Frigate installation, bird-classification accuracy
benchmarking, byte-for-byte reproducibility certification, or GitHub workflow
execution. Integration used a local synthetic Frigate HTTP fixture, a real local
MQTT broker and the real CPU TFLite classifier.

No commit, Git tag, push, merge, release or registry publication was made.
Kingfisher was not accessed or modified. Earlier overwritten credentials cannot
be recovered by the compatibility fix; their original backup would still be
needed, as documented in Stage F.

Durable evidence: [G1 JSON results](security-0.4.1-stage-g1-results.json).
Raw local evidence: `/tmp/wamf041-stageg1/`, including no-cache build logs,
image inspection, full inventories, exact payload/file comparisons, full test
outputs, native results, vulnerability scan JSON, startup checks and upgrade
snapshots. Evidence reuse is explicitly identified above.

## Post-release dependency manifest cleanup

Following the v0.4.1 release, the redundant dependency snapshots
`requirements/frozen.txt` and `requirements/working.txt`, along with
the unused `requirements/ui-dev.txt`, were removed from `main`.

The original files remain available in the immutable v0.4.1 Git tag.
This housekeeping change does not modify runtime dependency versions,
the published Docker image, or the previously documented protobuf risk.
