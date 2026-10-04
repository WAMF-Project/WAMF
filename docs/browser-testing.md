# Browser QA

Playwright is **development/QA-only**. Node and Chromium are not required on
deployed WAMF systems. Nothing here starts WAMF or changes Python dependencies.
Use Node 22 or newer (Bird-Lab currently uses Node 22).

From the repository root:

```sh
npm ci
npm run browser:install       # Chromium only; does not install system packages
npm run test:browser
npm run test:browser:headed   # requires a graphical display
npm run browser:screenshots
```

Tests target the already-running UI. The default `http://localhost:7767` was
verified against Bird-Lab's canonical runtime configuration and HTTP response.
Set `WAMF_BASE_URL` for a different instance, including its scheme and port.
There is no Flask import, server startup, or direct configuration access in Node.

## Admin authentication

If redirected to login, set `WAMF_ADMIN_PASSWORD` in your local command environment
(avoid shell history), or reuse a manually authenticated session:

```sh
npm run browser:login
export WAMF_BROWSER_STORAGE_STATE="$PWD/test-results/browser-auth/session.json"
npm run test:browser
```

The login helper opens Chromium; log in manually and press Enter in the terminal.
It saves a mode-0600 session file. Existing compatible Playwright storage-state
files can also be supplied through `WAMF_BROWSER_STORAGE_STATE`; cookies must match
the target hostname. Session files are credentials: keep them local, never commit
or share them, and delete them when finished. No password or WAMF signing secret is
stored by this setup. Expired sessions need another login. If auth is disabled,
the helper proceeds without a password; the Admin sidebar checks expect the
authenticated Admin navigation used on Bird-Lab.

## Coverage and artifacts

Chromium projects cover desktop 1440×1000, tablet 820×1180, and mobile 390×844.
Checks cover service text, compact sidebar, mobile navigation, page overflow,
Retention wrapping/presentation and keyboard disclosure with visible focus.
Retention checks require a recorded attempt (as on Bird-Lab), but do not assert
machine-specific counts, timestamps, schedules, or healthy service states.
They read the real server response and never trigger cleanup.

Screenshots on failure and explicit System Health captures go under ignored
`test-results/browser/`, with separate project directories. The capture task
runs only `@screenshot` cases. Normal smoke runs also produce these captures.
Artifacts may contain operational information; review before sharing. Traces and
video are disabled to avoid capturing credentials or Settings data.
Unexpected console errors and uncaught page errors fail tests. Only a favicon
404 is ignored; no blanket network-error suppression is used.

To select one viewport: `npm run test:browser -- --project=mobile`.
Browser binaries use Playwright's normal per-user cache. If Chromium reports
missing libraries, inspect the exact missing packages before installing them;
`browser:install` intentionally does not run `install-deps` or sudo.

## Settings save toast follow-up

The real Settings POST persists configuration, creates a backup, records an event,
and reloads runtime config even for an unchanged save. A live automated toast test
is therefore deferred. On a disposable WAMF instance with nonsecret fixture config,
navigate to Settings, save unchanged values without restart, verify the polite
save confirmation appears without focus entering the toast, then dismiss it using
the `Dismiss save confirmation` button. Do not emulate a successful POST or alter
application semantics to make this test pass.
