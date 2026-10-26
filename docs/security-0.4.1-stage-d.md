# WAMF 0.4.1 Stage D: runtime image reduction

Validated locally on 2026-10-26 in `maintenance/0.4.1`. This continues the
Stage A dependency updates and Stage B base-image work. No application code,
entrypoint, port, user, persistent paths, configuration format or database format
was changed. No release, Git tag, commit, push or publication was performed.

## Recommendation

Keep all three reductions: omit system FFmpeg, remove build/provisioning tools
after installation checks, and remove unused OpenCV and its graphics dependencies.
Retain `libusb-1.0-0`, which TFLite's native extensions require even with Coral
disabled. Keep every remaining declared Python dependency, including pybind11.

The final image uses Python **3.11.17**, NumPy **1.26.4**, TFLite Support
**0.4.4**, protobuf **3.20.3**, and Paho MQTT **1.6.1**. Its 24 installed
runtime distributions exactly match the constraints and frozen snapshot.

## Measured results

Sizes are Docker-reported image bytes, not compressed download sizes or exclusive
host disk usage. Scanner counts are package/advisory matches, including duplicates
across Debian binary packages, and are not counts of demonstrated exploits.

| Image | Size, bytes | Debian packages | Python distributions | Debian matches (unique IDs) | Critical matches | Python matches |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Stage B `wamf:0.4.1-test` | 1,515,506,378 | 294 | 29 | 719 (256) | 1 | 9 |
| Step 1 `wamf:0.4.1-d1-no-ffmpeg` | 1,179,399,338 | 131 | 29 | 226 (109) | 1 | 9 |
| Step 2 `wamf:0.4.1-d2-no-tools-complete` | 1,152,089,708 | 131 | 25 | 226 (109) | 1 | 2 |
| Step 3 `wamf:0.4.1-d3-minimal` | **621,270,442** | **88** | **24** | **166 (73)** | **0** | **2** |

Final reduction versus Stage B: **59.01%** by reported image size, 206 Debian
packages, four packaging distributions and OpenCV. Complete versioned removals,
per-step image IDs and scan summaries are in
[the package inventory](security-0.4.1-stage-d-packages.json).

Final image ID:
`sha256:2f333ce201708ec5ec3bd70e5045c4e4a867f5baa1e4225ebcfd4f7d4b495c29`.

All builds retain the reviewed official Python 3.11.17/trixie base index digest
`sha256:b7a9e5f6a93d6d4a31f129bbd4bdee69efccb6ded0fb230d78aafaf37af98377`.

## Step 1: remove system FFmpeg

Repository searches found no application or startup invocation of `ffmpeg` or
`ffprobe`. The README's FFmpeg section configures Frigate, a separate service.
`scripts/start-birdtest.sh` is a host-only stream generator, not copied into the
image or invoked by the WAMF entrypoint. WAMF downloads/proxies clip bytes rather
than decoding or transcoding them.

Replaced FFmpeg with explicit native-extension dependencies: libusb and, during
this comparison step, OpenCV's graphics libraries. This removed 163 Debian
packages, including FFmpeg codecs, cJSON, audio and other multimedia libraries.
All Python distributions remained unchanged. OpenCV import and image decoding
still passed. Libxml2 remained indirectly required by the graphics stack.

## Step 2: remove packaging tools

Patched build tools install the pinned runtime dependencies and run `pip check`.
The Dockerfile then uninstalls pip, setuptools, wheel and otherwise-unused
packaging, retaining all runtime distributions and their metadata. Installation
and removal occur in the same Docker RUN instruction in the final Dockerfile.

A deeper inspection found CPython's `ensurepip` directory still carried old
pip 24.0 and setuptools 79.0.1 wheels. The complete cleanup also removes this
bootstrap payload. Thus the runtime image intentionally supports neither pip
commands nor reinstallation through `python -m ensurepip`.

Validated absence of packaging executables, package directories, vendored pip
libraries, `_distutils_hack`, `pkg_resources`, `ensurepip`, and bundled `.whl`
files in the merged runtime filesystem. This does not rewrite deleted contents
in inherited base-image layers.

The initial `wamf:0.4.1-d2-no-tools` comparison image is retained. The
`-no-tools-complete` image adds the ensurepip cleanup without replacing it. Both
variants passed runtime checks; the table uses the complete variant. A similarly
retained initial `wamf:0.4.1-d3-no-opencv` image preceded the final cleanup.

Once pip is absent, dependency validation uses the image's actual
`importlib.metadata` inventory and checks every active `Requires-Dist`
specifier with the dedicated host Python 3.11 environment. No declared dependency
is missing or incompatible. Build-time `pip check` also passed.

## Step 3: remove OpenCV

Production Python code, startup scripts and the supported classifier path do not
import cv2 or use OpenCV. TFLite Support's TensorImage array path uses NumPy and
native image utilities. Earlier read-only investigation verified actual model
inference with cv2 imports blocked.

Removed OpenCV consistently from base requirements, runtime constraints and both
dependency snapshots. Removed stale cv2 mocks from two tests so they cannot mask
a future accidental OpenCV import. No application implementation was changed.

With OpenCV absent, the OS package list reduces to libusb beyond the updated base.
This removes another 43 Debian packages, including libxml2 and the graphics stack,
as well as OpenCV's bundled Qt, FFmpeg, libpng and OpenSSL 1.1.1 libraries.
Native-extension `ldd` checks find no unresolved shared libraries.

## Runtime validation

Every completed step passed:

- Python 3.11 and exact runtime-version checks; all active declared dependency
  requirements satisfied. Pybind11 and the preserved TFLite stack remain installed.
- Real bundled-model CPU inference with JPEG, PNG and WebP images and WAMF's
  RGB conversion, thumbnail, canvas-padding and contiguous NumPy preprocessing.
  All five returned category indices, names and scores matched Step 1 exactly
  in Steps 2 and 3 for all three inputs.
- Startup into setup mode, generated credentials, port 7767, config mode 0600,
  login, CSRF rejection, config saves/backups and Admin Restart.
- Snapshot and clip serving, traversal rejection, SQLite writes and session
  persistence across Admin Restart.
- Container recreation preserving configuration, credentials, SQLite detections
  and archived media; clean shutdown without OOM or forced termination.

The final source passed **251 application tests** in the dedicated host Python
3.11.17 environment. Inside the final minimal image, **246 passed and 5 skipped**;
the skips are the browser restart cases requiring Node, which is absent from the
runtime image. Those five passed in the host run.

In-container pytest used read-only source and pytest-tool snapshots, with only
temporary writable data directories. Application dependencies came from the
image, not the host virtual environment. A missing pytest `py.py` shim in the
initial external test harness was supplied before the successful runs; no
application or runtime regression was encountered.

No live Frigate/MQTT or Coral hardware test, ARM build, production-data upgrade,
or bird-image accuracy benchmark was performed. Synthetic image comparisons
establish pipeline execution and consistency, not classification accuracy.

## Remaining security findings

Trivy 0.76.1 scanned each image at all severities without an ignore list. Its
database was updated at `2026-10-26T07:15:18.945937093Z`. The final image reports:

- **No critical matches.** Libxml2 CVE-2026-6653 and the other libxml2 findings
  are eliminated by package removal.
- **166 Debian matches across 73 advisory IDs:** 44 high, 60 medium, 61 low and
  1 unknown. None has a fixed trixie version reported by the scanner.
- The high Debian matches group into util-linux local mount/namespace issues,
  ACL pathname handling, the ncurses infocmp utility, a source-level systemd-homed
  match against libraries, and a Perl Archive::Tar match against perl-base.
  The previous triage's absent-service/module and unused-utility qualifications
  still apply. No blanket suppression was introduced.
- **Two Python matches:** protobuf CVE-2025-4565 and CVE-2026-0994. Upstream fixes
  exist but conflict with TFLite Support 0.4.4's protobuf `<4` constraint.
- Pip-vendored urllib3 findings and the MessagePack/setuptools SBOM matches no
  longer occur, because their containing tooling is removed.

An absence of scanner matches does not certify every statically linked TFLite
or Pillow native component. Keep the trusted bundled model and preserved runtime
constraints; review future Python/Debian updates. The root user and Flask server
remain as before. The publish workflow is unchanged and still scans only fixable
critical findings; it does not enforce review of all residual findings here.

## Evidence and scope

Local raw evidence is under `/tmp/wamf041-staged/`, including per-step build
logs, Dockerfile snapshots, native results, image metadata, complete OS/Python
inventories, smoke-test results, Trivy JSON reports, and final container pytest
output. The versioned package-removal summary is also recorded in this directory.

Comparison images were retained. UUID-named smoke containers and volumes were
removed after validation. The existing Stage B image and the v0.4.0 image were
not replaced; no real installation directories or production data were mounted.
The Kingfisher worktree was not modified.
