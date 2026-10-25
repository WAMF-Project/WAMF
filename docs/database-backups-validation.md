# Kingfisher #49 final validation — 2026-10-25

The disposable native end-to-end rehearsal passed. **The live Bird-Lab database
was not restored or rolled back. No extra live backup was needed.** No commit or
push was performed as part of validation.

## Isolation and scope

The rehearsal root was `/tmp/wamf49-final-5eif1mai/`, created privately with a
separate copy of the current Python code, templates and assets in `checkout/`.
Its active database was `checkout/data/rehearsal.db`; fixed managed backups,
restore records and keys were therefore under that checkout's own
`data/backups/database/`. Configuration/secrets were in `config/`, snapshots and
clips in `media/`, models under `models/`, and all lifecycle/operation/retention
locks belonged to these disposable paths. The browser used loopback port 45299.
The configured MQTT namespace was
`wamf49-disposable-wamf49-final-5eif1mai`, with an unused loopback broker port.

The actual `speciesid.main()`, worker supervisor, signal/shutdown/join, OS re-exec,
Flask server, password login, CSRF, Admin routes, durable request coordinator,
exclusive lifecycle, SQLite backup, restore application, migrations/inspection,
rollback and retention backends ran. Only external consumer work was substituted:
MQTT/classification was an idle child process and external health monitoring was
disabled/stubbed. No broker, camera, model inference or production topic was used.
No auth or CSRF gate was bypassed. The launcher asserted previous worker PIDs were
gone before each new supervisor generation could start consumers.

Temporary launcher/driver/fault-injection scripts and credentials were kept out
of the repository. Evidence includes `evidence/report.json`, native JSONL/logs,
browser preview/hold screenshots, verified extracted disposable DBs, original and
rollback packages, and `evidence/crash-spots.json`. Credentials and confirmation
tokens are not reproduced here. All rehearsal services were stopped afterwards.

## Data and backup

The seed contained 12 detections, Robin metadata, a tagged rehearsal system event,
retention status, snapshot/clip references with actual disposable files, a local
taxonomy fixture, and all six migrations through
`006_versioned_schema_baseline`. Native startup added its normal audit event.

The authenticated online backup job published:

`wamf-db-20261025T111317963731Z-599df494e085f8d07e6e636b66f05870`

Package SHA-256:
`703e101b45c1836fee16188a91837baf3887a7365be7bde73a9398ac21d26fca`.

Enumeration, validated pinned-handle download, package/manifest/size/checksum
validation and SQLite integrity/current-schema inspection passed. The original
package remained unchanged after restore. Baseline per-table row counts and
SHA-256 digests were recorded. Rehearsal-tagged system events were compared
separately from normal startup/queue/retention audit events, which legitimately
change during an end-to-end restart.

Divergence added a thirteenth detection, another tagged event, changed species
description and changed retention `rows_scanned` from 12 to 99.

## Restore, retention and rollback

The browser logged in normally, rendered System Health/catalog, validated the
managed package and refreshed its preview without duplicating the operation.
The restore button required explicit confirmation; the backend also rejected a
request lacking destructive confirmation and a mutation lacking CSRF.

The exact preview token was sent through the production Admin request route.
The HTTP handler retained the active inode; the parent then received the normal
restart signal, stopped/joined both children and re-executed. Offline application
ran once before normal `ensure_schema()` and consumer startup. The operation
completed and detections, metadata, retention status, migration history and
tagged events matched backup-era checksums. Post-backup data was absent.

The verified rollback package contained the divergent pre-restore application
data. Another ordinary native restart retained the restored state and hold,
with no second restore application. The destructive retention backend returned
`held`, preserved a disposable orphan and left retention status unchanged.
The browser displayed the hold, required the acknowledgement checkbox, and
cleared it through the authenticated CSRF-protected route. Acknowledgement left
retention status unchanged. Retention then ran and deleted the disposable orphan.

After stopping every consumer, explicit offline `rollback_restore()` used the
validated rollback artifact and exact target identity. It preserved the DB being
rolled back from as another validated recovery package, atomically replaced the
target and passed normal native startup. Divergent application checksums,
integrity and current schema were recovered; operation state was complete.

## Startup failure and interruptions

A second queued restore injected a controlled exception after replacement but
before normal database startup validation. It produced `recovery_required`,
preserved the restored DB and valid divergent rollback package, and started no
workers. A subsequent real supervisor launch neither reapplied nor rolled back;
the restored file digest stayed identical and no workers started. The documented
explicit offline rollback procedure recovered the divergent data, preserved a
further recovery artifact and allowed successful native startup.

Separate disposable targets exercised actual process exits at these boundaries:

| Interruption | Reconciliation result |
| --- | --- |
| Before replacement, with durable replacement intent | Failed/recoverable; old DB authoritative and unchanged |
| After `os.replace()` but before outcome persistence | New DB recognized; startup completed without reapplication |
| `startup_pending` before normal validation | Startup completed without reapplication |
| After replacement followed by an unexplained write before validation | `recovery_required`; DB/evidence preserved and startup refused |

Each spot check recorded one application, preserved its rollback artifact and
installed a fail-fast guard proving reconciliation did not call application again.
These simulate process interruption, not power loss or filesystem hardware faults.

## Close-out review and limits

The full #49 diff was reviewed for schema-logic duplication, path and artifact
handling, startup ownership, live defaults, debug/temporary files and test-only
hooks. Compatibility/migration decisions still use #52; HTTP input uses opaque
IDs or bounded streams, pinned handles and private generated storage. Test
authentication conveniences remain in the isolated browser fixture, not in
production routes. No functional defect appeared during the rehearsal. Two stale
pre-restore docstrings were corrected and this evidence record was added.

Remaining limitations are deliberate: WAL packages, bare DB imports, media restoration,
automatic pruning/debris collection and automatic rollback are unsupported.
Rollback remains an explicit offline operation. Real broker/classifier/camera
integration and live Bird-Lab restore/rollback were outside this rehearsal.

## Final automated checks

All Python execution used `.venv312`; all database/package/media fixtures were
synthetic and disposable.

| Check | Result |
| --- | --- |
| Focused #49, #52 inspection/migrations, retention, startup/restart/shutdown | 552 passed |
| Complete Python suite | 1,903 passed in 161.92 seconds |
| Complete isolated Playwright suite, desktop/tablet/mobile | 246 passed |
| Ruff on all 29 changed Python files, compared with HEAD | No new findings; 42 existing findings |
| Ruff on backup/restore modules, dedicated routes/tests, process control and paths | Passed |
| JavaScript syntax: database tools and browser workflow tests | Passed |
| `git diff --check` | Passed |

The first restricted full Python invocation could not create multiprocessing
Manager IPC sockets (`PermissionError`). The complete suite was rerun with local
socket permission and passed without code changes for that environment failure.

No validation blocker was found for the agreed Kingfisher scope. The work is
ready for review and an explicitly authorized commit/push. This is not a claim
that a live Bird-Lab restore or power-loss test has been performed. Changes made
during this pass were documentation/docstring corrections and this validation
record; no feature or schema change was needed.
