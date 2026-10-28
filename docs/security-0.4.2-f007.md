# WAMF v0.4.2 F-007: bootstrap setup-token disposition

Assessed on 2026-10-27 in `release/v0.4.2` through source and existing-test
review. WAMF was not started and no setup tokens were generated or exposed.

**Status: final conditional risk acceptance for v0.4.2, recorded 2026-10-27.**
Acceptance depends on restricting startup logs to trusted administrators and
meeting the operational requirements below. This is a conditionally exploitable
credential exposure, not a finding that is inapplicable to WAMF.

## Threat and exposure

[Native bootstrap](../app/bootstrap.py) generates a setup token for a genuinely
unprovisioned, authentication-enabled installation, or when valid setup remains
pending. After persisting its digest, it prints the complete private setup link
directly to stdout, once per pending bootstrap. A restart replaces the old token
and prints a fresh link; HTTP requests and ordinary WSGI imports cannot generate
or renew it. Existing valid credentials receive no link.

A reader of the unused link who can reach WAMF within its 15-minute lifetime can
choose the administrator password before the intended operator. Log access does
not necessarily imply WAMF administrator privileges: read-only journal access or
centralized-log access can cross this trust boundary. CSRF and attempt limiting
do not prevent redemption by someone who already possesses the valid token.

Systemd normally captures service stdout in journald; Docker/Compose captures it
through the configured logging driver. Foreground execution exposes it in the
terminal, and supervisors, redirection or terminal recording may retain copies.
Readers depend on host permissions, journal ACLs, Docker access and logging
platform permissions. Direct `print()` output is independent of application
logging levels. Actual deployment log readership was not audited.

The default `0.0.0.0` binding listens on all IPv4 interfaces. A printed
`127.0.0.1` link does **not** restrict setup access to localhost; network exposure
also depends on published ports, proxies and firewall rules.

## Existing protections

- **Token strength and storage:** `secrets.token_urlsafe(32)` uses 256 bits of
  randomness. Only its SHA-256 digest and setup metadata are persisted; digest
  comparison uses `hmac.compare_digest`.
- **Expiry and invalidation:** a 900-second lifetime is checked using wall time
  and an inherited monotonic deadline. Clock rollback fails closed; observed
  expiry is terminal. Expiry is rechecked after password hashing and before
  replacement. Restarting pending setup invalidates the previous link.
- **Single use:** successful redemption stores the password hash and removes
  setup state. A configuration-directory lock serializes concurrent redemption
  across threads and forked processes. Expired, replaced or redeemed tokens
  cannot create another administrator password.
- **Request controls:** setup requires CSRF validation and matching passwords;
  invalid-token attempts share a ten-per-minute allowance. Valid-token
  redemption remains possible after that allowance is exhausted. Requests are
  limited to 8 KiB. Operational workers remain paused until setup completes.
- **Configuration replacement:** locked, mode-0600 temporary files are flushed
  and synchronized before atomic replacement; directory synchronization follows.
  There is no fallback to an in-place write. See
  [configuration storage](../app/config_store.py).
- **HTTP disclosure reduction:** the link carries the token in a URL fragment,
  excluded from normal HTTP request URLs and request-line logging. The
  [local setup script](../static/js/admin-setup.js) clears the fragment from the
  address bar, retains it in a closure and submits it only in the POST body,
  without persisting it in cookies or browser storage. Setup responses use
  `no-store`, `no-referrer`, restrictive CSP and framing protection. Expected
  exception handlers log sanitized metadata rather than secrets.

These controls limit exposure; they do not remove retained startup-log copies.
The POST body contains both the token and chosen password and remains visible to
the browser, TLS-terminating proxies and any request-body capture. Remote plain
HTTP allows interception or modification; HTTPS is recommended but not enforced
by the setup handler.

## Operational requirements and release disposition

- Restrict startup logs, console recordings and exported log copies to trusted
  administrators authorized to perform initial WAMF setup.
- Protect centralized logging access, including collectors, dashboards and
  exports; do not assume their readers have host or WAMF administrator rights.
- Disable request-body logging and capture during setup, including reverse-proxy,
  tracing and WAF capture of setup requests.
- Use HTTPS or a trusted tunnel for remote setup. A loopback URL printed in a
  log is not a network access control.
- Complete initial setup promptly and keep the unused link private. Expiry,
  restart and successful redemption invalidate links without deleting logs.

The [README](../README.md#development) already documents sensitive startup logs,
expiry, single use and restart rotation. The
[Docker instructions](docker-testing.md) require log protection, remote transport
protection and disabling proxy request-body logging. This finding records the
remaining risk and its acceptance conditions explicitly. Existing lifecycle,
HTTP and script regression cases are in [setup tests](../tests/test_admin_setup.py)
and [script tests](../tests/test_admin_setup_browser.py); they were reviewed,
not executed for this assessment.

Retain the current bootstrap mechanism for v0.4.2 under these accepted conditions.
Where log access cannot be adequately restricted, this
acceptance does not apply: a possible future alternative is owner-only setup-file
delivery with only retrieval instructions printed to stdout. Such a change must
preserve token expiry, single use and safe native/Docker operator retrieval;
simply suppressing the link would break provisioning. No such change is made
here, and no application, dependency or Dockerfile change is required for this
documented disposition.

## Final v0.4.2 review

Final documentation review on 2026-10-27 found no material discrepancy between
this assessment and the current release candidate. Relevant application and
security-test sources match the consolidated security audit. Source review
confirmed token persistence before stdout delivery, expiry and single-use
controls, atomic configuration replacement, request protections and fragment-only
link delivery. The README and Docker guidance retain the operational requirements
above.

This closes the F-007 risk-acceptance review for v0.4.2 under those conditions.
The startup-log and setup-body credential exposure remains real; acceptance does
not establish that deployed log permissions, request-body capture or remote
transport satisfy the requirements. Where those conditions cannot be met, this
acceptance does not apply. No WAMF instance was started, no setup token was
generated, and no new scan, test run or Docker build was performed for this final
documentation review. This disposition does not replace the outstanding
current-candidate Docker build/runtime and image-security verification or approve
publication of the release.
