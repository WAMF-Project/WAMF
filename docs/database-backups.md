# Database backup and offline restore — Kingfisher #49, Slices 1–5

The backend defines managed storage, artifact identity, package format,
read-only validation, online creation, atomic publication, enumeration, pinned
download resolution, durable restore previews, and explicit offline application
and rollback. System Health provides authenticated backup/restore actions and
queues confirmed restores for the native restart boundary. Services do not start
at import time and backups are never scheduled automatically. All tests use synthetic
temporary databases; automated validation never backs up the deployment DB.

## Storage and identity

The fixed layout under WAMF's existing `data/` mount is:

```text
data/backups/database/
  wamf-db-YYYYMMDDTHHMMSSffffffZ-<32 lowercase random hex characters>.zip
  .staging/
    <generated artifact names for package writers>
    create-<random>/database.db    # private SQLite copy, then removed
    validate-<random>/database.db  # temporary, removed after validation
  .restore/                      # private restore plans, separate from active DB
    .record.key                  # private record/confirmation authentication key
    .catalog.lock
    requests/operation.json      # authenticated single pending restart request
    targets/<target-path-digest>/operation.json  # authenticated head and media hold
    restore-<32 random hex>/
      .lock
      operation.json             # authenticated durable state
      package.zip                # independent captured input
      candidate.db               # verified, unmigrated candidate
```

The `data/` mount is the trusted anchor and can resolve to an administrator-owned
mount location. Its backup descendants cannot be symlinks. This location is
independent of custom active database and model locations; there is no new
configuration option. Path helpers in `wamf_paths.py` inspect without creating
directories or reading configuration. Existing startup directory creation is
unchanged.

`BackupIdentity.backup_id` is the generated filename stem. Treat it as opaque;
service operations never accept filesystem paths. Generation uses a UTC
microsecond timestamp and 128 random bits. Reservations use `O_EXCL`, bounded
collision retries and no overwrite. A reservation is an empty private file for
a writer, not a complete backup or an atomic publication mechanism by itself.
Failures remove only the originally reserved inode. Nothing is automatically
pruned, including manually created artifacts.

Managed directory access uses pinned directory descriptors, `O_DIRECTORY`,
`O_NOFOLLOW`, relative opens/mkdir/unlink, and owner checks. Explicit creation
sets owned backup directories to `0700`; files are `0600`. Reading rejects
nonprivate files/directories, symlinks, nonregular files, and files with more
than one hard link. The data mount's permissions are not changed. The service
account and trusted mount administrator remain trusted: these primitives do not
defend against a malicious process with the same account rewriting private
storage. POSIX descriptor/no-follow support is required, consistent with WAMF's
existing flock-based services.

## Package v1

A package is an ordinary single-disk ZIP containing exactly two unique regular
file members, in either order:

```text
database.db
manifest.json
```

Only `ZIP_STORED` is accepted. Encryption, compression, directory/symlink/special
entries, duplicate or additional members, alternate names, absolute paths,
traversal, NUL tricks and backslash variants are rejected. Version 1 also rejects
ZIP64, data descriptors, ZIP comments/extra fields, split archives, prepended or
trailing data and gaps between local entries. Local and central headers must
agree. Writers should use a seekable ordinary `zipfile.ZipFile` output with
fixed member names and no optional ZIP features.

Limits:

| Item | Maximum |
| --- | --- |
| Database payload | 1 GiB (1,073,741,824 bytes) |
| Manifest | 64 KiB (65,536 bytes) |
| ZIP metadata allowance | 4 KiB |
| Entire package | 1 GiB + 64 KiB + 4 KiB |
| Members | Exactly 2 |

The end record and central-directory size are checked before `ZipFile` allocates
member objects. Header lengths are checked, but actual decompressed/streamed
bytes are independently counted. ZIP CRC failures reject the package. These
limits apply to on-disk validation; upload handlers must also enforce
limits while receiving request bytes and before accepting temporary uploads.

## Manifest v1

`BackupManifest` is immutable and validates construction and parsing. UTF-8 JSON
must contain exactly these keys; unknown/missing keys, duplicate object keys,
nonstandard constants and malformed data are rejected:

| Key | Contract |
| --- | --- |
| `backup_format_version` | Integer `1`, not a boolean; other integers unsupported |
| `application` | `WAMF` |
| `wamf_version` | Nonempty provenance string from `version.VERSION` |
| `sqlite_version` | Nonempty provenance string from `sqlite3.sqlite_version` |
| `started_at`, `completed_at` | Valid UTC `YYYY-MM-DDTHH:MM:SS[.ffffff]Z`; completion must not precede start |
| `source_database` | Basename only, maximum 255 characters; no separators, colon, control characters, `.` or `..` |
| `database_member` | `database.db` |
| `database_size` | Integer from zero through 1 GiB, not a boolean |
| `database_sha256` | Exactly 64 lowercase hexadecimal characters |
| `recognized_migration_id` | Recognized ID or JSON null |
| `recognized_sequence` | Matching positive sequence or JSON null |
| `history` | Array of unique `{migration_id, applied_at}` entries, in #52 inspection order |
| `scope` | `database-only` |

Migration history strings preserve the ledger's original timestamps, including
SQLite's historical timestamp syntax. They are not rewritten as UTC backup
timestamps. Both recognized fields are null when #52 cannot establish an exact
legacy version; historical marker maxima must never be promoted to a version.
Migration metadata validation checks shape only, not a second registry.

Application and SQLite versions are provenance, never schema compatibility
gates. A SHA-256 digest detects payload changes but is **not** authentication:
an attacker can change a payload and its manifest together. Absolute source
paths, configuration/secrets, models, `birdnames.db`, snapshots and clips are
outside the package. Database rows may themselves retain historical media paths.

## Validation boundary

`validate_package(BackupIdentity(...), staged=False)` reads a managed artifact.
Use `staged=True` for an artifact previously reserved in `.staging/`. Arbitrary
paths, raw `.db` imports and network streams are not public validation inputs.

Validation checks package structure and manifest, streams the database into a
private generated extraction directory, verifies actual size and SHA-256, then
calls `inspect_database(path, check_integrity=True)`. This is #52's authoritative
compatibility logic, including `quick_check`. Accepted database metadata must
exactly agree with the manifest's recognized version and history. A separate
full read-only `PRAGMA integrity_check` supplements the quick check. Neither
step loads extensions, executes uploaded SQL or calls `ensure_schema()`.

| #52 classification | Package result |
| --- | --- |
| `current` | Valid if metadata and full integrity also pass |
| `upgradeable` | Valid preview input; not migrated |
| Recognized `legacy` | Valid preview input, with nullable exact version; not migrated |
| `newer` | Rejected as `newer_database` |
| `invalid` | Rejected as `invalid_database` |
| `unknown` (including foreign/empty DB) | Rejected as `unknown_database` |
| Unsupported WAL format | Rejected as `wal_unsupported` |

WAL support is explicitly deferred. The inspector rejects WAL header formats
before opening SQLite. Validation never changes journal mode, checkpoints,
recovers journals, deletes live sidecars, or uses `immutable=1` on a live DB.
The extracted payload is self-contained; no uploaded sidecar is accepted.

Results are immutable and contain a stable `ValidationCode`, identity, parsed
manifest and actual size/checksum facts when available, and the #52 inspection
with its temporary path removed. `valid` distinguishes acceptance from merely
having a parsed manifest. `compatibility` remains available for rejected newer,
invalid or unknown inputs. Raw exceptions, SQL values and server paths are not
presentation error codes. Extraction is removed on success and ordinary failure;
the package and original database bytes are unchanged. File access may update
filesystem access times. A process crash can leave private staging debris;
safe stale-workspace recovery belongs to later operation lifecycle work.

This is a synchronous internal primitive. The 1 GiB limit bounds input bytes,
not SQLite CPU time or memory for pathological inputs. Before exposing uploads,
an owned worker must provide execution deadlines/cancellation and process
resource limits. Future Admin operations require enabled Admin authentication,
CSRF checks and authenticated downloads. No Admin surface exists in this slice.

## Online creation

`app.database_backup_service.create_backup(bound_database_path, backup_root,
timeout_seconds=300, page_batch=64, cancelled=None, progress=None)` is a
synchronous service. The caller supplies its **already-bound absolute database
path** and `get_database_backup_root()`. The service never reloads configuration,
resolves a fresh runtime database default, calls `ensure_schema()`, reuses a
request connection, shells out, or copies the active file through the filesystem.
The root argument must match the fixed managed root; it is not an arbitrary
destination or a new configuration option.

Preflight records a regular, single-link source identity and rejects symlinks in
all source components. File identity is checked again before copying and after completion;
ordinary changes to content/size from WAMF writes are allowed, replacement is
not. `inspect_database(..., check_integrity=True)` must report `current`, and a
separate read-only full integrity check must return exactly `ok`. Legacy,
upgradeable, newer, invalid, unknown and WAL-format sources are refused. Source
connections use SQLite URI `mode=ro` and `query_only=ON`. No source journal-mode
change, checkpoint, journal recovery or writable connection is requested.

#52 reads the SQLite header through ordinary file I/O before opening SQLite.
Opening/closing a raw DB descriptor in a process with another thread's SQLite
connections can release that process's POSIX database locks. Consequently the
service runs source inspection in a **fresh, owned interpreter**, with no
inherited SQLite connections in use. The parent uses directory descriptors and
stat calls for source identity, and SQLite connections for all source data
reads; it never opens/closes a raw source DB descriptor. The source inspection
child is bounded by the operation deadline, killed/reaped on cancellation, and
uses the existing WAMF probe ownership pattern (parent PID checks and Linux
parent-death SIGKILL) so supervisor shutdown cannot leave it running. This child
does not start services or reload configuration. A regression test retains a
writer transaction while backing up and confirms a separate process still
cannot acquire its writer lock afterward.

The initial disk-space estimate is three times the observed database size plus
64 MiB and the manifest allowance. Peak use includes the copied database,
ZIP_STORED package and package validator's extraction. This is a preflight
estimate, not a disk reservation: concurrent growth/other users can still cause
a later I/O failure. Database growth is checked against the 1 GiB limit during
copying and after completion.

The service opens a dedicated source/destination connection pair and invokes:

```python
source.backup(destination, pages=page_batch, progress=callback,
              name='main', sleep=0.01)
```

Only `main` is copied, with 64-page batches by default. No long-lived source read
transaction is deliberately held across batches. SQLite acquires source read
locks while stepping and owns the destination write transaction. Other
connections can commit between steps; SQLite can restart copying when they do.
Success produces a transactionally consistent committed snapshot, **not a
promise of the source state at the initial operation timestamp**. Copy counts
and percentage can regress/reset as the source changes. The `sleep` argument is
the busy/locked retry delay, not a mandatory pause after each successful batch.
See the [SQLite backup API](https://www.sqlite.org/c3ref/backup_finish.html) and
[Python connection backup documentation](https://docs.python.org/3.12/library/sqlite3.html#sqlite3.Connection.backup).

The backup-specific guard does not exclude ordinary reads/writes or use the
retention/media locks. Preflight quick/full scans are real reads and can delay
rollback-journal writers while their read locks are held. Sustained writes or
locks can prevent completion; a bounded operation fails safely instead of
retrying indefinitely.

`cancelled()` is checked before phases, during streamed I/O and after SQLite
backup steps, including busy retries. The monotonic deadline defaults to 300
seconds. The additive #52 `checkpoint` argument and shared SQLite progress guard
allow inspection/full-check scans to propagate cancellation without treating it
as database corruption. Checkpoints run every 1,000 SQLite VM instructions;
maintenance lock waits are short (100 ms). These are cooperative limits, not
hard preemption of filesystem syscalls, Python schema parsing, or an individual
SQLite page step. Caller hooks must be quick; untrusted uploads still require
future owned workers and process resource limits.

## Verification and publication

After backup returns, **both connections close before verification/hash**. The
destination must have no journal/WAL/SHM sidecars, pass #52 inspection as
`current`, match the preflight migration history and pass full integrity checking.
Its inspected schema/history, closed-file size and SHA-256 populate the manifest;
source assumptions never replace destination facts. The manifest's `completed_at`
is recorded when the manifest is sealed into the package, after the database
member is written. Result/progress completion timestamps additionally include
package self-validation and publication; neither is an exact snapshot instant.

The private ZIP_STORED package is flushed and file-fsynced, then revalidated by
the Slice 1 package validator before publication. Publication holds a dedicated
nonwaiting `.backup.lock` flock for the entire creation operation. It works
across processes and threads and is separate from SQLite/retention/media locks.
Future restore lifecycle exclusion must coordinate with this guard. The lock
file is persistent and must never be unlinked as a release mechanism.

Publication exclusively reserves a generated final filename with `O_EXCL`,
retrying collisions at most eight times. It verifies that both the staged file
and final reservation still name their owned inodes and that both directories
share a filesystem. `os.replace()` atomically replaces **only that operation's
empty reservation** with the complete package. It never intentionally replaces
an existing backup. This relies on the documented private service-account trust
boundary and on all cooperating creators taking the root guard; it does not
claim to resist an attacker already running as the service account.

Root and staging directory fsyncs follow rename. Pre-publication file-fsync,
validation, cancellation, copy or rename failures remove operation-owned staging
and reservations, preserving all existing published artifacts. Once rename
succeeds, cancellation is no longer accepted: directory-fsync or later cleanup
failure yields a successful published result with `durability_uncertain` and the
backup ID. The artifact is retained; callers must not interpret that outcome as
an unpublished failure or retry blindly. Filesystems/hardware that do not honor
fsync cannot provide power-loss durability guarantees.

## State, enumeration and downloads

`BackupProgress` and `BackupResult` are immutable. They carry an independent
random operation ID, phase/timestamps, copied pages/estimated bytes and copy
percentage where available. Terminal progress and results expose a stable code,
safe summary, published ID, final package size, database SHA-256 and package
SHA-256. Percentage describes copying, not overall verification/publication.
Phases are `preflight`, `backing_up`, `validating`, `packaging`, `publishing`,
`complete`, `failed`, `cancelled`. Progress is delivered to an in-memory observer;
there is no persistent job state, polling route, scheduler or import-time work.
Observers are notification-only; expected observer errors are logged by type
without values, while cancellation uses its separate hook.

`list_backups(backup_root)` returns immutable `BackupEntry` records. Only generated
managed IDs directly under the published root are candidates; staging, lock
files, foreign names and zero-byte unpublished reservations are ignored. Each
candidate is opened safely and fully validated. Valid rows contain size,
timestamp, manifest provenance, actual compatibility and database/package
checksums. A corrupt, unreadable or incompatible candidate yields its own safe
invalid validation state rather than breaking the list; metadata from invalid
rows is untrusted. The list never repairs, rewrites or prunes artifacts.
Validation uses private extraction and may update access times, as above.

`resolve_backup_download(backup_id, backup_root)` is a **context manager**, not a
path-returning function. It validates the safely opened regular file, hashes it,
checks its entry/permissions/link count again and yields `BackupDownload` with a
rewound **read-only open handle**. HTTP code must stream that same handle
within the context, using the generated filename. It must not reopen a returned
path. Replacing the directory entry later cannot redirect that pinned handle to
an unvalidated file. Symlinks, hard-link aliases, nonregular files, arbitrary
paths and corrupted packages are refused. Slice 5 adds the authenticated route.

## Deferred work

Automatic crash-debris collection, bare database import and WAL support remain
deferred. Slice 5 adds native restore-request/restart coordination, authenticated
Admin integration, uploads, progress polling and retention acknowledgement.
Only explicitly queued confirmed plans are considered for startup application.
Database-only artifacts make no promise of media/archive consistency.

## Slice 3: restore staging, without application

`app.database_restore` provides synchronous backend APIs:

- `stage_restore_from_backup(backup_id, bound_target_path, ...)`
- `stage_restore_from_stream(stream, bound_target_path, ...)`
- `get_restore_plan(operation_id)`
- `revalidate_restore_plan(operation_id, bound_target_path=None, ...)`
- `confirm_restore_plan(operation_id, token, bound_target_path=...)`
- `cancel_restore(operation_id)`

These staging APIs do not apply a restore. No staging path opens the active DB contents, migrates
it, replaces it, starts a restart, changes retention, or exposes HTTP routes.
Target checks use directory handles and stat calls only, retaining the online
POSIX-lock safety established in Slice 2. The initial target argument is supplied
by trusted runtime code from its already-bound absolute path; it is never an
upload/route path selector and does not trigger configuration loading.

Managed backup input uses the existing validated download boundary, with its
validation runner replaced by the resource-limited worker described below. Only
trusted, pinned package handles are copied. Uploaded input is a caller-owned
binary stream/file handle: passing a `str`/`Path` server filename is refused,
and a stream's user-facing filename is ignored. Fixed generated internal names
are used for all artifacts. Input is streamed in 64 KiB chunks with the existing
whole-package limit enforced on actual bytes. The caller retains stream ownership.

Every operation exclusively creates a random opaque `restore-<32 hex>` directory
under `.restore/`; directories are `0700` and files `0600`. Owned no-follow
directory/file descriptors, regular-file checks, one-link checks and permissions
protect every re-open. A per-operation flock serializes staging/revalidation,
confirmation and cancellation. Different operations may stage concurrently;
only initialization of the private authentication key takes a short bounded
catalog lock. Existing plans/artifacts are never overwritten by name collisions.

The service first atomically records `staging`, captures/fsyncs `package.zip`,
records `validating`, then runs the Slice 1 package validator. That validator
checks structure, manifest, actual size/SHA-256, #52 quick/schema/history
inspection and supplemental full integrity. Only after those checks succeed
does it retain `candidate.db`. The candidate is not migrated. `current` is
accepted; `upgradeable` and recognized `legacy` additionally require #52's
explicit `automatic_upgrade_supported`. Newer, invalid, unknown, foreign and
WAL-format inputs are rejected. No independent compatibility registry exists.

Candidate and package files are flushed/fsynced before a `ready` record can be
published. Incomplete `staging`/`validating` records are never returned as valid
previews, including after process death. Original managed packages are preserved.
Rejected/failed/cancelled records cannot authorize later application.

### Immutable preview and confirmation

`RestorePlan` contains operation/source backup IDs; captured package SHA-256;
candidate size/SHA-256; opaque target binding plus device/inode; compatibility;
detected and latest supported migration ID/sequence; migration-required and
automatic-upgrade flags; manifest WAMF/SQLite provenance; creation timestamp;
the fixed media warning; and a confirmation token. Public plans contain no raw
target path. The exact absolute bound path and parent identity remain only in
the authenticated private operation record.

Confirmation is an HMAC digest with a separate purpose namespace, covering the
exact plan, target path/device/inode/parent identity, package/candidate digests,
staged file identities, provenance and migration decision. The operation ID and
timestamp prevent cross-operation token reuse. Ordinary live target writes do
not change target identity; target replacement, aliasing or a different bound
runtime path invalidates it. The token is a binding mechanism, **not Admin
authentication or CSRF protection**.

`get_restore_plan()` reconstructs an authenticated **cached preview** and checks
the staged file identities; it is not an authorization to apply. Confirmation
always revalidates. Revalidation safely reopens both artifacts, verifies their
identities and hashes, repeats full package/SQLite checks in a private scratch
copy and compares the complete migration preview against the original. It then
checks original hashes/identities and target identity again. Changes revoke the
plan/confirmation; the service does not silently bless tampered input. A fresh
stage/preview is needed. Wrong/missing confirmation tokens also fail closed.
Confirmation requires the caller's current bound target path, even though a
diagnostic revalidation can use the authenticated stored path. Future apply
**must** repeat this check under exclusive lifecycle ownership; a previously
confirmed record alone will never be sufficient.

### Durable state and cleanup

`operation.json` is a bounded, canonical JSON envelope authenticated by the
separate private 32-byte `.record.key`. Records contain metadata only: no secrets,
raw SQL or database rows. They include phase/code, source ID/digest, candidate
and package identities, internal target binding, plan, creation/update times,
confirmation state/time and safe package validation code. The key itself is
never included in records/plans/logs. Preserve this key with operation metadata;
loss/change makes old records unverifiable, rather than implicitly trusted.

Writes use an exclusive generated temporary file, file fsync, atomic replace
and directory fsync. A confirmation's phase, plan and confirmation timestamp are
committed together. Metadata lives outside `database.db`, so later DB replacement
cannot erase the restore operation. Corrupt JSON, duplicate fields, modified
metadata, symlink/hard-link aliases and unknown record/state formats fail closed.
Filesystem errors, including metadata fsync failures, are reported as failure;
callers must not assume a durable transition succeeded after an I/O error.

Staging phases are `staging`, `validating`, `ready`, `rejected`, `confirmed`, `cancelled`
and `failed`. Application adds the states described below. Cancellation writes
durable revocation **before** deleting only fixed operation-owned package,
candidate and extraction/scratch files. Metadata/lock files remain as a cancelled
record. Repeated cancellation can finish cleanup after an interruption. Busy
operations require their cancellation hook; cancellation never races an active
worker. Unknown future application-owned states are refused without deletion.
Automatic collection of abandoned operation directories/metadata temporaries
is deferred; incomplete records remain unusable and can be explicitly cancelled.

### Untrusted-input resource boundary

All untrusted ZIP/SQLite parsing runs in a fresh owned interpreter using inherited
managed file/directory handles. The worker receives **no active target path or
descriptor** and has no migration/application API. It reuses Slice 1 validation
and #52 inspection, never loads SQLite extensions or executes uploaded SQL.
Its extraction is inside the operation directory; validation never falls back
to a configuration-selected or live storage path.

The parent enforces an overall monotonic deadline (default 120 seconds, maximum
300), checks cancellation during streaming/polling, and kills/reaps the worker
on timeout/cancellation. Linux parent-death handling and parent PID checks reuse
the existing backup probe pattern. Worker limits are 512 MiB address space,
bounded CPU time, 128 descriptors, no core dumps and per-file size bounded by the
v1 package limit; streamed DB extraction still has the tighter 1 GiB DB limit.
Worker failure, resource exhaustion or malformed worker output never produces
a ready plan. The subprocess shares the service account's trust boundary; it is
not an OS filesystem sandbox.

Stream reads are cooperative: arbitrary caller-provided Python `read()` methods
and filesystem syscalls cannot be forcibly interrupted in the parent. Future
HTTP code must enforce transport/request read deadlines and authentication/CSRF
before invoking this boundary. Slice 5 adds the upload route described below.

### Database-only media warning

Every plan explicitly states that snapshots/clips are not restored, older DB
references may point to missing media and newer media may become orphaned.
Destructive retention must later be held until acknowledgement after application.
Staging records this requirement; offline application installs the hold below.
Retention operational state is never used as restore/scheduler state.

## Slice 4: explicit offline application

**A live restore has not yet been exercised on Bird-Lab.** All application,
rollback, crash and lifecycle tests use synthetic temporary databases. No Admin
or automatic restart/apply path exists. Do not run this maintenance boundary
against the live database until the maintenance procedure is explicitly chosen.

### Lifecycle exclusion

`exclusive_database_lifecycle(bound_path)` is the required caller-owned context.
It yields an active, context/PID/thread-bound lease backed by a nonblocking
exclusive flock on the persistent adjacent `<database>.lifecycle.lock`. It is
separate from SQLite transactions, retention/media guards and the backup guard.
Exiting invalidates the capability; passing an expired or different-target lease
cannot authorize application. PID checks alone are not a safety claim.

Supported consumers take shared leases:

- Native `speciesid.py` holds a supervisor lifetime lease inherited by owned
  children, covering Flask, MQTT/classification, health, metadata and scheduler
  work even between database calls. Children/managers must be joined before
  restart; lingering inherited leases safely prevent offline ownership.
- Explicit standalone Flask initialization retains its lifetime lease on the app.
- Retention service/CLI hold leases across their whole maintenance operation,
  including media work between database connections.
- `connect_db()` holds a shared lease until the SQLite connection actually
  closes; online backup holds one throughout creation.

The supervisor enters startup with exclusive ownership, reconciles restore state
**before** `ensure_schema()`, runs normal schema initialization under explicit
maintenance access, then downgrades to shared runtime ownership. Native startup
records success after configured owned children have been started; remote
MQTT/Frigate readiness remains normal runtime diagnostics, not a requirement
for local maintenance. Standalone initialization follows the same DB gate.
Startup failures are recorded before releasing lifecycle ownership.

Maintenance connections also count as consumers. An owner cannot apply/downgrade
while one remains open. If a connection escapes its maintenance context, its
kernel lock remains held until that connection closes. Persistent lock files
must never be unlinked as an unlocking mechanism.

This is cooperative exclusion for the supported WAMF entry points. Stop external
SQLite tools, old WAMF versions and other nonparticipating processes explicitly;
an advisory lock cannot exclude a program that ignores it. Application additionally
holds the managed-root backup guard. Only its verified rollback creation can
re-enter that guard using the explicit exclusive owner; ordinary recursive or
parallel backup creation remains excluded.

### Preparation and rollback preservation

`apply_restore(lease, operation_id, confirmation_token)` requires a confirmed,
unapplied operation. It authenticates the record, revalidates the full staged
package/candidate through the isolated Slice 3 boundary, verifies confirmation,
current bound target identity, schema and full integrity. Normal pre-maintenance
row writes do not invalidate the inode/path binding; the current contents are
captured afresh for rollback. Identity/schema/integrity changes refuse application.

The active DB must be healthy/current, owned by the service account, and free
of journal/WAL/SHM sidecars. Even a persistent cold journal is conservatively
refused. No checkpoint, journal deletion or automatic recovery is performed.
Offline raw hashing is safe here because all supported connections are excluded
and verification connections close before hashing.

A generated `0700` `.wamf-restore-<random>/` directory beside the target holds
the disposable `working.db`, on the same filesystem. The preserved candidate and
original package remain unchanged. Eligible older input is migrated **only** by
`ensure_schema(explicit_working_path)`. All connections close before reinspection,
full integrity checking and final digest/size capture. Target uid/gid/mode are
preserved on the replacement file; the working directory stays private. Failed
migration only removes disposable working files and records failure.

Immediately before replacement the service creates a fresh verified managed v1
backup of the **current active DB**, using the SQLite backup API and explicit
offline ownership. Package/self-validation, file/directory fsync and subsequent
download validation must succeed; durability warnings block replacement. The
rollback package ID, package SHA-256 and copied DB SHA-256 are authenticated in
the operation record. SQLite backup can change database-header bookkeeping, so
its database digest is the actual rollback payload digest, not an assumed raw
source-file digest. The frozen active file has a separate identity/digest for
crash reconciliation. No active filesystem copy is used for rollback.

### Replacement and crash journal

The authenticated per-target head is persisted before preparation is claimed.
It points to the operation independently of the active DB and blocks ordinary
consumers while offline work is unresolved. Operation records retain original
identity/digest, working identity/digest, rollback references, migration result,
timestamps, replacement outcome, startup evidence and durability status.

After working verification and rollback publication:

1. Persist the media hold and `rollback_ready`.
2. Persist `replacing`, including expected old/new file identities and digests.
3. Recheck active identity, digest, schema, integrity, sidecars and target parent.
4. Atomically `os.replace()` the same-filesystem working file into the target.
5. Fsync the target directory; persist `replaced`, then `startup_pending`.

No source sidecar is blindly removed. Pre-replacement failures preserve active
contents and both staged artifacts. Successful rename followed by metadata or
directory-sync failure is recovery-required, never reported as an untouched
database. Rollback artifacts and operation evidence are retained. No failure
path automatically rolls back or applies the candidate a second time.

| Durable phase | Startup reconciliation |
| --- | --- |
| `confirmed` without an application head | Preview only; never automatically applied |
| `preparing`, `migrating`, `rollback_ready`, or `replacing` with exact old identity/digest | Clearly pre-replacement; mark failed, preserve staged input, old DB remains authoritative |
| `replacing` with exact prepared new identity/digest | Rename occurred before outcome write; record `startup_pending`, never reapply |
| `replaced`, `startup_pending` with verified new file | Continue startup validation without replacement |
| Known validated startup, followed by partial startup row writes | Preserve those writes; known new inode remains authoritative after integrity checks |
| `complete` | Validate identity/health; permit normal future #52 schema upgrades and ordinary row changes |
| Missing file, conflicting identity/digest, corrupt journal, or ambiguous outcome | Refuse normal startup; preserve DB/artifacts/evidence; require explicit offline recovery |
| `recovery_required` / recorded startup failure | Explicit operator recovery required, even if the DB looks healthy |

Before resuming old-DB runtime after a pre-replacement failure, the old DB's
authority is durably recorded. Later normal row writes therefore do not make
that old DB ambiguous on restart. Likewise successful DB startup validation is
recorded before consumers are released, so partial-startup commits are not lost.
Explicitly recorded startup failure still blocks normal restart and never
triggers rollback. Missing restored DBs cannot fall through to empty initialization.

`reconcile_restore_startup(lease)` only classifies/authenticates evidence.
`explicit_recovery=True` is a deliberate offline operator action permitting
reconciliation of a recovery-required record; it does not force an uncertain
state to pass, repair SQLite, or roll back. Startup success is recorded as
`complete`; failure is recorded outside the DB as `recovery_required` with a safe
startup-failure code. Application-owned records cannot be cancelled, including
failed records; staging cancellation's allowlist never expands implicitly when
future phases are added.

### Explicit offline rollback

Backend procedure, without Admin routes:

1. Stop the supervisor and every other consumer. Enter
   `exclusive_database_lifecycle(bound_path)`; failure to acquire means stop.
2. Inspect `get_application_status(operation_id)` and authenticate/reconcile
   evidence. An unresolved uncertain state is not permission to overwrite it.
3. For application, pass the original confirmed token to `apply_restore()`.
   For an established applied/failed-startup restore, obtain
   `offline_target_identity(lease)` and explicitly call
   `rollback_restore(lease, operation_id, expected_target_identity=identity)`.
4. Rollback validates its recorded package/checksum, prepares a current-schema
   working copy, and publishes another verified **recovery artifact containing
   the DB being rolled back from** before replacement. It records both sets of
   artifact IDs/checksums and follows the same intent/rename/fsync protocol.
5. A `startup_pending` outcome can enter the normal guarded startup path.
   `recovery_required` or a durability warning requires offline review first.

Rollback is never automatic: workers may already have committed data. Both
operations require a healthy, preservable current DB; corrupt/missing DBs and
unsupported journals require separate explicit recovery rather than destroying
the only remaining evidence. New rollback/restore attempts do not prune old
managed artifacts. Cleanup of abandoned disposable directories is deferred.

### Media hold and results

The per-target authenticated head stores `retention_hold`, outside the DB and
separate from `retention_status`. It is set **before** rename and survives
replacement/restart/rollback. `retention_hold_active(bound_path)` fails closed
for corrupt control metadata. A failed attempt after the hold was installed
may conservatively leave it set even when the old DB is retained.

The minimal retention-service guard returns `outcome='held'`, skips all phases,
reports the restore hold without deleting media, and leaves observational
retention state unchanged. This also blocks orphan deletion against newer media.
System Health provides an explicit authenticated acknowledgement (described
below); there is no automatic hold release. Startup can serve/classify normally
while this separate hold remains active.

`ApplicationResult` / `get_application_status()` expose final phase, safe code
and message, replacement outcome (`None` when unknown), rollback ID/checksum,
final restored DB digest, migration flag, durability/recovery flags, hold status
and recovery-artifact references from explicit rollback. Raw paths and SQL/data
are not presentation messages. Operation state is authenticated and atomically
written/fsynced; metadata storage failure itself requires conservative recovery.

See [Database migrations and inspection](database-migrations.md) for #52's
compatibility definitions and migration ownership.

## Slice 5: Admin workflow and native restart coordination

Open **Admin → System Health → Database Backup & Restore**. Enabled Admin
authentication and an authenticated browser session are mandatory for every
database endpoint, including enumeration, status and download. API tokens do
not replace this session requirement. With authentication disabled, the panel
explains the requirement and exposes no actions. Every mutation uses the existing
CSRF mechanism. No additional navigation item or application version change is
introduced.

### Backups and restore preview

**Create backup** starts an explicitly owned background job and shows its phase
and copy percentage where available. The catalog shows time, package size,
observed schema, application provenance and invalid-artifact status. One damaged
artifact does not prevent listing others. **Download** validates the package
again and streams its pinned read-only handle as an attachment. Manual backups
are never automatically pruned.

Select **Validate for restore** beside a managed backup, or expand the restore
section and upload a v1 package. Uploaded filenames are ignored. The request
limit is the v1 package bound plus 64 KiB of transport overhead; streamed payloads
still enforce the package bound. Multipart input accepts the `package` field
with at most four form parts. Generated private names and existing containment,
link, permission and ZIP checks apply. Upload reception has a 120-second budget;
native Werkzeug also enforces a 30-second socket idle timeout. Other WSGI/proxy
deployments must enforce their own transport read deadlines. The existing
deadline/cancellation/resource-limited worker isolates untrusted ZIP/SQLite
validation, including catalog/download validation. SQLite extensions and uploaded
SQL execution are never enabled.

The preview shows actual compatibility, detected/latest migration, whether a
working-copy migration is needed, provenance and the database-only/media warning.
Current, upgradeable and explicitly eligible recognized legacy databases can
proceed. Newer, invalid and unknown databases cannot. Selection/upload alone
never confirms or applies a restore. Check the destructive confirmation and use
**Restore and restart** to submit the exact plan token. Standalone Flask can
validate and create backups but cannot request restore application without the
native supervisor restart capability.

The browser stores only opaque operation/job IDs and the old restart instance
ID for polling. Refresh reads state; it never repeats a POST. Progress jobs are
bounded and in memory; completed restore plans and application state survive
process restart. Failed/rejected validation reports safe codes without paths,
SQL, row contents, secrets or tracebacks. Staging can be cancelled before the
request becomes pending; pending/application-owned work cannot be cancelled by
the staging route.

### Controlled application and startup

The request route revalidates the confirmation and bound target, checks saved
configuration agrees with the runtime target, then atomically authenticates a
single global request under `.restore/requests/`. It queues only: it cannot
replace the database. The root request lock prevents competing restore requests;
an identical pending request is idempotent and sends no second restart signal.
No remote MQTT/Frigate readiness probe is required to request local maintenance.

The native sequence is:

1. Stop/join workers and listeners using the existing supervisor restart flow;
   release inherited database-consumer leases on re-exec.
2. Bootstrap and resolve canonical config and the bound database path.
3. Acquire exclusive database lifecycle ownership and reconcile authenticated
   restore/crash evidence. Any remaining consumer blocks exclusive ownership.
4. Claim a pending confirmed request durably, then apply it exactly once using
   Slice 4's working-copy migration, verified rollback artifact and atomic swap.
5. Validate/migrate the resulting active DB through normal `ensure_schema()`.
   Persist the successful database-startup boundary before releasing consumers.
6. Record the startup event, run diagnostics, then start Flask/MQTT/retention.
   Record completion when normal startup succeeds.

The request is marked `claimed` before application and `consumed` afterwards.
A crash after claiming never automatically replays application. Existing
application evidence is reconciled; a claim with no application evidence becomes
an interrupted failed operation. An incompatible target or corrupt/uncertain
recovery record refuses normal startup before workers. Ordinary restart follows
the same prior behavior when no restore request exists.

Application phases include preparing, migrating, rollback_ready, replacing,
replaced and startup_pending. The Admin projection additionally shows
pending_restart/applying. Completion, failed, cancelled and recovery_required
remain durable operation outcomes. If startup fails after replacement, the
restored DB and rollback artifact remain preserved, failure is recorded outside
SQLite, and startup is blocked until explicit offline recovery. There is no
automatic rollback or retry. If Flask cannot start, inspect the authenticated
operation/request records and process logs offline rather than relying on polling.

### Retention acknowledgement and recovery

After restore, System Health prominently reports that destructive retention is
held: snapshots/clips were not restored, older DB references may lack media,
and newer files may appear orphaned. Review media and saved retention policy
before checking the warning and acknowledging. This authenticated, CSRF-protected
action binds to the exact active completed restore, durably clears only its
restore-related hold, records the acknowledgement timestamp outside SQLite and
adds a system audit event. It never changes `retention_status` history. Retention
may execute saved destructive policy only after the hold is cleared. Pending
restore requests and incomplete/recovery-required startup block acknowledgement.

There is deliberately no rollback button. For recovery_required, stop all
consumers, preserve the current DB and all operation/rollback evidence, and use
the explicit offline rollback procedure above. Never delete recovery metadata,
sidecars or the DB to force startup. Rollback itself preserves the database being
rolled back from and requires the same exclusive lifecycle and atomic protocol.

### Disposable validation before a real restore

Live Bird-Lab restore and rollback have **not** been exercised. First use a
separate checkout/deployment, synthetic database, private config/storage and media
roots, an isolated port and MQTT topic/broker (or fake workers). Do not point it
at Bird-Lab database, media, secrets or backup roots. Enable Admin authentication.
Create a backup, add a distinctive synthetic row, stage the backup, refresh the
preview, confirm and observe one controlled restart. Verify the row disappears,
the operation completes, the verified rollback artifact exists, and retention
remains held across another restart. Review and acknowledge the hold; verify
retention resumes only then. Repeat with an eligible older synthetic package,
newer/corrupt rejection, interrupted startup, and explicit offline rollback in
that disposable deployment. Preserve a separately verified external copy of the
real database before any later authorized Bird-Lab restore rehearsal.

The disposable native supervisor/HTTP/browser rehearsal has passed; see the
[validation record](database-backups-validation.md) for scope and evidence.
Separately authorized live Bird-Lab restore/rollback validation remains unperformed. WAL packages, bare
DB imports, media restoration, automatic pruning and automatic rollback remain
outside Kingfisher's supported workflow.
