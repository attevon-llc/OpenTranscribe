# Security Scanning Guide

This guide explains how to use the free, open-source security scanning tools integrated into OpenTranscribe for local development and CI/CD pipelines.

> **v0.4.0**: Trivy and Grype scans are part of the release process. Run `./scripts/security-scan.sh all` and resolve any CRITICAL/HIGH findings before tagging a release. The GitHub Actions `security-scan.yml` workflow enforces this automatically on pushes to main.

## Table of Contents

- [Overview](#overview)
- [Tools Used](#tools-used)
- [Local Scanning](#local-scanning)
- [Pre-commit Hooks](#pre-commit-hooks)
- [CI/CD Integration](#cicd-integration)
- [Understanding Reports](#understanding-reports)
- [Best Practices](#best-practices)
- [Troubleshooting](#troubleshooting)

## Overview

OpenTranscribe uses a comprehensive security scanning approach with **free, open-source tools** that run locally—no Docker Hub subscription or paid services required.

### Key Benefits

- ✅ **100% Free**: All tools are open-source and free to use
- ✅ **No Cloud Required**: Scan images locally on your machine
- ✅ **Multi-layered**: Combines linting, best practices, and vulnerability scanning
- ✅ **SBOM Generation**: Create Software Bill of Materials for compliance
- ✅ **CI/CD Ready**: Automated scanning in GitHub Actions
- ✅ **Fast**: Smart caching and SBOM-based scanning

## Tools Used

### 1. Hadolint - Dockerfile Linter
**Purpose**: Lint Dockerfiles for best practices and common mistakes

```bash
# Install
brew install hadolint  # macOS
# Or download binary for Linux

# Scan
hadolint backend/Dockerfile.prod
```

**What it catches**: Deprecated instructions, inefficient layer caching, security issues in Dockerfile syntax

### 2. Dockle - Container Image Best Practices
**Purpose**: Check container images against CIS Docker Benchmarks

```bash
# Run via Docker (no installation needed)
docker run --rm -v /var/run/docker.sock:/var/run/docker.sock \
  goodwithtech/dockle:latest your-image:tag
```

**What it catches**: Missing security labels, exposed secrets, improper permissions, CIS violations

### 3. Trivy - Comprehensive Vulnerability Scanner
**Purpose**: Scan for CVEs in OS packages, language dependencies, and configurations

```bash
# Install
brew install trivy  # macOS
curl -sfL https://raw.githubusercontent.com/aquasecurity/trivy/main/contrib/install.sh | sh  # Linux

# Scan
trivy image your-image:tag
```

**What it catches**: Known vulnerabilities (CVEs), misconfigurations, exposed secrets, license issues

### 4. Grype - Fast Vulnerability Scanner
**Purpose**: High-performance vulnerability scanning with SBOM support

```bash
# Install
curl -sSfL https://raw.githubusercontent.com/anchore/grype/main/install.sh | sh

# Scan
grype your-image:tag
grype sbom:./sbom.json  # Faster, scan from SBOM
```

**What it catches**: CVEs in packages and dependencies with detailed fix recommendations

### 5. Syft - SBOM Generator
**Purpose**: Generate Software Bill of Materials for compliance and auditing

```bash
# Install
curl -sSfL https://raw.githubusercontent.com/anchore/syft/main/install.sh | sh

# Generate SBOM
syft your-image:tag -o cyclonedx-json > sbom.json
syft your-image:tag -o spdx-json > sbom-spdx.json
```

**What it provides**: Complete inventory of all software components, versions, licenses

## Local Scanning

### Quick Start

1. **Install all tools at once**:
   ```bash
   ./scripts/security-scan.sh install
   ```

2. **Scan both images**:
   ```bash
   ./scripts/security-scan.sh all
   ```

3. **Scan specific component**:
   ```bash
   ./scripts/security-scan.sh backend
   ./scripts/security-scan.sh frontend
   ```

### Advanced Usage

**Customize severity threshold**:
```bash
SEVERITY_THRESHOLD=HIGH ./scripts/security-scan.sh all
```

**Fail on critical vulnerabilities**:
```bash
FAIL_ON_CRITICAL=true ./scripts/security-scan.sh backend
```

**Custom output directory**:
```bash
OUTPUT_DIR=./my-reports ./scripts/security-scan.sh all
```

**Scan with Docker build integration**:
```bash
# Build and scan automatically
./scripts/docker-build-push.sh backend

# Build without scanning (faster)
SKIP_SECURITY_SCAN=true ./scripts/docker-build-push.sh backend

# Build and fail on security issues
FAIL_ON_SECURITY_ISSUES=true FAIL_ON_CRITICAL=true ./scripts/docker-build-push.sh all
```

### Typical Local Workflow

```bash
# 1. Make changes to Dockerfile or code
vim backend/Dockerfile.prod

# 2. Build image locally (single platform for speed)
cd backend
docker build -f Dockerfile.prod -t opentranscribe-backend:test .
cd ..

# 3. Lint Dockerfile
hadolint backend/Dockerfile.prod

# 4. Check best practices
docker run --rm -v /var/run/docker.sock:/var/run/docker.sock \
  goodwithtech/dockle:latest opentranscribe-backend:test

# 5. Generate SBOM once
syft opentranscribe-backend:test -o cyclonedx-json > sbom.json

# 6. Scan for vulnerabilities
trivy image opentranscribe-backend:test
grype sbom:sbom.json

# Or use the all-in-one script
./scripts/security-scan.sh backend
```

## Pre-commit Hooks

### Setup

Install pre-commit hooks to automatically lint Dockerfiles before commits:

```bash
# Install pre-commit
pip install pre-commit

# Install git hooks
pre-commit install

# Install commit message hook (optional)
pre-commit install --hook-type commit-msg
```

### What Gets Checked

Every commit automatically runs:
- ✅ **Hadolint**: Lints all Dockerfile.* files
- ✅ **Gitleaks**: Scans for exposed secrets
- ✅ **Bandit**: Python security linting
- ✅ **Shellcheck**: Shell script validation
- ✅ **Ruff**: Python formatting and linting
- ✅ **Prettier**: Frontend code formatting
- ✅ **mypy**: Python type checking

### Manual Runs

```bash
# Run on staged files only
pre-commit run

# Run on all files
pre-commit run --all-files

# Run specific hook
pre-commit run hadolint-docker --all-files

# Skip hooks temporarily (not recommended)
git commit --no-verify
```

### Configuration

Edit `.pre-commit-config.yaml` to customize hooks:

```yaml
repos:
  - repo: https://github.com/hadolint/hadolint
    rev: v2.12.0
    hooks:
      - id: hadolint-docker
        args: [--config, .hadolint.yaml]
```

Edit `.hadolint.yaml` to configure Dockerfile linting rules:

```yaml
ignored:
  - DL3008  # Pin versions in apt-get (can ignore for dev)

override:
  error:
    - DL3002  # Never switch to root USER
    - DL3020  # Use COPY instead of ADD
```

## CI/CD Integration

### GitHub Actions Workflows

Two workflows are included:

1. **`security-scan.yml`** - Comprehensive image scanning
2. **`pre-commit.yml`** - Pre-commit hooks in CI

### Security Scan Workflow

**Triggers**:
- Push to main/master/develop branches
- Pull requests
- Weekly schedule (Sundays at 00:00 UTC)
- Manual workflow dispatch

**What it does**:
1. Lints Dockerfiles with Hadolint
2. Builds both backend and frontend images
3. Scans with Trivy (uploads to GitHub Security tab)
4. Checks best practices with Dockle
5. Generates SBOMs with Syft
6. Scans SBOMs with Grype
7. Scans repository dependencies
8. Uploads artifacts and reports

**Viewing Results**:
- GitHub Security tab shows Trivy findings
- Workflow logs show detailed scan results
- SBOM artifacts available for download (90-day retention)

**Manual Trigger**:
```bash
# Via GitHub UI: Actions → Security Scanning → Run workflow

# Via GitHub CLI
gh workflow run security-scan.yml -f component=backend
```

### Pre-commit Workflow

Runs all pre-commit hooks on every push/PR:
- Ensures code quality standards
- Catches issues before merge
- Provides quick feedback loop

### Badge (Optional)

Add to README.md:

```markdown
[![Security Scanning](https://github.com/davidamacey/opentranscribe/actions/workflows/security-scan.yml/badge.svg)](https://github.com/davidamacey/opentranscribe/actions/workflows/security-scan.yml)
```

## Understanding Reports

### Report Files

All reports are saved to `./security-reports/`:

```
security-reports/
├── backend-hadolint.txt          # Dockerfile linting results
├── backend-dockle.json           # CIS best practices check
├── backend-sbom.json             # SBOM (CycloneDX format)
├── backend-sbom.txt              # SBOM (human-readable table)
├── backend-trivy.json            # Trivy scan (JSON)
├── backend-trivy.txt             # Trivy scan (table)
├── backend-grype.json            # Grype scan (JSON)
├── backend-grype.txt             # Grype scan (table)
└── frontend-*                    # Same for frontend
```

### Reading Trivy Reports

```bash
# View summary
cat security-reports/backend-amd64-trivy.txt

# Query JSON for specific severity
jq '.Results[].Vulnerabilities[] | select(.Severity == "CRITICAL")' \
  security-reports/backend-amd64-trivy.json

# Count vulnerabilities by severity
jq '[.Results[].Vulnerabilities[] | .Severity] | group_by(.) | map({severity: .[0], count: length})' \
  security-reports/backend-amd64-trivy.json
```

### Reading Grype Reports

```bash
# View summary
cat security-reports/backend-amd64-grype.txt

# Query JSON for fixable vulnerabilities
jq '.matches[] | select(.vulnerability.fix.state == "fixed")' \
  security-reports/backend-amd64-grype.json

# Group by package
jq '[.matches[] | {package: .artifact.name, cve: .vulnerability.id, severity: .vulnerability.severity}] | group_by(.package)' \
  security-reports/backend-amd64-grype.json
```

### Understanding Severity Levels

- **CRITICAL**: Immediate action required (exploitable, high impact)
- **HIGH**: Should fix soon (significant risk)
- **MEDIUM**: Fix when feasible (moderate risk)
- **LOW**: Informational (minimal risk)

### Common Vulnerability Types

1. **OS Package CVEs**: Outdated system packages (fix: update base image)
2. **Language Dependencies**: Vulnerable npm/pip packages (fix: update requirements)
3. **Configuration Issues**: Misconfigurations (fix: update Dockerfile/config)
4. **Embedded Secrets**: Exposed keys/passwords (fix: use environment variables)

## Best Practices

### Development Workflow

1. **Lint before build**: Run `hadolint` on Dockerfiles before building
2. **Build with security in mind**: Use minimal base images, multi-stage builds
3. **Scan early and often**: Run scans locally before pushing
4. **Generate SBOMs**: Keep SBOMs for compliance and faster re-scanning
5. **Fix high/critical first**: Prioritize by severity and exploitability

### Dockerfile Security Tips

```dockerfile
# ✅ GOOD: Minimal base, specific version
FROM python:3.11-slim-bookworm

# ❌ BAD: Using 'latest' tag
FROM python:latest

# ✅ GOOD: Multi-stage build to reduce attack surface
FROM node:20 AS builder
WORKDIR /app
COPY package*.json ./
RUN npm ci --only=production

FROM node:20-alpine
COPY --from=builder /app/node_modules ./node_modules

# ✅ GOOD: Non-root user
RUN adduser --disabled-password --gecos '' appuser
USER appuser

# ❌ BAD: Running as root (default)
# (no USER specified)

# ✅ GOOD: Clean up in same layer
RUN apt-get update && \
    apt-get install -y --no-install-recommends pkg && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# ❌ BAD: Separate layers, cache gets huge
RUN apt-get update
RUN apt-get install -y pkg
RUN apt-get clean
```

### Continuous Improvement

- **Weekly scans**: Run scheduled scans to catch new CVEs
- **Update dependencies**: Regularly update base images and packages
- **Track trends**: Monitor vulnerability counts over time
- **Automate fixes**: Use Dependabot or Renovate for dependency updates
- **Document exceptions**: If you can't fix a vulnerability, document why

### CI/CD Best Practices

```bash
# For pull requests: Scan but don't fail
FAIL_ON_CRITICAL=false ./scripts/security-scan.sh all

# For main branch: Fail on critical issues
FAIL_ON_CRITICAL=true FAIL_ON_SECURITY_ISSUES=true ./scripts/security-scan.sh all

# For releases (v0.4.0+ release process): Full scan with strict settings
SEVERITY_THRESHOLD=MEDIUM FAIL_ON_CRITICAL=true ./scripts/security-scan.sh all
# After passing: tag with annotated tag
git tag -a v0.4.0 -m "Release v0.4.0"
git push origin v0.4.0
```

## Troubleshooting

### "Command not found" errors

Install missing tools:
```bash
./scripts/security-scan.sh install
```

Or install individually:
```bash
brew install trivy hadolint
curl -sSfL https://raw.githubusercontent.com/anchore/grype/main/install.sh | sh
curl -sSfL https://raw.githubusercontent.com/anchore/syft/main/install.sh | sh
```

### "Image not found" errors

Build or pull the image first:
```bash
docker build -f backend/Dockerfile.prod -t davidamacey/opentranscribe-backend:latest ./backend
# or
docker pull davidamacey/opentranscribe-backend:latest
```

### Slow scans

Use SBOM-based scanning for speed:
```bash
# Generate SBOM once
syft your-image:tag -o cyclonedx-json > sbom.json

# Scan from SBOM (much faster)
grype sbom:sbom.json
```

### False positives

**Trivy**: Create `.trivyignore`:
```
# Ignore specific CVE
CVE-2023-12345

# Ignore by package
pkg:pypi/package-name
```

**Grype**: Create `.grype.yaml`:
```yaml
ignore:
  - vulnerability: CVE-2023-12345
    reason: False positive - not applicable to our use case
```

### Database update failures

Clear cache and update:
```bash
# Trivy
trivy image --clear-cache
trivy image --download-db-only

# Grype
grype db update
```

### Pre-commit hook failures

Skip temporarily (not recommended):
```bash
git commit --no-verify
```

Fix issues and commit again:
```bash
# Run specific hook to see details
pre-commit run hadolint-docker --all-files

# Fix the issue
vim backend/Dockerfile.prod

# Commit again
git add backend/Dockerfile.prod
git commit -m "fix: resolve Dockerfile linting issues"
```

## Additional Resources

- [Trivy Documentation](https://aquasecurity.github.io/trivy/)
- [Grype Documentation](https://github.com/anchore/grype)
- [Syft Documentation](https://github.com/anchore/syft)
- [Hadolint Documentation](https://github.com/hadolint/hadolint)
- [Dockle Documentation](https://github.com/goodwithtech/dockle)
- [Docker Security Best Practices](https://docs.docker.com/develop/security-best-practices/)
- [CIS Docker Benchmark](https://www.cisecurity.org/benchmark/docker)

## Support

For issues or questions:
1. Check the [Troubleshooting](#troubleshooting) section
2. Review tool documentation (links above)
3. Open an issue on GitHub with scan reports attached
