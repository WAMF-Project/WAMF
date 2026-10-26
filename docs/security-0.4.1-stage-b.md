# WAMF 0.4.1 Stage B validation

Validated locally on 2026-10-26 in `maintenance/0.4.1`. No publication,
Git tags, commits, or changes to existing installations were performed.

## Image and Docker changes

- Local tag: `wamf:0.4.1-test`, Linux amd64, 1,515,506,378 bytes.
- Image ID: `sha256:7b743ac2a9c9a4e6d2dababe532a29198cf10f61db152762ff71c8c98ba24778`.
- Base: official `python:3.11.17-slim-trixie`, pinned to index digest
  `sha256:b7a9e5f6a93d6d4a31f129bbd4bdee69efccb6ded0fb230d78aafaf37af98377`.
  The reviewed amd64 manifest is
  `sha256:b95327ce1439e687237841b0210677aeadd62d55a060a5f6bcb9c15bf6251dd3`.
- Retains Debian 13 (trixie), the suite used by the original v0.4.0 image.
  The resulting image reports Debian 13.7 and Python 3.11.17.
- Keeps Debian security upgrades and the existing system package list.
- Explicitly applies `requirements/constraints.txt` and runs `pip check`
  during the build.
- Updates inherited build tools to pip 26.2.2, setuptools 84.1.0,
  wheel 0.48.0 and packaging 26.3. The initial base tools had fixable
  findings, including a vulnerable vendored jaraco.context in setuptools.
- Preserves entrypoint, CMD, working directory, port 7767, default root user,
  persistent directory paths, and existing permission behavior.
- Application code and the publish workflow are unchanged. The packaging
  test now requires a digest-pinned Python 3.11/trixie base and explicit constraints.

All 25 runtime distributions match the Stage A constraints, including NumPy
1.26.4, TFLite Support 0.4.4, protobuf 3.20.3 and Paho MQTT 1.6.1.

## Validation

- Local image build and in-container `pip check`: passed.
- Dedicated host Python 3.11.17 application suite: **251 passed**.
- Real bundled-model CPU inference in a read-only container: passed for
  JPEG, PNG and WebP, including Pillow preprocessing and OpenCV decoding.
- Isolated container smoke tests: passed fresh bootstrap, generated credentials,
  config file mode 0600, login, CSRF rejection, configuration save/backups,
  media paths and serving, traversal rejection, SQLite writes, Admin Restart,
  session persistence, and clean shutdown.
- Container recreation with the same temporary volumes preserved configuration,
  credentials, SQLite detections and media. All test containers and volumes
  were removed; no host ports or real installation directories were used.
- v0.4.0 image ID remained
  `sha256:46a23e993cd7f98559fcd86cb869ce6c32567bbe3de7b748f4a1319a55807002`.

## Security findings

The installed-distribution audit queried PyPI advisory metadata for all 29
installed distributions. Only protobuf 3.20.3 reported findings: four entries
representing **CVE-2025-4565** and **CVE-2026-0994**. Fixes require protobuf
versions outside TFLite Support 0.4.4's `<4` constraint.

Trivy 0.76.1 was downloaded from its official release and verified against the
published SHA256 checksum. Its database was updated at
`2026-10-26T07:15:18.945937093Z`. The scan included all severities, unfixed
findings, installed package metadata and embedded SBOMs; no ignore list was used.

| Scan target | Package/advisory matches | Unique advisory IDs | Fixed version reported |
| --- | ---: | ---: | ---: |
| Debian, 294 installed packages | 719 | 256 | 0 |
| Python and embedded SBOM components | 9 | 9 | 9 |

Debian matches comprise **1 critical, 207 high, 295 medium, 195 low and 21
unknown**. Statuses are 367 `affected`, 330 `fix_deferred`, and 22
`will_not_fix`. Repeated matches across binary packages built from one source
package are not separate vulnerabilities. No fixed trixie package version is
reported; this does not mean no fix exists in another upstream release.

The critical Debian match is **CVE-2026-6653 in libxml2**
(`2.12.7+dfsg+really2.9.14-2.1+deb13u3`). Other high findings include FFmpeg,
TIFF, X11, ncurses and util-linux. These require applicability review and/or
future Debian updates; they are separate from the protobuf exceptions.

The nine Python/SBOM matches comprise six high, two medium and one low:

- Two protobuf CVEs: upstream fixes exist, but conflict with the preserved
  TFLite Support requirement. These remain the two known compatibility exceptions.
- Pip's actual vendored **urllib3 2.7.0**: CVE-2026-97687,
  CVE-2026-97688 and CVE-2026-97689; component fix is urllib3 2.8.0.
  WAMF's separately installed urllib3 is already 2.8.0, which does not replace
  pip's private copy.
- Pip's actual vendored **msgpack 1.1.2**: GHSA-6v7p-g79w-8964;
  component fix is msgpack 1.2.1.
- Pip's embedded SBOM lists **setuptools 70.3.0**, producing CVE-2025-47273,
  CVE-2026-59890 and GHSA-xmj3-9gf7-h52w. Actual installed setuptools is
  84.1.0; pip ships a `pkg_resources` subset, with neither a vendored
  `setuptools` directory nor `package_index.py`. These three matches need
  per-advisory applicability review and are not established runtime exploits.

The four confirmed pip-vendored component findings need an upstream pip update
that refreshes those copies, a reviewed vendor patch, or a separate decision
to remove build tools from the runtime image. This stage does not rewrite
vendored code, remove pip, suppress findings, or override incompatible requirements.

## Coverage and remaining risks

OpenCV's native build inventory includes Qt 5.15.16, libpng 1.6.44 and FFmpeg
libavcodec 59.37.100. Pillow reports WebP 1.6.0, libtiff 4.7.2 and
libjpeg-turbo 3.2.0. Debian upgrades do not replace wheel-bundled libraries.
Trivy's metadata/SBOM scan does not establish the absence of vulnerabilities
in every embedded ELF library or statically linked component, including TFLite.
The complete native build inventory is retained for further assessment.

No live Frigate/MQTT, Coral hardware, ARM image, production data upgrade or
classification-accuracy comparison was performed. The inherited root user and
Flask development server remain unchanged. A pinned base still requires regular
digest refreshes; Debian repositories and isolated package build dependencies
are not frozen snapshots, so this is not a byte-for-byte reproducible build.

The existing publish workflow scans only critical findings with available fixes
(`ignore-unfixed: true`). It would not enforce review of all findings above.
Operational validation passed, but security review remains outstanding; do not
describe this image as having only two residual vulnerabilities.

Raw local evidence is in `/tmp/wamf041-stageb/`: `build.log`,
`image-inspect.json`, `native-report.txt`, `python-audit.json`, `trivy.json`,
`vendor-inventory.txt`, `smoke-result.txt`, validation scripts and the scanner's
checksum record. Reports cover the final image ID above.
