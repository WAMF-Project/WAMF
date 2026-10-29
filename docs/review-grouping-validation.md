# Slice 2 manual Bird-Lab validation

Run this manually after deploying the reviewed changes. Automated tests use only
disposable DBs and fakes; implementation did not subscribe to or change Bird-Lab.

1. Use the existing #49 backup facility before deployment/startup. Confirm startup
   reports schema 007; this slice adds no further migration. Keep the current
   configured MQTT prefix and monitored feeder camera.
2. In System Health → MQTT connection details, confirm event subscription remains
   operational, review subscription is `subscribed`, and the configured review
   topic uses the saved prefix. A SUBACK with no review messages is not proof that
   Frigate is emitting them. API-only availability is a valid separate result.
3. Use the existing MediaMTX feeder loop. With the normal authorized MQTT observer,
   capture `<prefix>/events` and `<prefix>/reviews` evidence. Do not change Frigate
   tracking/review settings for this test. Record review ID and member IDs locally.
4. Inspect the WAMF DB with a read-only connection (for example SQLite CLI
   `-readonly`, with `PRAGMA query_only=ON`). Compare the following queries to the
   captured payloads, substituting the observed review ID as a bound parameter:

   ```sql
   SELECT review_id, camera_name, start_time, end_time, state,
          reconciliation_status, retry_count, last_error
   FROM frigate_reviews ORDER BY start_time DESC LIMIT 10;

   SELECT event_id, review_id, camera_name, label, start_time, end_time,
          classification_state, retry_count, last_error
   FROM frigate_review_events WHERE review_id = ? ORDER BY start_time, event_id;
   ```

5. Verify several tracked-object IDs accumulate under one review. Membership can
   first appear as sparse stubs, then hydrate without creating another event row.
   A repeated lifecycle payload should not increase row counts. `sub_labels` must
   not populate constituent species/classification results.
6. Allow Frigate to emit `end`. Confirm durable end time and `closing`, followed by
   reconciliation `synced` when API recovery succeeds. `closing` is correct in this
   slice: no worker may mark the review `finalized`.
7. Compare Recent Detections and notifications with the captured tracked-object
   `new` messages. Legacy immediate classification, per-event detections/sublabels
   and duplicate-looking feeder observations are expected to remain unchanged.
   Confirm newly inserted detections still have NULL `frigate_review_id` and no
   aggregate support metadata. Do not apply grouped-output acceptance criteria.
8. Replay and restart WAMF while a review is active. Verify durable members survive,
   the worker restores counts, and API reconciliation recovers missed end or member
   metadata. Repeated recovery must not create new user-facing detections.
9. If separately testing an API outage is appropriate, observe deferred work and
   bounded retries without inferred review end. Restore API availability and verify
   recovery. A quiet MQTT topic must not become unsupported merely through silence.

Record the Frigate version, actual API endpoints supported, review/member counts,
state transitions and any incomplete/conflict diagnostics. Do not include broker
credentials, URLs containing secrets or raw payloads in public reports.

Full grouped-output validation belongs after Slice 4. Terminal snapshot timing,
classification aggregation and child media preservation are not Slice 2 tests.

## Slice 3 manual terminal-evidence validation

Run this manually with the existing MediaMTX bird loop after reviewing/deploying
Slice 3. No live validation is performed by the automated test suite. Keep the
current feeder camera, classifier, threshold and Frigate configuration for a
meaningful comparison; use the existing backup facility before deployment.

1. Observe `<prefix>/events` and `<prefix>/reviews` with the normal authorized
   observer. Record one review ID and a few actual constituent event IDs. Capture
   each event's initial and later/final `snapshot.frame_time`, where present.
2. During the loop, query the DB through a read-only connection:

   ```sql
   SELECT event_id, review_id, label, end_time,
          classification_state, classification_phase, scientific_name,
          common_name, detection_index, score, reason, snapshot_time,
          snapshot_revision, classifier_model, retry_count, next_retry_at, last_error
   FROM frigate_review_events WHERE review_id = ? ORDER BY start_time, event_id;
   ```

   Confirm active rows start pending. End time must populate before terminal work
   runs; the parent review ending alone is insufficient. After work completes,
   check classified/rejected phase `terminal`, source/revision, loaded model and
   known frame time. Non-birds/false positives should be rejected without a model
   result. Missing evidence should retry, then fail explicitly if exhausted.
3. Compare one or two persisted `snapshot_time` values with their final MQTT
   snapshot frame times and current Frigate event metadata. Prefer the later/best
   terminal event evidence, even when its confidence is lower than the legacy
   early result. If the API omits frame time and no terminal hint exists, NULL is
   correct. Inspect the JSON source: a cached fallback must show its earlier
   evidence, never pretend it is the final frame.
4. Confirm the legacy UI still shows per-event cards, archives and notifications
   as before. Terminal work must not add cards, change legacy species/scores,
   publish extra notifications, or create grouped detections. Different accepted
   species in one review must remain distinct constituent results.
5. Restart WAMF with terminal work pending or waiting for retry. Verify the durable
   deadline is respected and work resumes. Then restart again after success:
   already-terminal rows must keep identical evidence and not run inference again.
   Recovery itself must not insert duplicate detections. An interrupted inference
   without a committed result may be recomputed; this creates no publication.
6. Check System Health's terminal counts against the DB. Confirm the parent review
   remains `closing`, not `finalized`, including after every constituent completes.
   Reconciliation `synced` does not yet mean aggregation has run.

The in-memory crop fallback is bounded and does not survive restart; do not expect
it to recover media already pruned by Frigate after a restart. No child-owned files
or full review clips are part of this validation. Grouped-output acceptance belongs
after Slice 4.

## Slice 4 manual acceptance (current behavior)

Do not apply the earlier Slice 2/3 expectation of unchanged per-event cards after
Slice 4 is deployed. Use a #49 backup first; schema remains 007. These steps are
manual and were not run against Bird-Lab by automated tests.

1. Run the known MediaMTX feeder loop containing Great Tit and Blue Tit with
   `<prefix>/events` and `<prefix>/reviews` visible. Before review end, inspect
   child terminal classifications: grouped rows must still be absent. With review
   capability unknown/degraded, WAMF waits rather than emitting fallback duplicates.
2. After review end and successful reconciliation/terminal work, run these read-only
   queries with the observed review ID bound to `?`:

   ```sql
   SELECT review_id, state, end_time, reconciliation_status,
          aggregation_rule_version, retry_count, last_error
   FROM frigate_reviews WHERE review_id = ?;

   SELECT id, display_name, score, support_count, detection_time,
          observation_end_time, frigate_event, wamf_snapshot_path,
          wamf_clip_path, aggregation_rule_version
   FROM detections WHERE frigate_review_id = ? ORDER BY display_name;

   SELECT event_id, scientific_name, classification_state, classification_phase,
          score, observation_id FROM frigate_review_events
   WHERE review_id = ? ORDER BY scientific_name, event_id;
   ```

3. Expect `finalized` / `review-species-v1`, one `Parus major` observation and one
   `Cyanistes caeruleus` observation if both have accepted terminal evidence.
   Each support_count equals that species' accepted track count; **it says nothing
   about physical bird count**, even at moments with several birds visible.
   Rejected/failed rows remain traceable and unlinked. Confidence should match
   the selected highest-confidence supporter, not an average.
4. Refresh Overview/Recent Feed and species/history pages. There must be no
   permanent constituent-card storm alongside the grouped rows for this review.
   Check representative snapshot/clip where available, correct real event links,
   existing API rendering, and compact finalization health counts. A transition
   review whose legacy rows already existed skips new grouped notifications;
   newly collected reviews notify once per accepted species after commit.
5. Restart after finalization: IDs/counts stay unchanged, state remains finalized,
   and notifications do not replay. Restart with terminal work pending: it resumes
   first and only then finalizes. A temporarily unavailable Frigate must defer,
   never finalize partial evidence or manufacture an end.
6. Run a single-species fragmented loop: many accepted tracks yield one species
   observation with support_count > 1. Run the multi-species loop again: separate
   reviews produce separate occurrences; two species in one review stay separate.
7. Optionally delete a grouped card through the existing authenticated route.
   Confirm suppression persists, child observation links clear, review/evidence
   remain, shared media survives, and restart does not recreate the card.
