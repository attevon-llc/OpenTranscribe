# Security Reports

This directory contains automated security scan reports for OpenTranscribe container images.

Third-party dependency **license** compliance (a different concern from vulnerability
scanning) is tracked separately in [`license-compliance/`](./license-compliance/) —
see its own README for scope and how to regenerate it.

## Purpose

We believe in **security transparency**. All security scan results are published here so users can:
- Understand the security posture of OpenTranscribe
- Make informed deployment decisions
- Review vulnerability assessments
- Track security improvements over time

## Scan Reports

Every report is named **`<component>-<arch>-<tool>.<ext>`**. The architecture segment is not
decoration: since the multi-arch split (issue #667) each image is built and scanned as a
separate per-architecture leg, and the two legs of one release can carry different findings
because they ship different base-image packages. A report without an arch segment cannot say
which binary it describes.

| Component | Image | Architectures scanned |
|---|---|---|
| `backend` | CUDA backend | `amd64` only — `cuda-arm64` is reserved but not built |
| `lite` | CPU-only backend | `amd64`, `arm64` |
| `frontend` | SPA | `amd64`, `arm64` |
| `docs` | documentation site | `amd64`, `arm64` |

`blackwell` is deliberately **never** published here: it is not part of
`docker-build-push.sh all`, so no release builds it and a scan of it would describe nothing.

Per leg, five tools write eight files, plus a ninth, `*-scan-verdict.json`. **Six of the nine are
published here**:

| Published | Tool |
|---|---|
| `*-trivy.txt` | Trivy vulnerability scan |
| `*-grype.txt` | Grype vulnerability scan with EPSS risk scoring |
| `*-sbom.txt` | Software Bill of Materials |
| `*-hadolint.txt` | Dockerfile lint |
| `*-dockle.json` | Dockle security best-practices audit (small, and has no text form) |
| `*-scan-verdict.json` | The scan's exit code and the policy (`severity_threshold`, `fail_on_critical`) it ran under; `security-scan.sh` reads it to decide whether an unchanged image can reuse its existing report |

So `backend-amd64-trivy.txt` is the human-readable Trivy result for the CUDA backend on x86-64.

`*-trivy.json`, `*-grype.json` and `*-sbom.json` are **gitignored**, for two independent
reasons. **Size**: 30.8 MB per scan against 2.1 MB for the set above, and git keeps every
release's copy forever in a public repo — while they regenerate in minutes from a published
tag. **False positives**: Trivy records the scanned image's `ENV` block, so every
`python:*`-derived report embeds `GPG_KEY=7169605F…`, the *public* CPython release-signing
fingerprint; gitleaks scores that as a `generic-api-key` and blocks the commit. It is not a
credential — but the right response is not to teach the secret scanner to ignore a directory,
it is to not commit machine blobs nobody needs committed.

Need the JSON? Regenerate it:

```bash
./scripts/security-scan.sh backend        # writes the report files for every leg of the component, locally
```

> ⚠️ **Do not commit a report under a name no scanner writes.** The pre-#667 un-suffixed names
> (`backend-trivy.json`) sat here for a month after nothing could refresh them, silently
> presenting scans of two-releases-old images as the current posture. Transparency depends on
> the published file being regenerable, so
> `backend/tests/unit/test_security_reports_are_regenerable.py` now fails on any tracked report
> whose filename the current scanner could not produce.

Regenerate with `./scripts/security-scan.sh <component>` (`./scripts/security-scan.sh list-components`
prints the valid names) and publish with `scripts/push-security-reports.sh`.

## Understanding the Reports

### Severity Levels

- **CRITICAL**: Requires immediate attention for direct dependencies
- **HIGH**: Should be addressed in near-term updates
- **MEDIUM**: Monitored and addressed in regular updates
- **LOW**: Informational, addressed when convenient

### Risk Assessment

Not all reported vulnerabilities pose actual risk to OpenTranscribe. We assess:

1. **Is it a direct dependency?** - If not, limited control until base image updates
2. **Does the app use the vulnerable code?** - Many vulnerabilities are in unused library functions
3. **Is there an attack vector?** - Can an attacker actually trigger the vulnerability?
4. **What's the EPSS score?** - Exploit Prediction Scoring System probability

See [SECURITY-ADVISORY.md](./SECURITY-ADVISORY.md) for detailed risk assessments of current vulnerabilities.

## Security Scanning Tools

### Trivy
- **Purpose**: Comprehensive vulnerability scanner
- **Scope**: OS packages, application dependencies, container configurations
- **Database**: Multiple vulnerability databases (NVD, vendor advisories, etc.)

### Grype
- **Purpose**: Vulnerability scanner with EPSS risk scoring
- **Scope**: OS packages, language-specific packages
- **Database**: Anchore vulnerability database
- **Unique Feature**: EPSS (Exploit Prediction Scoring System) scores

### Hadolint
- **Purpose**: Dockerfile linter
- **Scope**: The component's Dockerfile (`Dockerfile.prod`, `Dockerfile.lite`, `docs-site/Dockerfile`)

### Dockle
- **Purpose**: Docker best practices and security linter
- **Scope**: Dockerfile and container image configuration
- **Standards**: CIS Docker Benchmark compliance

### Syft
- **Purpose**: Software Bill of Materials (SBOM) generation
- **Scope**: Complete inventory of all packages and dependencies
- **Format**: CycloneDX JSON (gitignored) plus a human-readable table (`*-sbom.txt`)

## Current Security Status

Read the current numbers from the reports, not from this file:

```bash
jq .summary security-reports/backend-amd64-dockle.json     # fatal / warn / info / pass counts
head security-reports/backend-amd64-trivy.txt              # vulnerability table
```

Known vulnerabilities and the risk assessment for each are in
[SECURITY-ADVISORY.md](./SECURITY-ADVISORY.md). Check its `Applies to` line: it names the
release it was last written for.

## When Scans Run

- `scripts/docker-build-push.sh` scans each component after building it, unless
  `SKIP_SECURITY_SCAN=true`; `scripts/build-all.sh` does the same through it.
- The release pipeline's `scan` stage (`scripts/release/50-scan.sh`) scans the locally built
  images for the release tag and gates the release on the result.
- The "Security Scanning" GitHub workflow runs only when dispatched by hand; it has no
  schedule.
- Nothing commits the reports automatically. `scripts/push-security-reports.sh` does, and
  `docker-build-push.sh` calls it only when `PUSH_SECURITY_REPORTS=true`.

## Security Response

For our security vulnerability response policy, see [SECURITY-ADVISORY.md](./SECURITY-ADVISORY.md).

### Reporting Security Issues

If you discover a security vulnerability:
- **DO NOT** open a public GitHub issue
- Use GitHub's private vulnerability reporting, as described in [SECURITY.md](../SECURITY.md)
- See [SECURITY-ADVISORY.md](./SECURITY-ADVISORY.md) for known issues

## Generating Reports

```bash
./scripts/security-scan.sh all                      # every component, every architecture leg
IMAGE_TAG=<tag> ./scripts/security-scan.sh lite     # one component at a given image tag (default: latest)
SCAN_PLATFORM=linux/arm64 ./scripts/security-scan.sh frontend   # one architecture leg
```

Other knobs: `OUTPUT_DIR` (default `./security-reports`), `SEVERITY_THRESHOLD` (default
`MEDIUM`), `FAIL_ON_CRITICAL` (default `true`). Exit codes: `0` clean, `1` scanned and found
something, `2` could not scan (never a pass). Run `./scripts/security-scan.sh help` for the rest.
Docker is required; the script tries to install any missing scanner (`install` does only that).

For the full build-and-scan pipeline, see [docs/BUILD_PIPELINE.md](../docs/BUILD_PIPELINE.md).
