# Database migrations

`app.db.ensure_schema(path)` is the existing initialization API. It now runs the
ordered `MIGRATIONS` registry and records completion in `schema_migrations`.
Database schema sequences are independent of the WAMF release, YAML config
version, and retention status record version. `PRAGMA user_version` is unused.

Latest schema: **007_review_grouping_model**. Version 006 remains an exact,
supported upgrade source; its adoption definition is frozen. Inspection and the
runner validate the schema belonging to the recorded sequence, including each
post-baseline migration's target before recording completion. Partial 007
structures are invalid, not repairable legacy schemas. See
[Review grouping model](review-grouping-model.md) for the additive tables,
relationships and repository contract. Backup/Restore uses this same versioned
inspection boundary without its own schema rules.

Each registry entry has a contiguous integer sequence, a stable
`NNN_descriptive_name` ID, and an upgrade function accepting the runner's
connection. Registry declaration order does not affect execution order.
Functions must use individual SQL statements, must not commit or roll back, and
must not change journal mode or perform filesystem/media operations.

The runner sets a five-second lock timeout, acquires `BEGIN IMMEDIATE`, then
inspects history and schema. All pending changes, including history insertion,
share one transaction. Validation or execution failure rolls back the entire
batch. Connections close on success and failure. This does not provide online
migration safety: callers must exclude other application activity.

## Legacy adoption

Old startup reconciliation wrote IDs 001-005 without using them to select SQL.
Consequently those rows alone cannot establish which historical changes exist.
Before `006_versioned_schema_baseline`, the runner accepts:

- An empty database, which replays the complete chain.
- Nonempty subsets of the audited WAMF application tables: `detections`,
  `system_events`, `species_info`, and legacy/native `retention_status`.
  This includes detection-only old installs and retention-only maintenance DBs.
- Missing detection media columns, independently or together.
- Missing `species_info.thumbnail_url`, query indexes, or the ledger.
- Missing rows among the recognized historical 001-005 IDs.

Every present table, constraint, default, and index must match a recognized
schema definition. SQL formatting, identifier quoting, and column order may
differ. Extra application columns, tables, triggers, views, indexes, or altered
constraints/index definitions cause rejection. A ledger-only database is not
recognized as a WAMF application database.

The runner safely reconciles the postconditions of 001-005 even if those IDs are
already marked. Existing history rows and timestamps are preserved; only missing
rows are inserted. Retention conversion retains the existing deterministic
legacy-row selection and native-state preservation behavior.

006 adds a missing thumbnail column and validates the complete resulting schema
before its history row is inserted. The commit establishes the transition to a
strict ledger: subsequent history must be a contiguous registry prefix. Missing
predecessors, unknown IDs, malformed IDs/timestamps, and duplicates are rejected.
IDs with sequences beyond this build raise `DatabaseNewerError`. Unsupported
schema shapes raise `SchemaMigrationError`; SQLite errors propagate after
rollback. No downgrade or destructive repair is attempted.

After adoption, only pending migration functions run. The current schema is also
validated on repeated initialization. When adding a structural migration, extend
schema validation for its supported source and target definitions; do not relax
legacy adoption to accept arbitrary shapes.

## Scope and testing

Production database initialization is owned by `speciesid.main()`. After native
configuration bootstrap, the parent loads and validates canonical runtime
configuration, resolves its database path from that snapshot, and calls
`ensure_schema()` once. The migration connection is committed/closed before
storage setup and runtime binding, the startup database event, diagnostic manager,
or any Flask, MQTT/classifier, or retention worker starts.

Imports of `webui` and `speciesid` perform no configuration loading, storage
creation, database opening, or migrations. Flask construction/blueprint
registration is inert. The former `speciesid.setupdb()` wrapper has been removed.

The parent passes its configuration snapshot and resolved path explicitly to
Flask and MQTT workers. Each binds existing DB path overrides for classifier
writes, routes, queries/metadata, health, and system events without migration or
YAML reload. This applies to initial workers and watchdog replacements under
both fork and spawn. Retention receives its existing configuration snapshot with
the same absolute database path; its scheduling semantics are unchanged.

Flask starts health monitoring only after binding; metadata work remains lazy
and can start only once requests are served. Saving configuration does not move
active database consumers to an uninitialized file: the new path takes effect
on supervisor restart. Dynamic path resolution remains available to unbound
library/test APIs.

For standalone/test Flask startup, call `webui.initialize_app()` (optionally with
a runtime configuration snapshot) before serving. This explicit API validates,
initializes the selected database, creates storage, and binds consumers.
Production children use `configure_runtime()` instead and never call this API.
The independent `retention.py` maintenance CLI retains its explicit offline
initialization; Core scheduler children do not invoke that CLI.

Configuration/path, schema/history, future-version, SQLite, and unexpected
initialization failures refuse parent startup with exit status 1. Process logging
records the failure category/type without exception values, SQL parameters, or
secrets. No startup database event, diagnostics manager, listener, or worker is
started on failure. Initialization is offline; a single Core supervisor per
database remains the supported deployment model.

Backup/restore belongs to #49. No journal/WAL or configuration changes are made.

Tests use synthetic temporary databases. The shared Flask fixture supplies its
temporary database path before importing the web UI. Do not test migrations
against `data/speciesid.db` or import the application with a configuration that
selects that database during development validation.

## Read-only compatibility API

Use `app.db_inspection.inspect_database(path, check_integrity=False)`. The path is
required and selects an existing regular filesystem file. Relative paths resolve
from the caller's current directory; symlinks resolve to their target, which is
reported as `result.path`. Configuration defaults, runtime DB bindings, SQLite
URI input, and `:memory:` are not alternative path selectors.

The result is a frozen `DatabaseInspection` dataclass. `compatibility` is a
`DatabaseCompatibility` string enum:

| Classification | Meaning | Automatic upgrade eligible |
| --- | --- | --- |
| `current` | Authoritative contiguous ledger and actual schema match the latest supported version | No migration needed |
| `upgradeable` | Validated older version; includes exact pre-006 Kingfisher schema with complete 001-005 history | Yes |
| `legacy` | Recognized historical schema, with absent/incomplete/unreliable pre-006 history or known missing elements | Yes |
| `newer` | Ledger claims a migration sequence beyond this build | No |
| `invalid` | Malformed/impossible history, incompatible known-looking schema, corrupt SQLite, or failed requested integrity check | No |
| `unknown` | Empty/foreign file, unrecognized ID without a newer sequence, inaccessible/missing file, or unsupported inspection format | No |

An empty database is deliberately `unknown` for compatibility/restore preflight.
Inspection never turns it into WAMF. Explicit fresh production startup through
`ensure_schema()` remains supported. A read-only file can classify as `current`,
`upgradeable`, or `legacy`; eligibility does not imply write permission.

Metadata fields:

- `history`: deterministic ID/timestamp order as a tuple of frozen
  `MigrationHistoryEntry` records; `migration_ids` exposes the IDs alone.
  Existing timestamps remain unchanged. Non-text damaged values are represented
  as `None`; an unreadable or malformed ledger definition yields no records.
- `recognized_sequence` and `recognized_migration_id`: trusted detected version,
  or `None` when no exact version can be established. Legacy labels are never
  treated as authoritative by taking their maximum. Exact pre-006 Kingfisher is
  recognized as sequence 5 only after checking both schema and complete history.
- `latest_sequence` and `latest_migration_id`: build support, independently of the
  inspected database and application/configuration/retention record versions.
- `migration_required`: `False` for current, `True` for upgradeable/legacy, and
  `None` when a migration decision cannot safely be made.
- `automatic_upgrade_supported`: structural eligibility for a pending upgrade;
  true only for `upgradeable`/`legacy`. Disk, permissions, invalid legacy data,
  concurrent activity, or I/O can still cause transactional migration failure.
- `newer_than_build`, `legacy_recognized`, and `unversioned`: separate flags.
  `unversioned` means the readable ledger has no records (including an absent
  ledger); it is `None` if that cannot be determined. `legacy_recognized` also
  covers the recognized pre-adoption Kingfisher version.
- `error_category`: an `InspectionError` enum instead of raw SQLite/error text.
  Categories distinguish empty/foreign databases, unknown IDs, invalid history
  or schema, corruption, integrity-check failure, missing/invalid/unreadable
  paths, locks, unsupported journal mode, and newer schemas. `sqlite_errorcode`
  is available when SQLite reported an error.
- `integrity_checked`: whether the requested `PRAGMA quick_check` completed.
  Schema/history inspection is always performed for eligible inputs, but data
  and index-page integrity is checked only with `check_integrity=True`.

Inspection opens SQLite with `mode=ro`, enables `query_only`, and reads within
one transaction. It never calls `connect_db()`/`ensure_schema()`, creates storage,
reconciles schema, inserts history, commits, or probes write permission. It
reuses the runner's registry, history reader/validator, and schema validator.
Invalid build registries remain programmer errors and raise
`SchemaMigrationError`; ordinary file/compatibility failures return results.

WAL-format files conservatively return `unknown` with
`unsupported_journal_mode` before SQLite opens them. A nominally read-only WAL
connection can create/update auxiliary files, as described in the
[SQLite WAL documentation](https://www.sqlite.org/wal.html#read_only_databases).
The inspector checks the documented [SQLite header format versions](https://www.sqlite.org/fileformat.html#file_format_version_numbers)
and does not checkpoint, delete sidecars, switch journal mode, or use
`immutable=1` to bypass locking on a potentially active diagnostics database.
WAMF's journal mode is unchanged. Staged files must be consistent/self-contained;
callers must exclude file replacement and journal-mode changes during inspection.
Locks use the same bounded five-second wait as migrations.

## Contract for #49 Backup & Restore

The #49 implementation of managed package storage, read-only validation and
online backup creation is documented in [Database backups](database-backups.md).
It reuses this inspection API; restore remains deferred. Maintenance callers may
supply an optional `checkpoint` to cooperate with cancellation/deadlines. It
does not change ordinary inspection's compatibility logic or default lock wait.
For online source inspection, #49 uses a separate owned interpreter: the
inspector's ordinary header-file close must not release POSIX locks belonging
to other SQLite connections in the calling application process.

No backup packaging, SQLite backup API, file replacement, restore workflow, or
Admin surface is implemented here. The future #49 workflow should:

1. Produce a stable, explicit staged SQLite file through its own consistent
   backup/staging mechanism; preserve the active database during validation.
2. Call `inspect_database(staged_path, check_integrity=True)`. Record/compare the
   detected `recognized_sequence`, `recognized_migration_id`, history, and
   legacy/unversioned flags with backup metadata. The actual database is
   authoritative; build `latest_*` values describe support, not backup identity.
3. Reject `newer`, `invalid`, and `unknown` results. Do not invoke migration as a
   probe for unknown inputs. A `current` result needs no migration.
4. For `upgradeable`/`legacy`, require `automatic_upgrade_supported`, make only
   the staged database writable, exclude runtime activity, then call
   `app.db.ensure_schema(staged_path)`. Handle migration failure by rejecting the
   staged restore; inspection eligibility is not a guarantee of write success.
5. Re-inspect the same stage with `check_integrity=True` and require `current`,
   matching the build's latest schema and successful integrity checks, before
   #49 considers replacement through its separate restore mechanism.

Store the application version and backup format version separately from schema
identity. Do not manufacture missing history, stamp 006 directly, downgrade a
future schema, or overwrite a newer/incompatible active database to make a
restore pass. WAL backup handling remains outside this inspection contract.

## Future migration authoring

- Append the next contiguous sequence and a new stable ID to `MIGRATIONS`.
  Never renumber or rewrite completed migration IDs/history timestamps.
- Use explicit connection statements and the runner's transaction. No internal
  commit/rollback, `executescript()`, journal changes, or filesystem operations.
- Preserve historical 001-006 source/target definitions and their adoption
  meaning. For a structural change, extend the shared validators for every
  supported source/target version, including validation at each migration step;
  do not replace the frozen adoption baseline with only the newest schema.
- Add fresh-chain, predecessor-to-latest, sequential upgrade, second-startup,
  rollback, data preservation, and inspection/runner-agreement tests. Verify
  inspection before and after upgrade, including unsupported future history.
- Keep migrations offline behind the parent startup barrier. Continue to reject
  unfamiliar schema shapes rather than opportunistically rewriting them.
