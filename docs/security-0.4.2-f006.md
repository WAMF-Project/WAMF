# WAMF v0.4.2 F-006: pip/setuptools disposition

Assessed on 2026-10-27 in `release/v0.4.2`, using `.venv311`:
Python **3.11.17**, pip **24.0**, setuptools **79.0.1**.

**Status: final scoped risk acceptance for v0.4.2, recorded 2026-10-27.**
Acceptance covers the retained local packaging-tool exposure described below;
it does not classify the findings as exploitable WAMF runtime vulnerabilities.
No application, dependency or Dockerfile changes are required for this finding.
The existing protobuf decision is outside this assessment and remains unchanged.

## Advisory applicability

All eight advisory version ranges match the inspected local tools. None has an
identified execution path through normal WAMF operation. Advisory links provide
the upstream affected ranges and fixes; a version match alone does not establish
exploitability.

| Package / advisory | Affected versions | First fixed version | Conditions and applicability |
| --- | --- | --- | --- |
| pip: CVE-2025-8869 / [GHSA-4xh5-x5gv-qwph](https://github.com/advisories/GHSA-4xh5-x5gv-qwph) | `<25.3` | `25.3` | Unsafe fallback tar extraction can follow escaping symlinks in a malicious archive. Local package-management risk; no WAMF runtime path. |
| pip: CVE-2026-1703 / [GHSA-6vgw-5pg2-w6jp](https://github.com/advisories/GHSA-6vgw-5pg2-w6jp) | `<26.0` | `26.0` | Crafted wheel extraction can write outside the destination, limited to directory prefixes. Local installation risk; no WAMF runtime path. |
| pip: CVE-2026-3219 / [GHSA-58qw-9mgm-455v](https://github.com/advisories/GHSA-58qw-9mgm-455v) | `<=26.0.1` | `26.1` | A combined tar/ZIP archive can install different contents than expected. Local installation risk; no WAMF runtime path. |
| pip: CVE-2026-6357 / [GHSA-jp4c-xjxw-mgf9](https://github.com/advisories/GHSA-jp4c-xjxw-mgf9) | `<26.1` | `26.1` | Pip's post-install update check can import newly installed wheel modules. Local installation risk; no WAMF runtime path. |
| pip: CVE-2026-8643 / [GHSA-wf93-45jw-7689](https://github.com/advisories/GHSA-wf93-45jw-7689) | `<26.1.2` | `26.1.2` | Crafted console/gui entry-point names can install scripts outside their destination. Local installation risk; no WAMF runtime path. |
| pip: CVE-2026-13346 / [GHSA-qwm4-qh6w-59xr](https://github.com/advisories/GHSA-qwm4-qh6w-59xr) | `<26.2.0` | `26.2.0` | Doubly encoded URLs from a malicious package index permit arbitrary destination writes; a malicious package alone is insufficient. Local download/installation risk; no WAMF runtime path. |
| setuptools: CVE-2026-59890 / [GHSA-h35f-9h28-mq5c](https://github.com/pypa/setuptools/security/advisories/GHSA-h35f-9h28-mq5c) | `<83.0.0` | `83.0.0` | Unicode normalization mismatches can bypass `MANIFEST.in` exclusions during sdist creation, especially on macOS filesystems; publication can disclose excluded files. Not applicable to the observed Linux runtime or documented WAMF installation flow. |
| setuptools: [GHSA-xmj3-9gf7-h52w](https://github.com/pypa/setuptools/security/advisories/GHSA-xmj3-9gf7-h52w) | `>=38.2.2,<84.1.0` | `84.1.0` | Windows legacy wheel-to-egg installation with crafted namespace metadata can create a fixed-content `__init__.py` outside its destination. Not applicable to this Linux environment or normal WAMF operation. |

Python 3.11.17 provides PEP 706 extraction filters, but the inspected pip 24.0
tar extraction implementation does not use them. The interpreter version alone
is therefore not accepted as a confirmed local mitigation for CVE-2025-8869.

## Dependency roles and existing mitigation

Neither pip nor setuptools is a declared WAMF runtime dependency or an active
requirement of the installed runtime distributions. They are package-management
and build tools retained in the local virtual environment. The
[native/development instructions](../README.md#development) invoke pip during
installation, then launch `python speciesid.py`; CI also invokes pip to install
test dependencies.

Inspection found no WAMF application or startup imports/invocations of pip,
setuptools, `pkg_resources` or ensurepip. Normal native operation does not
perform the vulnerable download, installation or distribution-building
operations. Setuptools' installed `_distutils_hack` startup shim does not itself
execute those advisory-specific paths.

The existing [Dockerfile](../Dockerfile) updates build tools to pip **26.2.2**,
setuptools **84.1.0**, wheel **0.48.0** and packaging **26.3** before installing
the constrained runtime dependencies. It runs `pip check`, then removes all
four tools and the ensurepip bootstrap payload from the runtime image.

[Stage B](security-0.4.1-stage-b.md#security-findings) distinguished packaging
and embedded-SBOM matches from demonstrated runtime exploits.
[Stage D](security-0.4.1-stage-d.md#step-2-remove-packaging-tools) records tool
removal and validation of the remaining runtime dependency metadata.
[Stage G1](security-0.4.1-stage-g1.md#4-dependency-and-docker-reproducibility)
records successful build-time and post-removal dependency checks, and absence
of packaging tools, vendored directories and bootstrap wheels from the tested
merged runtime filesystem. These are prior validation results, not a new image
build or scan. Removing files does not erase inherited image-layer contents.

## Scoped acceptance and release disposition

- **Docker runtime:** retain the existing mitigation; these tooling advisories
  are not applicable when the tools and bootstrap payloads are absent.
- **Native runtime:** no identified exposure through normal WAMF operation.
- **Local package management:** pip 24.0 remains affected during the relevant
  download/installation operations. Retaining it is an accepted scoped residual
  risk for v0.4.2, rather than a claim that the tool is safe for
  arbitrary packages or indexes.
- **Tooling maintenance:** keep packaging tools current and reviewed; avoid
  untrusted installation sources, indexes and artifacts. Plan a separate local
  tooling update before processing untrusted inputs. No package installation
  or upgrade was performed for this assessment or documentation change.

This assessment does not certify older or independently built images, nor every
deployed installation. Those require verification of their actual contents and
workflows. No application, runtime manifest or Dockerfile change is necessary
for F-006; its final v0.4.2 risk disposition is accepted within this scope.

## Final v0.4.2 review

Final documentation review on 2026-10-27 found no material discrepancy between
this assessment and the current release candidate. The local Python, pip and
setuptools versions still match the inventory above. Relevant application and
security-test sources match the consolidated security audit; the Dockerfile
retains the documented build-tool pins, dependency check and removal commands.
The protobuf decision remains unchanged and was not reopened.

This closes the F-006 risk-acceptance review for v0.4.2. The local
package-management exposure and tooling-maintenance guidance remain in force.
Acceptance does not certify a newly built image or every deployed environment;
current-candidate Docker build/runtime and image-security verification remain
outstanding. No new security scan, test run, package installation or Docker build
was performed for this final documentation review.
