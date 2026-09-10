# Security Reports

This directory contains automated security scan reports for OpenTranscribe container images.

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

Per leg, five tools write eight files. **Five of the eight are published here**:

| Published | Tool |
|---|---|
| `*-trivy.txt` | Trivy vulnerability scan |
| `*-grype.txt` | Grype vulnerability scan with EPSS risk scoring |
| `*-sbom.txt` | Software Bill of Materials |
| `*-hadolint.txt` | Dockerfile lint |
| `*-dockle.json` | Dockle security best-practices audit (small, and has no text form) |

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
./scripts/security-scan.sh backend        # writes all eight files per leg, locally
```

> ⚠️ **Do not commit a report under a name no scanner writes.** The pre-#667 un-suffixed names
> (`backend-trivy.json`) sat here for a month after nothing could refresh them, silently
> presenting scans of two-releases-old images as the current posture. Transparency depends on
> the published file being regenerable, so
> `backend/tests/unit/test_security_reports_are_regenerable.py` now fails on any tracked report
> whose filename the current scanner could not produce.

Regenerate with `./scripts/security-scan.sh <component>` and publish with
`scripts/push-security-reports.sh`.

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

### Dockle
- **Purpose**: Docker best practices and security linter
- **Scope**: Dockerfile and container image configuration
- **Standards**: CIS Docker Benchmark compliance

### Syft
- **Purpose**: Software Bill of Materials (SBOM) generation
- **Scope**: Complete inventory of all packages and dependencies
- **Format**: SPDX, CycloneDX compatible

## Current Security Status

✅ **Backend Dockle Score**: 0 fatal, 0 warn, 1 info
- No critical security misconfigurations
- Follows Docker security best practices
- Non-root container user (UID 1000)

⚠️ **Known Vulnerabilities**: See [SECURITY-ADVISORY.md](./SECURITY-ADVISORY.md)
- CVE-2025-47917 (Critical - Low Risk): Mbed TLS vulnerability in unused functionality
- Other low-risk vulnerabilities in transitive system dependencies

## Update Frequency

Security scans are run:
- On every Docker image build
- After base image updates
- After dependency updates
- Monthly for routine checks

Reports are automatically updated in this directory.

## Security Response

For our security vulnerability response policy, see [SECURITY-ADVISORY.md](./SECURITY-ADVISORY.md).

### Reporting Security Issues

If you discover a security vulnerability:
- **DO NOT** open a public GitHub issue
- Contact: security@opentranscribe.org (or create a private security advisory)
- See [SECURITY-ADVISORY.md](./SECURITY-ADVISORY.md) for details

## Generating Reports

Run `./scripts/build-all.sh` or `./scripts/docker-build-push.sh` to generate updated reports.

For details, see [docs/BUILD_PIPELINE.md](../docs/BUILD_PIPELINE.md).

---

**Last Updated**: 2025-10-27
