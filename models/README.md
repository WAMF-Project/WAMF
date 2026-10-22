This directory contains WAMF's release-owned classification catalogue. It is
not a record of the models installed on a particular host. M1 provides local
discovery and validation only; it does not download or activate models.

`catalog.yml` uses schema version 1. Its single `default` reference selects a
model ID and immutable artifact version. Each model has a name, description,
and one or more artifacts. Each artifact declares its filename, SHA-256, exact
size, LiteRT contract, class count, background mapping, source and licence.
`bundled_path`, `download_url` and `notes` are optional. URLs are metadata only.
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

Downloaded artifacts, when installation is implemented, will resolve beneath
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
