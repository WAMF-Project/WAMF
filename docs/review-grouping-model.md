# Kingfisher review grouping: evidence and species occurrences

Migration `007_review_grouping_model` adds persistence only. Runtime slices build
review ingestion, terminal evidence and species finalization on that schema; see
the Slice 4 section for current output and notification behavior.
No application version change is required. Subsequent startup migrates through
the existing exclusive #52 startup barrier; development tests use temporary DBs.

## Meaning and ownership

A Frigate review is a camera activity interval, potentially containing several
animals/species. A constituent is one real Frigate tracked-object event ID. A
WAMF observation means a **species occurrence, not an identified individual
animal**. Conflicting accepted species evidence is preserved in separate species
observations within the review, rather than forced into a single global winner.

`detections` remains the observation table. Its integer IDs, event IDs, media and
historical fields are preserved. Historical rows receive NULL grouping fields;
there is no inferred membership, fabricated timing/support or time-window
regrouping. Historical duplicate event IDs remain legal. Existing public queries
continue selecting the original columns through `DETECTION_SELECT`.

## Additive 007 schema

Source of truth: `app/review_schema.py`; the 006 definitions in `app/db.py` remain
frozen. Frigate times, snapshot times and retry deadlines are Unix seconds (REAL).
Created/updated/suppressed timestamps are UTC SQLite `datetime('now')` text.
Observation timestamps retain the existing local ISO text convention.

| Table | Fields / purpose |
| --- | --- |
| `frigate_reviews` | `review_id` non-null text PK, camera, start/end, state, severity, reconciliation status, retry count/deadline/error, aggregation rule version, created/updated times |
| `frigate_review_events` | `event_id` non-null text PK, nullable review, camera/label, start/end, classification state/phase, scientific/common/category names, class index/score, reason, snapshot revision/time, classifier model identity, nullable observation ID, retry metadata, created/updated times |
| `frigate_review_suppressions` | `(review_id, scientific_name)` composite PK and suppression timestamp; survives observation deletion |
| `detections` additions | Nullable `frigate_review_id TEXT`, `observation_end_time TEXT`, positive-or-null `support_count INTEGER`, `aggregation_rule_version TEXT` |

An event's label and start time are nullable while it is a membership stub:
review `data.detections` provides IDs before object metadata may be available.
The camera is known from the review. Hydration fills missing facts without
clearing membership or known timing; classification requires hydrated metadata.

The partial unique index on `(frigate_review_id, display_name)` applies only to
non-null review IDs. There is no global event uniqueness on historical detections.
Indexes cover review state/retry deadline, event membership, classification
state/retry deadline and observation links.

`support_count` means distinct supporting constituent IDs, not animal count or
MQTT message count. An aggregation rule must assign its own version and aggregate
score; Slice 1 does not select or implement that rule. Review-wide support shares
can later be derived from the constituent evidence without duplicating totals.

## Small state vocabulary

- Review: `active`, `closing`, `finalized`, `deferred`, `incomplete`.
- Reconciliation: `pending`, `synced`, `deferred`, `incomplete`.
- Classification: `pending`, `classified`, `rejected`, `failed`.
- Classification phase: NULL before work; `provisional` or `terminal` afterwards.

Review lifecycle and reconciliation status are separate: an ended closing review
can be deferred for API reconciliation without losing its known end. Closing and
finalized require an end time; finalized also requires a rule version. Failures
and rejections require a reason; accepted results require species and score.
`failed` with a next retry deadline represents retryable failure. The later
worker owns scheduling, retry limits and deciding when evidence is incomplete.
Store bounded, sanitized reason/error codes, never raw URLs, secrets or payloads.

One authoritative classification is stored per event. Terminal evidence replaces
provisional evidence; earlier evidence is not another vote. This is not an
append-only attempt history. Model/revision identity records which evidence was
used. Terminal snapshot fetch failure can retain provisional evidence by leaving
the classification intact and scheduling recovery in a later slice. Classifier
exceptions and rejected/background/below-threshold outcomes can also be persisted
explicitly, including their phase and reason.

## Repository and integrity contract

`app/review_repository.py` receives an existing connection. It never opens a DB,
commits, rolls back, performs network/media work or starts a service. Every
mutation requires `conn.in_transaction`; callers start a transaction (`BEGIN`
or `BEGIN IMMEDIATE`) and own commit/rollback. `BEGIN IMMEDIATE` is recommended
for multi-step finalization. A failed helper must be handled by rolling back the
caller transaction before retrying the unit of work. Helpers work with ordinary
tuple rows or `sqlite3.Row` connections.

APIs:

- `upsert_review`, `get_review`, `set_review_state`, `list_open_reviews`.
- `upsert_event`, `get_event`, `link_event_to_review`, `list_review_members`,
  `list_pending_events`.
- `persist_classification` for accepted, rejected and failed evidence with
  provisional/terminal provenance and absolute retry metadata.
- `set_event_retry` schedules recovery without overwriting provisional evidence.
- `create_grouped_observation` and `link_event_to_observation` as transaction
  primitives; callers still own aggregation and complete finalization.
- `suppress_species` and `is_suppressed` as deletion/reconciliation primitives.

Upserts preserve known end times, keep the earliest start/latest end, and reject
camera/label mismatches. Review severity promotes to alert without stale demotion.
Duplicate facts do not change timestamps or add rows. Old open messages cannot
reopen an ended review; finalized reviews cannot regress through the state helper.

Relationships use **application-level checks, not foreign keys**. Slice 1 does
not change connection-wide SQLite FK policy. A member must reference an existing
same-camera review; it cannot silently move to another review. Observation links
must match review, camera and classified species. A representative must be a real
classified member of a closing review. Finalized membership and linked evidence
changes require a future explicit revision workflow. Grouped observation creation
is idempotent by review/species and refuses suppressed species. An existing row is
returned unchanged; this is not a revision/update API.

Raw SQL bypasses these relationship checks. Later ingestion, finalization and
deletion code must use the repository or preserve the same invariants. A deletion
transaction must write suppression, detach child observation links, then delete
the observation while preserving the review and desired evidence. The existing
deletion route is unchanged and no grouped observations are created by runtime
code yet. Review purge/cascades and revision behavior are deliberately deferred.

Example transaction (a temporary/test DB or a supervised runtime connection):

```python
conn.execute('BEGIN IMMEDIATE')
try:
    upsert_review(conn, review_id, camera_name=camera, start_time=start)
    link_event_to_review(conn, event_id, review_id)
    conn.commit()
except Exception:
    conn.rollback()
    raise
```

## Media and retention boundary

There are deliberately **no child-owned media path columns**. Snapshot revision
and classifier identity are metadata, not archived-file references. Existing
retention continues owning only detection snapshot/clip paths. A representative
tracked-event snapshot/clip is sufficient for Kingfisher; full review-duration
video is out of scope.

Before any future child evidence files or paths are enabled, extend the media
reference inventory, orphan scanning, expiration, deletion and crash recovery
together, using the existing media activity guard and authoritative A1 retention
execution guard. Otherwise pending evidence files could be deleted as orphans.
No files should be written to managed archive directories as child-owned evidence
under the Slice 1 model. Retention does not yet purge review/event/suppression rows;
their eventual lifetime policy remains future work.

## Migration, backup and deferred work

The 007 migration uses only SQL under the existing runner transaction and never
rebuilds detections or rewrites `sqlite_sequence`. Fresh databases replay 001–007;
006 upgrades add the new structures. Repeated startup changes nothing. Inspection
requires the exact schema for the ledger version: 006 is upgradeable; 007 is
current; malformed or partial 007 is invalid. Historical source/target validation
is preserved at each step. Backup/Restore #49 requires no schema special cases:
007 review state travels in the DB package, and a 006 restore migrates only its
working copy before replacement. Media is still outside DB backup packages.

Slice 2 ingests and reconciles review lifecycle and membership under supervisor
ownership (below). Classification work/retries, aggregation policy, notifications, media
preservation, finalization orchestration, revision handling and deletion UI are
not wired here. Rule versions are supplied by future callers, not stamped onto
historical rows or invented by this migration.

## Slice 2: MQTT ingestion and reconciliation

The supervised MQTT child subscribes separately to `<topic_prefix>/events` and
`<topic_prefix>/reviews`. Each subscription has its own MID/SUBACK diagnostics and
is reset on reconnect. Existing MQTT availability continues to describe the
legacy event consumer; review subscription and capability are separate. Retained
review messages are ignored before receipt/capability tracking. Retained event
filtering and its existing transport-receipt diagnostic are preserved.

Review `new`, `update` and `end` messages are defensively validated before writes.
End-first and update-first delivery are supported. Facts and explicitly announced
member IDs are merged, never replaced wholesale; old open messages cannot erase
a known end. End transitions toward `closing`, never `finalized`. Configured
cameras only are ingested. There is no timestamp-based grouping.

Review `data.detections` IDs create durable sparse stubs. Eligible bird event
`new/update/end` messages hydrate metadata while preserving membership, retry
deadlines and any existing classification provenance. A later sparse review does
not erase rich event metadata. Membership/camera conflicts do not move events:
reviews are marked reconciliation `incomplete`, and conflicting event rows retain
their existing membership with a bounded proposed-review diagnostic in
`last_error`. These conflicts require explicit future repair; ordinary replay does
not silently clear them.

The callback performs no reconciliation API calls, media downloads or additional
classification. Its preparatory writes use a short transaction and 50 ms SQLite
busy timeout. Payloads are capped at 1 MiB and 4,096 member/context entries; invalid
or oversized input is rejected rather than truncated. Invalid new review payloads
cannot partially modify prior state. Failed metadata writes are diagnosed and
wake recovery; event metadata failure does not suppress the legacy classification
path. API reconciliation can recover recent reviews and known event membership,
but MQTT QoS 0 still cannot guarantee capture of every message during an outage.

**Classification timing/output is unchanged in Slice 2.** Only the existing
tracked-object `new` dispatcher classifies immediately. `update/end` add metadata
without classification. Preparatory constituent `classification_state` remains
`pending`, even where the legacy path has inserted a detection. No grouped
detections, new notification semantics, terminal-snapshot classification, child
media or public count changes are enabled. Lifecycle updates deliberately do not
overwrite `snapshot_revision`, which denotes evidence actually classified.
The legacy event callback still performs synchronous classification/media work;
that can delay review MQTT dispatch. Independent API recovery compensates for
missed/delayed lifecycle messages without changing classifier timing in this slice.

### API and owned recovery work

`FrigateClient` supports `/api/review/{id}`, older `/api/review_ids`,
`/api/review/event/{event_id}`, `/api/review`, and tracked-object metadata lookup.
Responses have bounded size, request/read timeouts, closed streams, fixed safe
errors and no redirects. Missing lookup responses are distinct from unsupported
review endpoints. See the [Frigate API source](https://github.com/blakeblackshear/frigate/blob/v0.16.2/frigate/api/review.py)
and [MQTT contract](https://docs.frigate.video/integrations/mqtt/).

When direct event lookup is absent, interval lookup searches the event's actual
lifetime and requires its ID in `data.detections`. A full result page with no match
is `lookup_incomplete`, never proof of no membership. This bounded fallback does
not paginate exhaustive historical activity: it defers rather than invents an
association. Recent discovery checks up to 100 retained reviews per camera,
rotating configured cameras at 60-second intervals. A full discovery page is
diagnosed as truncated. This is discovery, not historical observation replay;
known older rows still receive direct ID-based recovery.

`ReviewReconciler` is an explicitly owned thread inside the supervised MQTT child,
created after classifier initialization and stopped on exit. No Flask import or
route starts it. It holds a database-consumer lease across each scan including
network calls, participating in #49 offline restore exclusion. TERM unwinds the
worker context; shutdown signals a stop event and joins for seven seconds. A
pathologically blocked daemon thread cannot outlive its process; the supervisor
has a ten-second MQTT-child shutdown grace and kill/reap fallback. No mutation
starts after the thread observes shutdown following network work.

Work comes from durable rows, with one in-memory wakeup bit. Each scan selects at
most four due reviews and four due events; duplicate MQTT deliveries do not enqueue
more jobs or reset unchanged rows' backoff. Restart reclaims durable work without
an in-memory running-job marker. Polling is five seconds; retry delays are 5, 10,
20… seconds, capped at 300, and survive restart. Successful open reviews are
checked again after 60 seconds. Missing end times are never fabricated from age.
Timeouts, unavailable media metadata/API objects and missing reviews leave durable
work deferred. A later successful reconciliation clears retry state. Scan/DB
failures are safely diagnosed and retried by the owned loop.

### Capability and health

System Health's existing MQTT details include review SUBACK state, last valid
review receipt, API/reconciliation state, active/closing/deferred counts, unmatched
events, backlog and a fixed diagnostic code. Counts reload from the DB after
restart. Only bounded ordinary diagnostic data crosses the existing supervisor
IPC; payloads, event lists and remote exception text do not appear in normal UI.

- `unknown`: no current evidence of review support.
- `available`: a successful review API response (including an empty list) or valid
  review MQTT observation, with a running reconciler.
- `degraded`: temporary reconciliation failure, incomplete/deferred reviews, or
  review subscription failure while API recovery is available.
- `unavailable`: review endpoints return explicit unsupported statuses and no
  current-worker MQTT review evidence exists. This describes the configured API
  connection, not an assertion of a specific Frigate version.

SUBACK alone and silence alone never prove capability or lack of support. Endpoint
support is re-probed after outages/unsupported responses. A working MQTT source
with unsupported API is degraded, not unavailable. The event-based output path is
preserved in every mode; grouped/fallback output decisions remain future work.

Review objects, verified objects, zones and `sub_labels` are validated context only
and discarded in this slice. In particular, WAMF-generated sublabels are never
reimported as independent species votes. No migration 008 is required; frozen 007
DDL is unchanged. Existing retry fields currently describe metadata recovery and
will need coordinated ownership when Slice 3 adds classification work.

See [manual Bird-Lab validation](review-grouping-validation.md). This workflow has
not been exercised against live Bird-Lab services as part of Slice 2 implementation.

## Slice 3: authoritative terminal constituent evidence

Slice 3 extends the existing owned review thread; it does not start another
classifier interpreter or background framework. The legacy MQTT `new` path still
classifies immediately, archives media, creates per-event detections and sends its
existing notifications/sublabels. It also remembers a bounded in-memory copy of
the crop it already fetched. It performs no extra image request or terminal
classification in the callback. Shared preprocessing remains RGB, 224×224 uint8;
one mutex serializes all access to the worker's loaded interpreter.

Eligible terminal work requires a configured camera, explicit review membership,
known label and a known event end. Unmatched events must first gain authoritative
membership through Slice 2. Active events cannot become terminal-ready merely
through review closure or age. Non-bird members receive terminal rejection
`non_bird` without invoking the bird model. Known terminal Frigate false positives
receive rejection `false_positive`, also without inference.

### Evidence and provenance

The existing 007 fields suffice; **007 is unchanged and there is no 008**.
While classification is pending, a bounded versioned JSON `snapshot_revision`
records safe MQTT hints: snapshot frame time, event frame time, terminal status
and boolean false-positive flag. New/update/end merge these facts in timestamp
order. Sparse later payloads never erase richer hints, provisional evidence or a
completed terminal result. Pending hints identify announced evidence; they are
not a classification or proof that an image has been downloaded.

`FrigateClient.acquire_terminal_image()` is the sole acquisition boundary. It:

1. Checks event identity, camera, label and completion against current event API
   metadata where available. An explicitly active API event defers work even if
   an earlier MQTT end is stored. Mismatched identity fails safely.
2. Uses the completed event's `/api/events/<id>/snapshot.jpg` with the existing
   crop=1 and quality=95 request. Current terminal snapshot metadata takes
   precedence over early MQTT hints. There is no documented separate URL for a
   timestamp-addressable best event snapshot, so the event endpoint is the image
   source; no camera-latest image or unrelated thumbnail is substituted.
3. If the object metadata API is unavailable/pruned, uses the durably observed
   event end and available terminal MQTT hints for the same event. A snapshot
   still must be retrievable and usable; missing metadata does not fabricate an
   image or a timestamp.
4. On the sixth acquisition attempt, if the endpoint still reports snapshot
   missing/pruned (404/410), permits the same event/camera's previously fetched
   legacy crop as a last resort. Network/authentication failures do not select
   cached fallback. If none is available, the bounded failure policy applies.

The process-local fallback cache holds at most 32 crops, 16 MiB total, and 2 MiB
per crop. It owns no files and is lost on restart or eviction. Restart recovery
therefore depends on Frigate's remaining evidence; an unavailable fallback is
never reconstructed from a legacy detection's species/score. Terminal downloads
are capped at 4 MiB with bounded request/read timeouts and closed streams.

Completed evidence provenance is deterministic JSON: event ID, image SHA-256,
requested crop/quality, source, metadata source and known snapshot frame time.
Fallback explicitly records `cached_legacy_fallback` and its earlier frame time;
accepted fallback also records reason `fallback_cached_snapshot`. It is never
presented as the final frame. The actual loaded model ID/version plus artifact
SHA-256 identifies `classifier_model`; custom models use their loaded content
hash. A later saved Settings choice cannot relabel running-model evidence.

`snapshot_time` may be NULL for classified evidence when Frigate omits it. It is
not replaced with event start, event end, fetch time or wall-clock time. The
repository's validation now permits that existing nullable schema field while
still requiring model/revision/score for classified evidence. Frigate 0.16's
stored event data does not always expose snapshot frame time; terminal MQTT hints
can provide it. Its completed snapshot endpoint can also ignore crop overrides
and serve the camera's saved snapshot configuration. These version differences
are handled without guessing a frame or introducing brittle snapshot URLs.
References: [0.16 event persistence](https://github.com/blakeblackshear/frigate/blob/v0.16.2/frigate/events/maintainer.py),
[snapshot API](https://github.com/blakeblackshear/frigate/blob/v0.16.2/frigate/api/media.py),
[snapshot selection](https://docs.frigate.video/configuration/snapshots/).

### Execution, retry ownership and recovery

The review thread processes at most four due terminal members per scan after
metadata/membership reconciliation. Once a member is linked and ended, terminal
work owns its retry fields. Metadata recovery cannot clear a `terminal:` retry
deadline or overwrite completed classification state. A newly completed/hydrated
member clears earlier metadata retries so its terminal work can begin.

- Success: `classified`, phase `terminal`, species/common/category/index/score,
  model and evidence provenance; retry count resets to zero and deadline clears.
- Background, score at/below the configured threshold, or no usable classifier
  category: `rejected`, phase `terminal`, with an explicit reason and available
  evidence provenance. Frigate sublabels are never used as a fallback vote.
- Missing/unusable snapshot or temporary model execution failure: retain pending
  evidence and persist `terminal:<safe-code>`, attempt count and retry deadline.
  Six total attempts use delays of 5, 10, 20, 40 and 80 seconds (the shared backoff
  helper is capped at 300 seconds). Deadline/count survive restart.
- Exhaustion or non-retryable failure: `failed`, phase `terminal`, reason and
  available attempt provenance. A temporary missing snapshot is never rejected
  as a species decision.

All committed terminal classified/rejected/failed rows are excluded from automatic
work and immutable through ordinary repository updates. Repeated end messages,
review updates and restart do not trigger inference again. A future explicit
repair/reclassification tool would need to deliberately revise such evidence.
The worker keeps up to four computed outcomes across a transient DB-write failure
so it can retry persistence without repeating inference in the same process.
A crash after inference but before commit can repeat uncommitted computation;
there is still only one durable authoritative result, and terminal work has no
external notification/publication side effects. No exactly-once CPU guarantee is
claimed across that unavoidable crash boundary.

The existing consumer lease covers acquisition, inference and writes, participating
in #49 exclusion. Shutdown checks prevent committing newly computed results after
stop is observed. The owning MQTT process retains its existing bounded shutdown
and supervisor restart policy.

### Aggregation boundary and diagnostics

Each event keeps its own result. Great Tit / Blue Tit / Great Tit remains exactly
those three constituent results. There is no review-wide winner, individual-animal
count, grouped detection insertion, terminal sublabel write or notification.
Legacy detections are neither read as votes nor updated with terminal results.

A closing review remains closing while constituent work is pending/retrying and
after all terminal work completes. Reconciliation `synced` describes membership
and lifecycle, not aggregation completion. Slice 4 must inspect every constituent's
terminal outcome before deciding what species observations to create; it alone
will own finalization. Existing System Health MQTT details add two compact rows:
terminal pending/retrying and classified/rejected/failed counts, reconstructed from
durable state. No payloads or event lists are exposed in those diagnostics.

No child-owned snapshots/clips or new media paths are created. Representative
event media and grouped-observation publication remain Slice 4/5 work; full review
duration video stays out of scope. Legacy history/count semantics deliberately
remain unchanged until grouped output is implemented.

## Slice 4: species observation finalization (current runtime)

The owned review worker now runs reconciliation, terminal classification, then
aggregation. Earlier slice descriptions above describe their deployment boundary;
Slice 4 replaces their temporary per-event output compatibility for review-capable
activity. Schema 007 is unchanged; there is no migration 008 or version change.

Eligibility requires an ended, non-finalized review with API-reconciled (`synced`)
membership, no membership/camera conflict, and hydrated, ended terminal members.
A pending/retrying member blocks finalization; failed and rejected terminal rows
settle the work but supply no species observation. Unresolved/pruned metadata
continues to defer rather than manufacture an end or partial observation.
An authoritative empty review, or one with only rejected/failed evidence, finalizes
with zero observations. `synced` alone is not evidence completeness.

`review-species-v1` groups accepted terminal **bird** rows by whitespace-normalized,
case-folded scientific name. Common names and review sub_labels never vote.
Canonical display_name is the lexical first whitespace-normalized scientific
spelling among supporters, independent of arrival order. Ordinary model labels
therefore retain their original scientific spelling. Every distinct accepted
species receives one observation, including conflicting Great Tit/Blue Tit tracks.

The representative is chosen by descending terminal score, descending known
snapshot timestamp (unknown last), ascending event start, then lexical event ID.
Confidence is that representative's score, never an average or a combined
probability. Start is the earliest accepted support's event start; end is the
latest accepted support's event end, using the application's existing local ISO
timestamp convention. Finalization wall-clock time does not become observation
time. `support_count` counts accepted constituent **tracks, not animals**: both
fragmentation and simultaneous same-species birds can increase it. No bird_count
is inferred. Representative frigate_event remains a real event ID.

All species rows, supporter observation links, exact-member legacy replacement,
and review state/rule/retry reset commit in one SQLite transaction. A second-species
failure rolls back the first; restart retries safely. The existing partial unique
index remains the last insertion guard; repository comparisons normalize species
identity. Existing partial rows can be completed before finalization. Finalized
reviews are stable and skipped, including after deletion. Grouped deletion now
records durable suppression and detaches constituent links without deleting the
review or evidence. A future explicit reconciliation revision must honor suppression.

### Output transition and publication

With the supervisor-owned review worker present, unknown/available/degraded review
capability defers event output to review finalization. Silence and SUBACK never
enable permanent per-event output. Confirmed unsupported review APIs with no known
reviews retains the legacy immediate path. Standalone legacy/test entry points
remain usable. Review recovery and terminal inference remain independent of
notifications; transient unavailability does not switch to event output.

Pre-Slice-4 legacy rows for exact authoritative member IDs are removed only in the
successful finalization transaction. Unrelated historical NULL-review detections
are untouched: there is no historical time-window grouping. Such transition reviews
skip grouped publications because their legacy events could already have notified;
external notifications already sent cannot be retracted. New review activity sends
one ordinary detection MQTT publication and Bridge observation per finalized species,
plus the existing first-species publication when applicable. Only representative
event sublabels are updated, never a review-wide winner. Terminal-only nonrepresentative
tracks retain their durable provenance without extra publications.

Publication remains best-effort **at-most-once attempt after commit**, matching the
existing commit-then-post architecture. A crash between commit and publication can
lose a notification; restart never replays it. Reliable delivery/outbox semantics
would require a separate explicit design, not an unrecorded replay. Publication
failure does not undo an observation.

### Detection-owned media and query compatibility

The media guard spans representative acquisition through DB reference commit and
cleanup. Existing representative snapshot/clip paths are transferred to the grouped
row. New representatives use atomic WAMF archive files named by their real event ID:
one bounded streaming attempt per media type, 2-second connect/read timeout,
5-second stream budget, 4 MiB snapshot / 64 MiB clip limit, and shutdown checks.
Unavailable media remains NULL as in existing detection semantics; no full-review
clip and no child-owned paths are introduced. Classification image provenance is
separate from the representative archived media, which preserves normal Frigate
snapshot serving semantics. Unreferenced transition/rollback files are removed
only after checking both detection path columns; shared files are preserved.
Unlink failure/crash can leave a recoverable orphan for the existing retention
orphan scan. Archive references remain governed by existing retention policy.

Overview, Recent Feed, Activity, Daily Summary, Species Profile, Detection Results,
archive/analytics counts and the recent API still count `detections` rows, so they
naturally count occurrences after finalization. Integer IDs, real representative
event IDs, media serialization and species-name joins remain compatible. Polling
uses representative event IDs; an already open page can retain pre-upgrade cards
until refresh. No public visual redesign, support-count label or individual count
is added. System Health adds ready/finalized/failure counts and current grouped-row
count; deferred/incomplete reconciliation remains shown separately.
