# Docker Image Deployment Guide
<!-- Updated for v0.4.0 -->

This guide explains how Docker images are built and published for OpenTranscribe.

## Current Setup (Hybrid Approach)

### Automated (GitHub Actions)
**Triggers:** Push to `master` branch
**File:** `.github/workflows/docker-publish.yml`

**What it builds:**
- ✅ Frontend: AMD64 + ARM64
- ✅ Backend: AMD64 only (ARM64 disabled due to size)

**When to use:**
- Automatic on every push to master
- Can manually trigger via GitHub Actions UI

**Limitations:**
- Backend ARM64 builds disabled (13.8GB image causes runner failures)
- May still fail if backend AMD64 build hits disk limits

### Manual (Local Script)
**Location:** `./scripts/docker-build-push.sh`
**Documentation:** [scripts/README.md](../scripts/README.md)

**What it builds:**
- ✅ Frontend: AMD64 + ARM64
- ✅ Backend: AMD64 + ARM64 (full multi-arch support)

**When to use:**
- When GitHub Actions fails
- When you need ARM64 backend images
- For testing before pushing to main
- Quick iterations during development

## Recommended Workflow

### For Regular Development

1. **Make changes and commit:**
   ```bash
   git add .
   git commit -m "Your changes"
   git push origin master
   ```

2. **GitHub Actions automatically builds:**
   - Frontend: AMD64 + ARM64 ✅
   - Backend: AMD64 only ✅
   - Check progress: https://github.com/davidamacey/OpenTranscribe/actions

3. **If GitHub Actions fails or you need ARM64 backend:**
   ```bash
   ./scripts/docker-build-push.sh
   ```

### For Quick Iterations

```bash
# Only build what changed
./scripts/docker-build-push.sh auto

# Or build specific component
./scripts/docker-build-push.sh backend
./scripts/docker-build-push.sh frontend
```

### For Testing Before Production

```bash
# Fast single-platform build
PLATFORMS=linux/amd64 ./scripts/docker-build-push.sh backend

# Test the image locally
docker run -p 8080:8080 davidamacey/opentranscribe-backend:latest
```

## Image Tags

All builds create two tags:

- `latest` - Always the most recent build
- `<commit-sha>` - Specific version (e.g., `a1b2c3d`)

**Examples:**
```bash
# Pull latest
docker pull davidamacey/opentranscribe-backend:latest
docker pull davidamacey/opentranscribe-frontend:latest

# Pull specific version
docker pull davidamacey/opentranscribe-backend:1e013ea
```

## Architecture Support

| Component | AMD64 | ARM64 | Notes |
|-----------|-------|-------|-------|
| Frontend  | ✅ Auto | ✅ Auto | GitHub Actions builds both |
| Backend   | ✅ Auto | ⚠️ Manual | ARM64 needs local build (13.8GB) |
| Backend (Blackwell) | ✅ Manual | ✅ Manual | `Dockerfile.blackwell` for DGX Spark (SM_121) |

### Blackwell / DGX Spark

For NVIDIA Blackwell GPUs (DGX Spark, GB10/GB20x), a specialized image is required. See `docs/BLACKWELL_SETUP.md` for full details.

```bash
# Build Blackwell backend image
docker build -t opentranscribe-backend-blackwell:latest \
    -f backend/Dockerfile.blackwell backend/

# Start with Blackwell overlay (auto-detected by opentr.sh on SM_12x hardware)
./opentr.sh start dev
```

### Compose Overlay Reference

| Overlay file | Purpose |
|---|---|
| `docker-compose.yml` | Base (all environments) |
| `docker-compose.override.yml` | Dev hot-reload (auto-loaded) |
| `docker-compose.prod.yml` | Production (Docker Hub images) |
| `docker-compose.local.yml` | Local builds (`pull_policy: never`) |
| `docker-compose.nginx.yml` | NGINX reverse proxy |
| `docker-compose.pki.yml` | mTLS / PKI certificate auth |
| `docker-compose.blackwell.yml` | Blackwell GPU (SM_121) |
| `docker-compose.gpu-scale.yml` | Multi-GPU worker scaling (parallel workers on one GPU) |
| *(profile: `gpu-split`)* | GPU split mode — transcribe/diarize workers in main `docker-compose.yml` |

**PKI deployment** (requires NGINX):
```bash
./opentr.sh start prod --build --with-pki
# Access at https://localhost:5182 with client certificate
```

## Troubleshooting

### GitHub Actions Failing

**Symptom:** Build cancelled or runs out of disk space
**Solution:** Use manual script
```bash
./scripts/docker-build-push.sh
```

### Need ARM64 Backend

**Symptom:** Docker pull fails on ARM64 systems (Apple Silicon, etc.)
**Solution:** Build locally with full multi-arch support
```bash
./scripts/docker-build-push.sh backend
```

### Slow Builds

**Solution:** Build single platform or use auto-detection
```bash
# Single platform (much faster)
PLATFORMS=linux/amd64 ./scripts/docker-build-push.sh

# Only build what changed
./scripts/docker-build-push.sh auto
```

### Not Logged Into Docker Hub

**Symptom:** "denied: requested access to resource"
**Solution:**
```bash
docker login
# Enter your Docker Hub credentials
```

## Future Improvements

See [Issue #81](https://github.com/davidamacey/OpenTranscribe/issues/81) for planned enhancements:

- [ ] Self-hosted GitHub Actions runner (solves disk space issue)
- [ ] Backend image size optimization (13.8GB → <10GB)
- [ ] Smart change detection in GitHub Actions
- [ ] Automated ARM64 backend builds

## Build Times Reference

**Frontend** (~2 minutes total):
- AMD64: ~1 min
- ARM64: ~1 min
- Size: ~51 MB

**Backend** (~15-30 minutes total):
- AMD64: ~8-15 min
- ARM64: ~8-15 min
- Size: ~13.8 GB

## Blackwell GPU Support

NVIDIA Blackwell GPUs (GB10x/GB20x series, e.g. DGX Spark) require a separate Dockerfile that links against the Blackwell CUDA libraries. Pass `--blackwell` to the build script:

```bash
./scripts/docker-build-push.sh --blackwell backend
```

This builds from `backend/Dockerfile.blackwell` instead of `backend/Dockerfile.prod`.

## Lite Mode (GPU-Free) Images

`DEPLOYMENT_MODE=lite` disables local GPU workers and routes transcription to cloud ASR providers. The same backend image supports both modes — set `DEPLOYMENT_MODE=lite` in `.env` before starting:

```bash
# .env
DEPLOYMENT_MODE=lite        # full (default) or lite
DEEPGRAM_API_KEY=your-key   # configure at least one cloud ASR provider
```

## Related Files

- [scripts/docker-build-push.sh](../scripts/docker-build-push.sh) - Local build script
- [scripts/README.md](../scripts/README.md) - Detailed script documentation
- [.github/workflows/docker-publish.yml](../.github/workflows/docker-publish.yml) - GitHub Actions workflow
- [backend/Dockerfile.prod](../backend/Dockerfile.prod) - Backend production Dockerfile
- [backend/Dockerfile.blackwell](../backend/Dockerfile.blackwell) - Blackwell GPU Dockerfile
- [frontend/Dockerfile.prod](../frontend/Dockerfile.prod) - Frontend production Dockerfile
