# WAMF Development Instructions

## Development environment

- On Bird-Lab, use `.venv312` for all WAMF Python execution and tests.
- Do not use `.venv311` for WAMF tests or application execution.
- Do not modify the development environment or dependencies unless the task requires it.

## Change discipline

- Keep changes focused on the requested task.
- Do not perform unrelated refactors or cleanup.
- Preserve existing configuration and API compatibility unless a task explicitly changes it.
- Do not commit, push, rebase, tag, or change branches unless explicitly requested.
- Never force-push.

## Validation

For implementation work:

1. Run the most relevant focused tests first.
2. Run the complete test suite before declaring substantial work ready.
3. Use `.venv312`.
4. Run `git diff --check`.
5. Report tests and validation performed.

Do not depend on live Frigate, MQTT, cameras, or Bird-Lab media in unit tests unless the task explicitly requires integration testing.

## Configuration

- Use the canonical WAMF configuration loading, migration, validation, and persistence boundaries.
- Do not introduce independent configuration readers/writers when an existing boundary is available.
- Preserve partial Settings POST semantics.
- Treat separated secrets carefully and never expose them in logs, tests, reports, or UI.

## Database changes

- Use the existing schema reconciliation/migration infrastructure.
- Schema migrations must be idempotent.
- Structural/data migrations must be transactional where appropriate.
- Do not record a migration as complete before the migration itself succeeds.
- Preserve valid existing data unless the task explicitly requires otherwise.

## Retention architecture

Keep these responsibilities separate:

- Retention service: cleanup policy and execution.
- Retention state: durable observational state about completed/acquired executions.
- Retention scheduler: decides when automatic retention runs.
- Admin UI: configuration and presentation.

The A1 execution guard is authoritative for retention overlap.

Do not use retention operational state as configuration or scheduler state.

Automatic retention scheduling is owned by the WAMF supervisor and must not be started by Flask imports or routes.

One running WAMF Core supervisor per WAMF database is the supported deployment model.

## Process lifecycle

- `speciesid.py` is the production supervisor/lifecycle authority.
- Background services should have explicit supervisor ownership.
- Avoid import-time side effects.
- Child processes must participate correctly in shutdown and restart/re-exec.
- Do not make local maintenance services unnecessarily dependent on MQTT or Frigate availability.

## Code quality

- Follow the existing code style and project conventions.
- Do not silence warnings or lint findings merely to make checks pass.
- Ruff may be used for static analysis according to the repository's existing configuration.
- Application and test execution must still use `.venv312`.

## Reporting

At the end of implementation work, report:

- files changed;
- important implementation decisions;
- tests/checks run and their results;
- known limitations or concerns;
- whether any commit or push was performed.