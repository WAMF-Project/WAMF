This directory contains WAMF's release-owned classification catalogue. It is
not a record of the models installed on a particular host. M1 provides local
discovery/validation; M2 adds an explicit install service without activation.

`catalog.yml` uses schema version 1. Its single `default` reference selects a
model ID and immutable artifact version. Each model has a name, description,
and one or more artifacts. Each artifact declares its filename, SHA-256, exact
size, LiteRT contract, class count, background mapping, source and licence.
`bundled_path`, `download_url` and `notes` are optional in the catalogue schema.
The install service requires an approved URL for non-bundled artifacts.
Do not change the bytes/hash of a published artifact version.

The `bird/bundled-1` entry refers directly to the existing root `model.tflite`;
no copy or download is required. `bundled-1` is a WAMF revision. Upstream model
provenance and weight licensing remain unverified; WAMF's software licence
must not be assumed to cover the weights.

Existing `classification.model` paths remain supported, relative to the WAMF
application root or absolute/`~`-expanded as before. Managed configuration uses
both `classification.model_id` and `classification.model_version`, for example
`bird` and `bundled-1`. These managed fields cannot coexist with `model`.
No automatic conversion of legacy configuration is performed. The examples
remain on the legacy path until the later Model Manager UI work.

Downloaded artifacts resolve beneath
`data/models/<id>/<version>/<filename>`. `storage.models_path` can override that
root using the existing application-relative/absolute/`~` path conventions.
Docker uses the existing `/app/data` mount. Temporary files do not count as
installed artifacts. Presence, verified identity, classifier validation,
configuration selection and the running classifier are distinct states.

`wamf-bird-v1` requires RGB, direct 224x224 resizing, raw uint8 input with
quantization `(0.0078125, 128)`, one uint8 class-vector output with scalar
quantization, and the two embedded probability-label files. Managed validation
checks identity, structure, labels/background and isolated timed synthetic
inference. It does not measure accuracy. Legacy initialization retains its
historical checks. Model changes continue to require a WAMF restart.

The M2 backend API is `app.model_install.install_model(id, version, ...)` and
`get_install_status()`, or an explicitly constructed `ModelInstaller` with its
own `snapshot()`. Calls are synchronous, owned by the calling request; another
request can read the frozen in-memory snapshot. No queue, background thread,
Admin endpoint, automatic activation or configuration write is involved.

Sources must be HTTPS release downloads under `WAMF-Project/WAMF`. Redirects
are manually limited to five and explicit GitHub asset hosts, including the
documented `release-assets.githubusercontent.com` host. Environment proxies
and netrc authentication are disabled. Bodies stream in 64 KiB chunks with an
exact catalogue-size bound and a 256 MiB application cap. HTTP connect/read
timeouts are 5/15 seconds; the owned install subprocess has a separate 600-second
deadline. M1 validation retains its own 15-second subprocess deadline.

One nonblocking `.install.lock` flock per model root covers staging, cleanup,
verification, validation and promotion. It is inherited by the owned worker
and released after that worker exits, including timeout/parent-death cases.
Only recognized `.filename.<32 hex characters>.part` files for this artifact
are cleaned, while locked. Staging is exclusive/private in the destination
directory; size/hash checks precede M1 validation and atomic replacement.
File fsync failure aborts before promotion. Directory fsync after promotion is
best effort, as in config persistence, and surfaces `durability_warning`.

Valid installed versions return `already_installed` without HTTP. Bundled
artifacts are validated in place and never repaired/downloaded automatically.
Repair of an invalid final file is deferred with `repair_blocked` when running
identity is unknown. Internal callers may provide authoritative `running_paths`
(or `()` if no classifier runs); configured selection is also protected.
M3 must coordinate selection changes using the same install guard. Different
versions coexist and no existing bytes are truncated during repair.

Progress is observational and forgotten on restart. Killed workers may leave
recognized staging files for the next install to clean. A deadline interrupting
after atomic promotion can leave a fully validated installed artifact without
a successful response; retry discovers it idempotently. Installation never
asserts that a model is active. No accuracy measurement is performed.
