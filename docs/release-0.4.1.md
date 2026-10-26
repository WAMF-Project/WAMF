# WAMF v0.4.1 security maintenance release (candidate)

This release prepares security updates for existing v0.4.0 installations. It
retains Python 3.11, the existing classifier and persistent-data formats. It does
not include Kingfisher development features. Publication requires release approval.

## Deployment scope

WAMF v0.4.1 targets AMD64 (x86-64) deployments. ARM architectures are not
currently supported deployment targets, and ARM validation was not performed.
WAMF runs its classifier on the CPU and does not directly use Coral acceleration
(`use_coral=False`). Frigate may independently use Coral for object detection;
that does not change WAMF's classifier execution. This clarifies the existing
deployment scope without changing supported functionality.

## Security and runtime changes

Flask 3.1.3, Werkzeug 3.1.9, Requests 2.34.2, Pillow 13.0.0, certifi 2026.10.23
and urllib3 2.8.0 replace the previous vulnerable versions. Exact constraints
cover all 24 Python runtime distributions. NumPy 1.26.4, TFLite Support 0.4.4,
protobuf 3.20.3 and Paho MQTT 1.6.1 remain pinned for compatibility.

The Docker image uses the reviewed Python 3.11.17 slim-trixie digest and installs
available Debian updates at build time. Unused system FFmpeg, graphics/multimedia
dependencies and OpenCV are removed. Pip, setuptools, wheel, packaging and
ensurepip are removed after installation and dependency checking. Runtime package
metadata and required dependencies, including pybind11 and libusb, are retained.
The built image supports WAMF's application paths; it is not intended for in-place
package installation or use as a general multimedia/tooling environment.

Removing those components eliminated the previous libxml2, system multimedia
and packaging-tool scanner matches from the tested runtime image. OpenCV's
bundled native libraries were also removed; the scanner had not established a
complete vulnerability inventory for those libraries. The local amd64 image
decreased from 1,505,089,400 bytes (published v0.4.0), or 1,515,506,378 bytes
(Stage B, approximately 1.52 GB), to 621,275,926 bytes (G1, approximately 621 MB).
See the [G1 report](security-0.4.1-stage-g1.md) for final inventories and scans.

## Credential compatibility and upgrade

The Werkzeug upgrade required restoring verification of historical Werkzeug
HMAC password hashes and API-token wrappers. Verification uses a bounded method
allowlist and constant-time comparisons, with the historical 600,000-iteration
default for shortened PBKDF2-SHA256 descriptors. Modern PBKDF2 and scrypt
verification remains delegated to Werkzeug; failed checks do not fall back to
weaker methods. Bare digests and unsupported/malformed hashes are rejected.

Existing admin hashes, API-token hashes and session secrets are preserved during
startup and authentication. No automatic rehashing occurs. Malformed configured
credentials cause a sanitised startup configuration error, rather than a password
reset. Fresh installations retain the existing automatic provisioning and one-time
temporary-password display. Explicit password changes and token rotation continue
to generate modern credentials.

No v0.4.0 configuration, schema or media migration is required for the validated
paths. Back up configuration, database and media after a clean stop, retain the
old image, and use the same mounts and session-secret environment setting when
recreating the container. Existing `/data` and `/media` compatibility aliases and
custom port mappings should be preserved where used. See the
[Docker upgrade instructions](docker-testing.md#upgrading-v040-to-v041).

Stage F passed five original-image → candidate → original-image scenarios,
including SHA256, SHA1, MD5, PBKDF2 and scrypt credentials, session continuity,
API access, media, MQTT inference and restart persistence. G1 records final
candidate verification and any reuse of that evidence. No credential loss was
observed in the passing scenarios. A credential already overwritten by an older
pre-release candidate cannot be reconstructed by this fix; recover the original
configuration from its backup if that occurred.

## Known security limitations

- **Present:** protobuf 3.20.3 is still matched to CVE-2025-4565 and
  CVE-2026-0994. Upstream patched versions conflict with TFLite Support 0.4.4's
  protobuf `<4` requirement. The application uses its trusted bundled TFLite
  model; this context does not remove the vulnerable dependency or certify all
  protobuf/native-code paths as safe for untrusted input.
- **Residual scanner findings:** Stage F and G1 reported 166 Debian package matches
  across 73 advisory IDs, with no fixed versions reported by the scanner. Some
  have absent-component or unused-utility qualifications described in the
  [Stage D assessment](security-0.4.1-stage-d.md#remaining-security-findings).
  They have not been blanket-suppressed or all classified as exploitable.
- **Removed findings:** scanners no longer match the removed system multimedia
  stack or packaging-tool vendored components in the tested runtime image.
- **No critical scanner matches** were reported for Stage F or G1. Scanner
  coverage does not establish that every
  statically bundled native library is free of vulnerabilities.
- Legacy HMAC and plain API-token wrappers remain weak historical formats
  supported for compatibility. Operators can explicitly change passwords and
  rotate tokens using the existing UI; no new mandatory workflow is imposed.
- The existing root runtime user and Flask server remain unchanged. Physical
  Coral hardware and ARM images were not validated in this maintenance run.

Python versions and runtime dependency resolution are pinned, but downloadable
artifacts are not hash-locked and Debian updates use live repositories. Future
builds require renewed inventory and scan review. This image is not claimed to
be vulnerability-free.
