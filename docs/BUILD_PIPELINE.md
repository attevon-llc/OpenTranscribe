# OpenTranscribe Complete Build Pipeline

## Overview

The complete build pipeline executes all build, security scanning, and packaging tasks required to prepare OpenTranscribe for deployment. It's designed to run unattended in the background for 2-3 hours.

## What It Does

The pipeline executes two main phases:

### Phase 1: Docker Build & Security Scanning (~30-45 minutes)
- Builds multi-architecture Docker images (AMD64 + ARM64)
- Pushes images to Docker Hub with version tags
- Runs comprehensive security scans:
  - **Hadolint**: Dockerfile linting
  - **Dockle**: CIS Docker best practices
  - **Syft**: Software Bill of Materials (SBOM) generation
  - **Trivy**: Vulnerability scanning
  - **Grype**: Additional vulnerability scanning
- Generates security reports in `./security-reports/`

### Phase 2: Offline Package Creation (~1.5-2 hours)
- Pulls all Docker images from registry
- Downloads AI models (~38GB)
- Packages configuration files and scripts
- Creates checksums for integrity verification
- Compresses final package (~15-20GB)
- Output: `opentranscribe-offline-v{version}.tar.xz`

## Prerequisites

### Required Tools
- Docker 20.10+ with buildx support
- Git
- tar, xz, jq
- 100GB+ free disk space

### Required Accounts & Credentials
1. **Docker Hub**: Must be logged in
   ```bash
   docker login
   ```

2. **HuggingFace Token**: Required for AI model downloads
   - Get your token: https://huggingface.co/settings/tokens
   - Set in environment or `.env` file:
   ```bash
   export HUGGINGFACE_TOKEN=hf_your_token_here
   ```
   Or add to `.env`:
   ```
   HUGGINGFACE_TOKEN=hf_your_token_here
   ```

### Security Scanning Tools
The script will automatically install these if missing:
- Trivy
- Grype
- Syft
- Hadolint
- Dockle (runs via Docker)

## Usage

### Interactive Mode (Recommended for First-Time Use)

**Run with interactive prompts and validation:**
```bash
./scripts/build-all.sh --interactive
```

This will:
- Check all prerequisites (Docker, HuggingFace token, disk space)
- Prompt for version tag
- Display configuration summary
- Ask for confirmation before starting

### Direct Execution

**Run immediately without prompts:**
```bash
./scripts/build-all.sh                    # Auto-detected version
./scripts/build-all.sh v2.1.0             # Specific version
./scripts/build-all.sh -i v2.1.0          # Interactive with version
```

### Background Execution

**Run with nohup (survives terminal disconnection):**
```bash
nohup ./scripts/build-all.sh > build-$(date +%Y%m%d-%H%M%S).log 2>&1 &
```

**Monitor progress:**
```bash
# Watch the log file in real-time
tail -f build-*.log

# Check the last 50 lines
tail -50 build-*.log

# Search for errors
grep ERROR build-*.log

# Check if process is still running
ps aux | grep build-all.sh
```

**Stop the build (if needed):**
```bash
# Find the process ID
ps aux | grep build-all.sh

# Kill the process
kill <PID>
```

### Environment Variables

Customize the build behavior:

```bash
# Use different Docker Hub username
DOCKERHUB_USERNAME=myusername nohup ./scripts/build-all.sh &

# Skip security scanning for faster builds
SKIP_SECURITY_SCAN=true nohup ./scripts/build-all.sh &

# Fail build on CRITICAL vulnerabilities (CI/CD mode)
FAIL_ON_CRITICAL=true FAIL_ON_SECURITY_ISSUES=true ./scripts/build-all.sh

# Build only for AMD64 (faster, single platform)
PLATFORMS=linux/amd64 nohup ./scripts/build-all.sh &

# Custom version tag
nohup ./scripts/build-all.sh v2.1.0-rc1 &
```

## Timeline

Total estimated time: **2-3 hours**

| Phase | Task | Duration |
|-------|------|----------|
| Pre-flight | Validation checks | 1-2 min |
| Phase 1 | Backend build (AMD64 + ARM64) | 15-30 min |
| Phase 1 | Frontend build (AMD64 + ARM64) | 2-4 min |
| Phase 1 | Security scanning (both images) | 10-15 min |
| Phase 2 | Pull Docker images | 10-20 min |
| Phase 2 | Download AI models | 30-60 min |
| Phase 2 | Package compression | 30-60 min |

## Output Artifacts

After successful completion:

### 1. Docker Images (on Docker Hub)
```
davidamacey/opentranscribe-backend:latest
davidamacey/opentranscribe-backend:{commit-sha}
davidamacey/opentranscribe-frontend:latest
davidamacey/opentranscribe-frontend:{commit-sha}
```

### 2. Security Reports (`./security-reports/`)
```
backend-hadolint.txt          # Dockerfile linting
backend-dockle.json           # CIS best practices
backend-sbom.json             # Software Bill of Materials
backend-sbom.txt              # Human-readable SBOM
backend-trivy.json            # Trivy vulnerability scan
backend-trivy.txt             # Human-readable Trivy report
backend-grype.json            # Grype vulnerability scan
backend-grype.txt             # Human-readable Grype report

frontend-hadolint.txt
frontend-dockle.json
frontend-sbom.json
frontend-sbom.txt
frontend-trivy.json
frontend-trivy.txt
frontend-grype.json
frontend-grype.txt
```

### 3. Offline Package (`./offline-package-build/`)
```
opentranscribe-offline-v{version}.tar.xz        # Compressed package (~15-20GB)
opentranscribe-offline-v{version}.tar.xz.sha256 # Checksum file
```

## Verification

After the build completes, verify the artifacts:

### 1. Check Build Summary
```bash
tail -100 build-*.log
```

### 2. Verify Docker Images
```bash
docker pull davidamacey/opentranscribe-backend:latest
docker pull davidamacey/opentranscribe-frontend:latest
```

### 3. Review Security Reports
```bash
ls -lh security-reports/
cat security-reports/backend-amd64-trivy.txt
cat security-reports/frontend-amd64-trivy.txt
```

### 4. Verify Offline Package
```bash
cd offline-package-build
sha256sum -c opentranscribe-offline-v*.tar.xz.sha256
```

## Typical Workflow

### For Regular Releases

```bash
# 1. Ensure latest code is on master
git checkout master
git pull origin master

# 2. Verify prerequisites
docker login
grep HUGGINGFACE_TOKEN .env

# 3. Start the build pipeline
nohup ./scripts/build-all.sh v2.1.0 > build-$(date +%Y%m%d-%H%M%S).log 2>&1 &

# 4. Note the process ID
echo $! > build.pid

# 5. Monitor progress (optional)
tail -f build-*.log

# 6. Disconnect from terminal
# Build continues running via nohup

# 7. Later, check status
tail -100 build-*.log

# 8. Verify completion
ls -lh offline-package-build/
ls -lh security-reports/
```

### For Testing/Development

```bash
# Faster build for testing (skip some features)
SKIP_SECURITY_SCAN=true \
PLATFORMS=linux/amd64 \
./scripts/build-all.sh test-build
```

## Troubleshooting

### Build Fails: "Not logged into Docker Hub"
```bash
docker login
```

### Build Fails: "HUGGINGFACE_TOKEN not set"
```bash
export HUGGINGFACE_TOKEN=hf_your_token_here
# Or add to .env file
echo "HUGGINGFACE_TOKEN=hf_your_token_here" >> .env
```

### Build Fails: "Insufficient disk space"
```bash
# Check available space
df -h .

# Clean Docker cache
docker system prune -a --volumes

# Remove old build artifacts
rm -rf offline-package-build/
rm -rf security-reports/
```

### Security Scan Fails
```bash
# Skip security scanning
SKIP_SECURITY_SCAN=true ./scripts/build-all.sh
```

### Build is Too Slow
```bash
# Build only AMD64 (skip ARM64)
PLATFORMS=linux/amd64 ./scripts/build-all.sh

# Skip security scanning
SKIP_SECURITY_SCAN=true ./scripts/build-all.sh

# Both optimizations
PLATFORMS=linux/amd64 SKIP_SECURITY_SCAN=true ./scripts/build-all.sh
```

### Monitor Running Build
```bash
# Find the process
ps aux | grep build-all.sh

# Monitor log file
tail -f build-*.log

# Check current phase
grep -E "PHASE|Building|Downloading|Compressing" build-*.log | tail -10

# Check for errors
grep -i error build-*.log
```

### Kill Stuck Build
```bash
# Find process ID
ps aux | grep build-all.sh

# Kill it
kill <PID>

# Force kill if needed
kill -9 <PID>
```

## Advanced Usage

### CI/CD Integration

```bash
#!/bin/bash
# Example GitHub Actions / Jenkins pipeline

set -e

# Strict mode for CI
export FAIL_ON_CRITICAL=true
export FAIL_ON_SECURITY_ISSUES=true

# Run pipeline
./scripts/build-all.sh "${GIT_TAG}"

# Upload artifacts
aws s3 cp offline-package-build/*.tar.xz s3://releases/
aws s3 cp security-reports/ s3://security-reports/ --recursive
```

### Custom Security Thresholds

```bash
# Only fail on CRITICAL (not HIGH)
FAIL_ON_CRITICAL=true \
FAIL_ON_SECURITY_ISSUES=false \
./scripts/build-all.sh
```

### Multi-Stage Execution

```bash
# Phase 1 only (Docker build + security)
./scripts/docker-build-push.sh all

# Phase 2 only (offline package)
./scripts/build-offline-package.sh v2.1.0
```

## Next Steps After Build

1. **Verify Package Checksum**
   ```bash
   cd offline-package-build
   sha256sum -c opentranscribe-offline-v*.tar.xz.sha256
   ```

2. **Review Security Reports**
   ```bash
   cat security-reports/backend-amd64-trivy.txt
   cat security-reports/frontend-amd64-trivy.txt
   ```

3. **Test Docker Images**
   ```bash
   docker run --rm davidamacey/opentranscribe-backend:latest --version
   ```

4. **Transfer Offline Package**
   - Copy `.tar.xz` and `.sha256` to target systems
   - Use USB drive, network transfer, or physical media

5. **Deploy to Target System**
   ```bash
   # On target system
   tar -xf opentranscribe-offline-v*.tar.xz
   cd opentranscribe-offline-v*/
   sudo ./install.sh
   ```

## Quick Start

For a guided setup with prerequisite validation:
```bash
./scripts/build-all.sh --interactive
```

This will check all prerequisites, prompt for configuration, and start the build after confirmation.

## Related Documentation

- [scripts/README.md](../scripts/README.md) - Individual script documentation
- [CLAUDE.md](../CLAUDE.md) - Project architecture and development guide
- [README-OFFLINE.md](../README-OFFLINE.md) - Offline deployment guide
- [SECURITY_SCANNING.md](SECURITY_SCANNING.md) - Security scanning details
