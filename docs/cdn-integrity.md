# Pinned CDN integrity checks

The three direct external script/stylesheet tags use SHA-384 SRI and anonymous CORS.
Versions, providers, URLs and load order are unchanged. On verification, each URL
returned HTTP 200 over certificate-verified HTTPS with `Access-Control-Allow-Origin: *`.
Hashes below were computed from decoded response bytes with Python hashlib and independently checked with OpenSSL.

| Resource | URL | Bytes | SHA-384 integrity |
| --- | --- | ---: | --- |
| bootstrap.min.css | https://cdn.jsdelivr.net/npm/bootstrap@5.1.3/dist/css/bootstrap.min.css | 163873 | `sha384-1BmE4kWBq78iYhFldvKuhfTAU6auU8tT94WrHftjDbrCEXSU1oBoqyl2QvZ6jIW3` |
| jquery-3.6.0.min.js | https://code.jquery.com/jquery-3.6.0.min.js | 89501 | `sha384-vtXRMe3mGCbOeY7l30aIg8H9p3GdeSe4IFlP6G8JMa7o7lXvnz3GFKzPxzJdPfGK` |
| bootstrap.bundle.min.js | https://cdn.jsdelivr.net/npm/bootstrap@5.1.3/dist/js/bootstrap.bundle.min.js | 78129 | `sha384-ka7Sk0Gln4gmtz2MlQnikT1wXgYsOg+OMhuP+IlRH9sENBO0LRn5q+8nbTov4+1p` |

Bootstrap 5.1.3 uses its existing bundle, including Popper. The jQuery 3.6.0 tag is retained.
Monaco Editor 0.52.2 is now served entirely from
`static/vendor/monaco-editor/0.52.2/min/vs/`, including its AMD modules, stylesheet,
font and workers. The original LICENSE and ThirdPartyNotices.txt are retained.
The published npm tarball was verified against its SHA-512 integrity before
extracting the complete 103-file AMD distribution. `PROVENANCE.json` records the
source, archive integrity and SHA-256 checksums for all 105 original files.
The local loader retains SRI calculated from its actual npm-distribution bytes;
its hash differs from the former cdnjs minified loader. No npm installation,
ESM migration or pruning is involved. Worker URLs follow the local AMD base path;
the Flask-generated path is converted to an absolute URL using the browser origin
so the existing Blob bootstrap workers can resolve their local module imports.
The Dockerfile already copies the static tree, so no packaging changes are needed.

SRI prevents use of changed bytes; it does not provide CDN availability, privacy,
or assurance that the approved library itself has no vulnerabilities. A CDN content
change will intentionally block the affected resource until independently reviewed.

Verification and regression procedure:

1. Download each exact URL above using certificate-verified HTTPS without redirects.
   Save the binary response body under its listed resource filename; do not normalize text or line endings.
2. Compute `sha384-` plus Base64 of the SHA-384 digest. Independently compare with
   `openssl dgst -sha384 -binary RESOURCE | openssl base64 -A`.
3. Confirm HTTP 200 and an appropriate CORS response header. Review any unexpected hash change before updating templates or fixtures.
4. Point `WAMF_CDN_ASSET_DIR` at that download directory and run:

```sh
WAMF_CDN_ASSET_DIR=/path/to/downloads .venv311/bin/python -m pytest -q tests/test_cdn_integrity.py
```

Tests always check every direct external script/stylesheet tag and the rendered
overview/configuration pages. Byte checks and browser checks use the explicit cache
directory; they skip when it is not supplied, so ordinary tests require no live CDN.
Browser checks use an already-installed Chromium and two loopback origins serving
the verified bytes. They check anonymous CORS, Bootstrap collapse/modal controls,
detection cards and browser rejection of modified JS/CSS. Separate local Monaco
browser tests initialize the real YAML editor and workers with off-origin requests
blocked, exercising editing, copy, save and restart controls on synthetic data.
Bootstrap and jQuery remain external and SRI-pinned. Monaco is locally vendored;
no new build or browser dependencies are installed. Its byte checks and local
browser tests do not require WAMF_CDN_ASSET_DIR or access to any CDN.
