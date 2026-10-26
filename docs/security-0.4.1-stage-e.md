# WAMF 0.4.1 Stage E: upgrade and rollback compatibility

Validated on 2026-10-26 in `maintenance/0.4.1`, using the existing images:

- Original v0.4.0: `ghcr.io/wamf-project/wamf:0.4.0`, image ID
  `sha256:46a23e993cd7f98559fcd86cb869ce6c32567bbe3de7b748f4a1319a55807002`.
- Candidate: `wamf:0.4.1-d3-minimal`, image ID
  `sha256:2f333ce201708ec5ec3bd70e5045c4e4a867f5baa1e4225ebcfd4f7d4b495c29`.

**Recommendation: hold an unconditional release.** Normal PBKDF2/scrypt
installations passed upgrade and rollback. An additional legacy-hash test found
an authentication compatibility defect that can rewrite an existing admin
credential during candidate startup and break API-token authentication.

No application code or images were changed. Only this report and its
[sanitized results](security-0.4.1-stage-e-results.json) were added during Stage E.

## Representative installations

Each scenario used new UUID-named Docker volumes. The original image created
credential hashes and the complete v0.4.0 database schema. Fixtures contained
three detections, three species-metadata records including Unicode text,
retention status, migration history, an existing audit entry, an existing config
backup/example, three real JPEG snapshots and three synthetic valid MP4 clips.
Archived database paths covered absolute, repository-relative and basename forms.

| Scenario | Paths and port | Credentials | Result |
| --- | --- | --- | --- |
| Standard | `/app/config`, `/app/data`, `/app/media`; 7767 | PBKDF2-SHA256 admin, scrypt API token; configured session secret | Upgrade and rollback passed |
| Legacy paths | Same volumes additionally mounted at `/data` and `/media`; retained absolute paths and port 7766 | scrypt admin, PBKDF2-SHA256 API token; unchanged `WAMF_SECRET_KEY` override | Upgrade and rollback passed |
| Legacy hashes | Separate disposable installation; default paths | Legacy salted `sha256` admin and API-token hashes accepted by v0.4.0 | Compatibility defect reproduced |

The normal scenarios ran against a private `--internal` Docker network with a
real local Mosquitto broker and a synthetic Frigate HTTP fixture. No host ports
were published, and no real installation directories were mounted. The weak-hash
reproduction ran with networking disabled except container loopback.

## Passed behavior for PBKDF2/scrypt installations

Both normal installations completed original v0.4.0 -> candidate -> original
v0.4.0 against the same volumes, with clean shutdown between images. All six
phases verified:

- Existing credentials and API tokens, rejection of incorrect credentials/tokens,
  protected Admin/API routes and ordinary administrative pages.
- Fresh logins, signed sessions inherited from the preceding image, unchanged
  CSRF tokens, acceptance of valid CSRF and rejection of missing/incorrect CSRF,
  and logout.
- Existing public detection records, snapshots and clips through both media-route
  aliases, clip range responses, Frigate media proxying and traversal rejection.
- Configuration saves/backups preserving all values and credential hashes,
  Admin Restart with a new instance ID, and session continuity after restart.
- Normal MQTT subscription and real bundled-model inference from a controlled
  Frigate event, using the supported sub-label fallback to persist a new record.
  These were actual native workers, not mocked application processes.
- Normal shutdown with exit code zero and no OOM/forced termination.

The standard candidate additionally changed the password and regenerated the API
token through the Admin UI. Both replacements worked after rollback to v0.4.0.
The legacy-path scenario retained its original hashes throughout the entire cycle.

The initial test-client run created a duplicate domainless cookie and failed its
logout assertion on v0.4.0. Correcting the external harness to preserve the cookie
domain resolved this; no application workaround was introduced.

## Persistent-data integrity

Snapshots were taken before startup, after read checks, after requested actions
and after clean shutdown. Comparisons covered file SHA256, size, mode, owner and
mtime; SQLite schema SQL and all business-table rows; migration history and
`PRAGMA integrity_check`.

For both normal installations, across upgrade and rollback startup:

- Configuration, credentials, session secret, existing examples/backups and all
  six media files were unchanged byte-for-byte, including file metadata.
- Existing detections, species metadata, retention state and all four migration
  entries were unchanged; no schema migration occurred.
- Existing audit history remained intact. Each image startup appended two normal
  lifecycle/MQTT entries. Database file hashes therefore changed as expected;
  byte-identical SQLite files are not claimed.

Requested operator/test actions were recorded separately from startup changes:

| State after phase | Detections, either scenario | Backups, standard | Backups, legacy paths |
| --- | ---: | ---: | ---: |
| Seeded installation | 3 | 1 | 1 |
| Original v0.4.0 | 4 | 2 | 2 |
| Candidate | 5 | 5 | 3 |
| Rollback to v0.4.0 | 6 | 6 | 4 |

Each phase intentionally saved configuration, restarted and injected one unique
test event. The standard candidate's two credential actions explain its two
additional backups and hash changes. All prior records, media and backups were
preserved; only the explicitly expected new detection and audit rows were added.

## Confirmed legacy credential defect

Read-only comparisons established that v0.4.0 accepts and verifies simple
`sha256`, `sha1` and `md5` hash methods. Candidate Werkzeug 3 rejects these with
`ValueError`; candidate bootstrap considers their admin hashes invalid.
`plain` was already rejected by v0.4.0 bootstrap and is not a new incompatibility.

A live separate `sha256` installation reproduced the persistent effects:

1. Original v0.4.0: existing admin login succeeded; API token returned HTTP 200.
2. Candidate startup: bootstrap replaced the valid legacy admin hash and printed
   a new temporary password. The original password stopped authenticating.
3. The legacy API-token hash stayed unchanged, but authenticated API requests
   returned HTTP 500 because the unsupported-method exception was not handled.
4. Rollback: the API token worked again. The original admin password still failed
   because rollback retained the candidate's newly generated hash; the replacement
   password worked. Rollback alone did not undo this configuration mutation.

This is a genuine compatibility failure, not a test-harness issue. The normal
PBKDF2/scrypt pass does not clear every existing v0.4.0 credential format.

## Migration requirements and release decision

No database, media or configuration migration is needed for the tested v0.4.0
PBKDF2/scrypt installations. Retain existing mount aliases for absolute `/data`
and `/media` paths; retain the effective session secret and align any custom port
with its Docker mapping. The candidate does not automatically rewrite these paths.

Before a general release, address legacy authentication explicitly. A minimal
safe policy must detect unsupported existing hashes before bootstrap can replace
them, prevent unhandled API-token verification errors, and provide a reviewed
migration path or compatibility mechanism. Add regression coverage and repeat
this legacy-hash cycle after any fix.

An operator migration before upgrade can use v0.4.0's password-change and token
regeneration facilities to produce compatible hashes. Regenerating an API token
requires updating its clients. Back up configuration first; restoring the old
image does not restore overwritten credentials. This prerequisite must not be
presented as transparent compatibility for legacy-hash installations.

These tests do not certify arbitrary older schemas, damaged configuration,
non-root/CIFS volume permissions, TLS proxy deployments, real Frigate versions,
or Coral hardware. Stage D's residual vulnerability review is unchanged.

## Evidence and cleanup

Normal-cycle raw evidence:
`/tmp/wamf041-stagee/run-072bce74b0/`.
Legacy-hash reproduction:
`/tmp/wamf041-stagee/legacy-hash-277db083bf/`.
Raw snapshots use private test-artifact directories and synthetic credentials.
The repository results file contains no plaintext passwords, tokens or
session-cookie values.

All disposable containers, volumes and internal networks were removed. Both
tested image identities remained unchanged. No real production data or
Kingfisher files were accessed or modified; nothing was committed or published.
