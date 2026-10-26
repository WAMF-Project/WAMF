# WAMF v0.4.1 Stage F: authentication compatibility

The Stage E authentication release blocker is resolved in local image
`wamf:0.4.1-f-auth`. No commit, push, release or publication was performed.
All work used the maintenance worktree and Python 3.11.17. Earlier Stage A–E
changes were retained; Stage F did not change the Dockerfile or dependencies.

## Implementation

- `app/credentials.py` provides one read-only verifier. Fully parameterised
  PBKDF2 and scrypt use Werkzeug. Shortened PBKDF2 descriptors resolve to the
  historical SHA256 / 600,000-iteration defaults; shortened scrypt resolves to
  32768:8:1. Failed modern checks never fall back to a legacy method.
- The fixed-output legacy HMAC allowlist is blake2b, blake2s, md5, md5-sha1,
  ripemd160, sha1, sha224, sha256, sha384, sha3_224, sha3_256, sha3_384,
  sha3_512, sha512, sha512_224, sha512_256 and sm3. HMAC and legacy wrapper
  comparisons use `hmac.compare_digest`. API tokens additionally retain the
  original empty-salt and ASCII `plain` wrapper compatibility. Admin passwords
  do not accept those API-only wrappers. Bare digests, unsupported methods and
  malformed hashes fail closed.
- `app/bootstrap.py` recognises supported credentials without authenticating or
  generating a replacement. It validates existing admin and API hashes before
  any provisioning write. Errors identify the configuration field without
  including its value. Existing credentials and session secrets are preserved.
- A genuinely unconfigured installation has no configured admin hash, API hash
  or stored session secret: missing/blank values and the documented example
  placeholders qualify. It retains v0.4.0's automatic password provisioning
  and one-time temporary-password display after persistence. An environment
  session secret is retained. Partially configured existing installations with
  missing required admin credentials fail with a configuration error.
- `app/security.py`, `routes/auth.py` and `routes/admin.py` use the shared verifier
  for API tokens, login and password changes. Generation on explicit operator
  password/token changes remains modern. Verification never rehashes anything.
  Rate limiting, CSRF, cookie settings and persistent-data formats are unchanged.

## Regression tests

`tests/fixtures/credentials-v040.json` contains 57 synthetic vectors generated
and verified using Werkzeug 2.3.8 inside the unchanged published v0.4.0 image
(`sha256:46a23e993cd7f98559fcd86cb869ce6c32567bbe3de7b748f4a1319a55807002`).
They cover the full HMAC allowlist, PBKDF2, scrypt, shortened descriptors,
Unicode, empty-salt API hashes and ASCII plain API wrappers.

`tests/test_credentials.py` exercises correct/incorrect inputs, malformed and
unsupported formats, modern verification without fallback, the historical
PBKDF2 default, shared route verification, current-password checks and repeated
startup/authentication without config byte, mode or mtime changes. It also
checks sanitised configuration errors and no disclosure of existing secrets.
`tests/test_bootstrap.py` replaces the former invalid-hash reset expectations
with preservation/fail-closed expectations and retains first-run coverage.

- Host: **411 passed**, dedicated `.venv311/bin/python`, Python 3.11.17.
- Container: **406 passed, 5 skipped**. Only the Node-dependent browser restart
  cases skipped; all five passed on the host. Runtime dependencies came from
  the image; pytest tooling and source were mounted read-only externally.
- Host `pip check`: passed. The image's complete installed dependency metadata
  also passed checking; packaging tools remain absent from the runtime image.
- Five malformed-config cases each rejected twice inside the built image,
  preserving file bytes, permissions and mtime, without provisioning or revealing
  stored values.
- Fresh-container provisioning, login, CSRF, media, SQLite writes, Admin Restart,
  recreation/persistence and normal shutdown passed.
- Bundled TFLite model inference and JPEG/PNG/WebP preprocessing passed with
  predictions identical to the Stage D baseline. All native extension shared
  libraries resolved.

## Upgrade and rollback

Five disposable installations were created with the original image, then each
ran through v0.4.0 → Stage F → v0.4.0, including Admin Restart during every phase:

| Scenario | Admin / API formats | Result |
| --- | --- | --- |
| Current paths | PBKDF2 / scrypt, explicit operator rotation on upgrade | PASS |
| Legacy paths and environment session secret | scrypt / PBKDF2 | PASS |
| SHA256 legacy | SHA256 HMAC / SHA256 HMAC | PASS |
| SHA1 legacy | SHA1 HMAC / SHA1 HMAC | PASS |
| MD5 legacy | MD5 HMAC / MD5 HMAC | PASS |

All 15 phases passed authentication, API checks, inherited signed sessions,
CSRF, logout, database/media serving, range requests, configuration save,
restart, actual MQTT-triggered model inference and clean exit (code 0).
SHA256, SHA1 and MD5 credentials remained unchanged across all transitions.
There were no API HTTP 500 regressions. Explicit modern credential rotation
in the current-path scenario also remained usable after rollback.

At startup, read-only authentication and shutdown boundaries, configuration,
credentials, backups and media remained byte-for-byte unchanged. Existing
business records and schema were unchanged. Expected writes were appended
lifecycle audit events, operator-requested config saves/backups and rotations,
and one new synthetic detection per phase. No migration is required.

All data and credentials were synthetic, in uniquely named disposable volumes
on an internal network without published ports. Resources were cleaned up;
production data and Kingfisher were not accessed. The original and Stage D
image IDs remain unchanged. The fix preserves credentials present before
upgrade; it cannot reconstruct a hash already overwritten by an earlier
candidate. Such an installation would require its original configuration backup.

## Image and outstanding findings

Image ID: `sha256:69c22df0e48e0cdaa4bc444ae0ab2255191cb1f4421a298d79533eef840161da`.
Size: **621,275,937 bytes**, 5,495 bytes larger than Stage D. Python and Debian
package inventories are identical to Stage D (24 distributions, 88 OS packages).

Trivy reports no critical findings; the residual findings are unchanged:

- Two high protobuf findings, **CVE-2025-4565** and **CVE-2026-0994**. Upstream
  fixes conflict with the retained TFLite Support 0.4.4 / protobuf <4 requirement.
- 166 Debian matches across 73 advisory IDs: 44 high, 60 medium, 61 low and one
  unknown. No fixed versions are reported. Stage D's applicability qualifications
  still apply; these counts do not establish that every match is exploitable.

Legacy HMAC and plain API wrappers remain weak legacy formats accepted solely
for existing credential compatibility; new credential generation remains modern.
There were no remaining regressions in the tested scenarios. Physical Coral
hardware and ARM execution were not tested. Native-library and residual CVE
limitations recorded in Stage D remain.

The authentication blocker is resolved for the tested maintenance candidate.
Release review can proceed with the existing security exceptions. Raw local
evidence is under `/tmp/wamf041-stagef/`; the sanitised durable summary is
`docs/security-0.4.1-stage-f-results.json`.
