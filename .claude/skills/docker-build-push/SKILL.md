---
name: docker-build-push
description: Build and push OpenTranscribe production images (backend/frontend) to Docker Hub, multi-arch via the remote builder. Use when the user says "build the production images", "push to Docker Hub", "publish images", "multi-arch build", or is cutting a release that ships new container images.
---

# Docker build & push (production images)

**Always** use `USE_REMOTE_BUILDER=true` for multi-arch — without it ARM64 falls back to QEMU (2–3 hours vs 15–30 min via the Mac Studio builder).

```bash
USE_REMOTE_BUILDER=true ./scripts/docker-build-push.sh                    # both services
USE_REMOTE_BUILDER=true ./scripts/docker-build-push.sh backend|frontend   # one service
USE_REMOTE_BUILDER=true SKIP_SECURITY_SCAN=true ./scripts/docker-build-push.sh backend  # quick iteration
USE_REMOTE_BUILDER=true ./scripts/docker-build-push.sh auto               # auto-detect changed
PLATFORMS=linux/amd64 ./scripts/docker-build-push.sh backend              # single-arch (no remote needed)
```

Full docs: `scripts/README.md`.
