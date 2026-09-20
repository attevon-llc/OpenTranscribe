# Security Advisory - OpenTranscribe

**Last Updated**: 2026-09-20
**Applies to**: OpenTranscribe v0.5.1
**Base Images**: `python:3.13-slim-trixie` (Debian 13), `nginx:1.29.8-alpine3.23`

## Overview

This document provides transparency about known security vulnerabilities in OpenTranscribe's container images and explains their actual risk to the application. It is updated alongside every tagged release.

## v0.5.1 — Security Patch Release

v0.5.1 is a dependency-maintenance and CVE-remediation release. It consolidates the week's
Dependabot batch and specifically targets CRITICAL-severity container findings.

### Fixed: vendored `pcre2` CRITICALs (issue #951)

**Root cause**: `psycopg2-binary`'s `manylinux` wheel bundles its own copy of several shared
libraries via `auditwheel`, including `pcre2`. On the arm64 wheel, that vendored copy was built
against an AlmaLinux 8 base (`10.32-3.el8_6`) carrying three unpatched CRITICAL CVEs — confirmed
by inspecting the wheel's `.libs/` directory directly (`psycopg2_binary.libs/libpcre2-8-*.so`),
not by scanner guesswork. The system's own `libpcre2-8-0` (from Debian trixie, kept current by
the existing `apt-get upgrade` step) was never affected — this was an entirely separate,
invisible copy.

**Fix**: switched from `psycopg2-binary` to `psycopg2` (source build) across every Dockerfile
(`backend/Dockerfile.prod`, `.lite`, `.blackwell`) and CI workflow that installs it. A
source-built `psycopg2` links against the system's own `libpq`/`pcre2` rather than vendoring its
own — the vulnerable copy doesn't exist at all, rather than being merely patched.

**Verified, not assumed**: rebuilt every v0.5.1 release image from scratch and re-scanned.

| Image | CRITICAL before | CRITICAL after |
|---|---|---|
| `backend-amd64` | 1 | 1 (unrelated — see below) |
| `lite-amd64` | 1 | 1 (unrelated — see below) |
| **`lite-arm64`** | **4** | **1** (3 pcre2 CVEs eliminated: CVE-2022-1586, CVE-2022-1587, CVE-2025-58050) |
| `frontend-amd64` / `arm64` | 0 | 0 |
| `docs-amd64` / `arm64` | 0 | 0 |

### Fixed: 4 real dependency-resolution conflicts caught during the Dependabot batch

Each verified via a direct `pip install` reproduction before merging, not assumed from a diff:

| Package | Proposed | Reverted to | Real conflict |
|---|---|---|---|
| `numpy` | 2.5.3 | 2.4.6 | `ResolutionImpossible` vs presidio-analyzer/anonymizer's own `numpy<2.5.0` metadata |
| `tokenizers` | 0.23.2 | 0.22.2 | `ResolutionImpossible` vs `transformers==4.57.6`'s own `tokenizers<=0.23.0,>=0.22.0` metadata |
| `presidio-anonymizer` | 2.2.364 | 2.2.362 | `ResolutionImpossible` vs `cryptography==50.0.1` — 2.2.364's own wheel metadata declares `cryptography<49.0.0` |
| `fastapi` (`requirements-lite.txt` only) | 0.141.1 (pre-existing) | 0.136.3 | The exact version that caused the #940 route-mounting regression; this file was missed by the original fix |

Each now has a `dependabot.yml` ignore rule documenting the constraint, so it won't silently
recur in a future weekly batch.

---

## Remaining CRITICAL: CVE-2026-6653 — libxml2 Use-After-Free

**Package**: `libxml2` 2.12.7+dfsg+really2.9.14-2.1+deb13u3
**Present on**: `backend-amd64`, `lite-amd64`, `lite-arm64` (not `frontend`/`docs` — those are nginx-based)
**Status**: **ACCEPTED RISK — no upstream patch exists anywhere**
**Tracked**: issue [#775](https://github.com/attevon-llc/OpenTranscribe/issues/775) — this
repo's standing "unfixed OS-level CRITICAL" record (also covers `libimage-exiftool-perl`'s
perl-stack findings, kept for metadata-extraction fidelity; re-measured for v0.5.1 there:
16 → 1 CRITICAL total on `backend-amd64`, this finding being the 1 remaining)

**Verified directly, not assumed stale**: ran `apt-cache policy libxml2` against a *fresh*
`debian:trixie-slim` container on 2026-09-20. The installed version IS the latest candidate
available from both `trixie/main` and `trixie-security` — there is no newer package to upgrade
to. This cannot be closed by any dependency or base-image bump today; it can only be closed when
Debian ships a patch, which the existing `apt-get upgrade -y` step in `Dockerfile.prod`/`.lite`
will then pick up automatically on the next image build.

**Reachability — assessed honestly, not reflexively dismissed.** Traced via `ldd` against the
actual running container rather than asserted:

- **NOT reachable** via `python3-saml`/`xmlsec` (SAML assertion parsing): confirmed the bundled
  `xmlsec` `.so` files do not dynamically link `libxml2` at all.
- **IS reachable** via ffmpeg: `libavformat`, `libavfilter`, `libavdevice`, and `libavcodec` all
  link `libxml2` directly, and OpenTranscribe's transcription pipeline demuxes every uploaded
  media file through them. Some container/subtitle formats ffmpeg supports carry XML-structured
  data (e.g. TTML subtitle tracks), and `librsvg` (SVG, itself XML) is also linked, reachable via
  embedded cover art. This is a plausible, if narrow, path for adversarial input — unlike prior
  advisory entries in this document for unrelated packages (ncurses, systemd, mbedtls) that this
  application genuinely never exercises. No working exploit chain against the specific
  vulnerable function (`xmlParseInternalSubset`, an internal-DTD-subset parsing path) has been
  demonstrated or ruled out here.

**Mitigation until Debian ships a fix**: none available beyond the existing hardening (non-root
container user, no privileged mode, network segmentation). Tracked for re-scan on every future
release build; will close automatically the moment a patched `libxml2` lands in `trixie` or
`trixie-security`.

---

## HIGH-severity findings — summary, not an exhaustive per-CVE audit

`backend-amd64` carries 54 distinct HIGH-severity CVE IDs. Cataloguing each individually (as this
document did for the much smaller v0.4.0 baseline) doesn't scale at this count and would mostly
duplicate what the raw reports already say. Full detail: `security-reports/backend-amd64-trivy.txt` (tracked in this repo) and the
equivalent `-grype.txt` output for the same leg.

**Composition** (backend-amd64, the largest leg):

- **~38 of 54** are Debian OS-level packages with **no fix available** in `trixie`/`trixie-security`
  today: the same `libxml2` CVEs as above (7), ffmpeg's own bundled libraries (16 findings across
  8 shared libs, same handful of CVE IDs), `curl`/`libcurl4t64`, `gnupg` family, `util-linux`
  family (`mount`, `login`, `libuuid1`, `libblkid1`, etc. — all the same 4 CVE IDs, since they
  ship from one source package), `libcjson1`, `libexpat1`, `libtiff6`, `libncursesw6`/`ncurses-*`,
  `libsystemd0`/`libudev1`, `perl` family. None have a Debian-provided fix as of 2026-09-20;
  `apt-get upgrade` will pick each up automatically the moment one ships.
- **3 have a fix available in our own dependency tree, not yet taken**:
  - `transformers` (CVE-2026-4372/5241/9856, fixed in 5.3.0+/5.5.0+/5.10.0+) — the fix requires
    a **major version bump** (4.57.6 → 5.x), which the project has an existing `dependabot.yml`
    ignore rule for: transformers 5.x breaks whisperx/pyannote compatibility. This work is
    **deliberately deferred to v0.8.0**'s planned backend changes rather than rushed into a
    security patch release — doing compatibility work now would likely be redone there.
  - `msgpack` (GHSA-6v7p-g79w-8964, installed 1.1.2, fixed 1.2.1) — confirmed **not actually
    importable** in the running application (`ModuleNotFoundError: No module named 'msgpack'`).
    An orphaned artifact somewhere in the image rather than a reachable dependency; lower-severity
    cousin of the pcre2 issue above. Not yet root-caused to a specific vendoring package —
    follow-up work, not release-blocking.
  - `setuptools` CVE-2025-47273 (installed 70.3.0 per the scan) — this version is **not present
    anywhere in the built container's filesystem** (verified directly); it is a stale SBOM
    reference from an intermediate multi-stage build layer, not a shipped artifact. The version
    actually installed and running (81.0.0) already exceeds the advisory's fixed version.
    `setuptools` CVE-2026-59890 (fixed 83.0.0) genuinely affects the installed 81.0.0 and remains
    open — transitive via `torch`/`virtualenv`, no direct pin exists yet.

`lite-amd64`/`lite-arm64` carry a similar composition (221 HIGH findings each, same dominant
ffmpeg/libxml2/util-linux pattern as backend, since they share the same Debian base and ffmpeg
build). `frontend`/`docs` (nginx-based, Alpine) carry 11 and 0 HIGH findings respectively — see
the raw reports.

---

## Frontend Image (v0.5.1)

**Base**: `nginx:1.29.8-alpine3.23` + `apk upgrade --no-cache`

Trivy: **0 Critical** on both `amd64` and `arm64`. 11 High on `amd64`, 0 on `arm64` — see
`security-reports/frontend-amd64-trivy.txt`.

## Docs Image (v0.5.1)

**Base**: `nginx:1.29.8-alpine3.23` + `apk upgrade --no-cache`

Trivy: **0 Critical** on both `amd64` and `arm64`. 11 High on `amd64`, 0 on `arm64` — see
`security-reports/docs-amd64-trivy.txt`.

---

## Security Scanning Process

OpenTranscribe is scanned with the following tools on every release:

- **Trivy** — comprehensive vulnerability scanner with CVE database (authoritative for the
  CRITICAL gate — `FAIL_ON_CRITICAL` reads Trivy's count, not Grype's)
- **Grype** — secondary scanner with EPSS risk scoring for corroboration
- **Syft** — software bill of materials (SBOM) generation (SPDX + CycloneDX)
- **Dockle** — Docker best-practices and image hardening linter
- **Hadolint** — Dockerfile linter

All scan outputs are committed to `security-reports/` in this repository for transparency and
reproducibility. Re-run them yourself with:

```bash
IMAGE_TAG=0.5.1 ./scripts/security-scan.sh all
```

---

## Reporting Security Issues

If you discover a security vulnerability in OpenTranscribe:

1. **DO NOT** open a public GitHub issue
2. Report via GitHub private security advisory: https://github.com/attevon-llc/OpenTranscribe/security/advisories
3. Include:
   - Description of the vulnerability
   - Steps to reproduce
   - Potential impact
   - Suggested fix (if available)

---

## Update Policy

- **Critical vulnerabilities** in direct dependencies: patched within 7 days
- **High vulnerabilities** in direct dependencies: patched within 30 days
- **Transitive / OS-level vulnerabilities**: evaluated for actual exploitability; patched automatically via `apt-get upgrade` / `apk upgrade` on every image build
- **Base image updates**: applied on every release (see `backend/Dockerfile.prod` and `frontend/Dockerfile.prod` runtime stages)

---

## Hardening Notes

### Non-Root Container User

OpenTranscribe backend containers run as `appuser` (UID 1000), not root. This significantly reduces the attack surface even if a vulnerability exists in system libraries.

### Minimal Attack Surface

The application:
- Requires authentication for all operations (local password, LDAP, Keycloak/OIDC, or PKI client cert)
- Validates all user inputs via Pydantic schemas
- Uses parameterized SQL queries (SQLAlchemy ORM only — no string-interpolated SQL)
- Isolates services via Docker networking with minimum-necessary port exposure
- Stores credentials encrypted at rest (Fernet, per-field encryption for API keys)
- Enforces configurable password policy + rate limiting + account lockout
- Supports optional TOTP MFA (RFC 6238) for local-auth users

### Container Isolation

- All services run as non-root inside their containers
- No privileged mode
- Backend container uses `--cap-drop ALL` where possible
- Network segmentation via Docker bridge networks

---

**Document Version**: 3.0
**Applies to**: OpenTranscribe 0.5.1
**Maintainer**: OpenTranscribe Security Team
