---
name: docker-build-push
description: Build and push OpenTranscribe production images (backend, lite backend, frontend, docs) to Docker Hub, multi-arch via the remote builder. Use when the user says "build the production images", "push to Docker Hub", "publish images", "multi-arch build", or is cutting a release that ships new container images.
---

# Docker build & push (production images)

**Ask the script what it builds — never assume, and never copy the table into another file:**

```bash
./scripts/docker-build-push.sh list-platforms   # component<TAB>capability<TAB>platforms
./scripts/security-scan.sh list-components      # must be the SAME key set (asserted at runtime)
```

Current output (re-derive it, do not trust this paste):

```
backend     cuda        linux/amd64
blackwell   blackwell   linux/arm64
docs        multiarch   linux/amd64,linux/arm64
frontend    multiarch   linux/amd64,linux/arm64
lite        cpu         linux/amd64,linux/arm64
```

## Tag grammar (issue #680)

Capability lives in the **repository** — a manifest index cannot span repos — and is **restated
in the tag**:

- `davidamacey/opentranscribe-backend` (CUDA): leg `vX.Y.Z-cuda-amd64` → index `vX.Y.Z` →
  `:latest` as a digest-copy of the index. `vX.Y.Z-cuda-arm64` is **reserved and not built**
  (no aarch64 CUDA torch wheel at the pinned version; `onnxruntime-gpu` has zero aarch64 wheels;
  diar-native ships no CUDA arm64 sidecar).
- `davidamacey/opentranscribe-backend-lite` (CPU): legs `vX.Y.Z-cpu-amd64` + `vX.Y.Z-cpu-arm64`
  → index `vX.Y.Z` → `:latest` digest-copy. **This is the arm64 image** — `opentranscribe.sh`
  defaults an arm64 host to it.
- `frontend` / `docs`: no capability legs, one multi-platform build under `vX.Y.Z`.
- `blackwell`: `vX.Y.Z-blackwell-arm64` + `:blackwell`. Built **only on request**, never by
  `all`/`auto`, and not published by the release pipeline.

Legs are pushed individually and the index is assembled with `buildx imagetools create`, so the
index provably contains exactly those legs — that is what `scripts/release/80-publish.sh` then
verifies (leg has exactly one platform; index platform set matches exactly, missing **and** extra
both fail; same-capability legs are equivalent within a size-ratio bound).

## Usage

**`USE_REMOTE_BUILDER=true` for anything including `linux/arm64`** (today: `lite`) — without it
arm64 runs under QEMU, 2–3 h instead of 15–30 min.

```bash
USE_REMOTE_BUILDER=true ./scripts/docker-build-push.sh              # all: backend, lite, frontend, docs
USE_REMOTE_BUILDER=true ./scripts/docker-build-push.sh backend      # full/CUDA backend only
USE_REMOTE_BUILDER=true ./scripts/docker-build-push.sh lite         # CPU backend only (amd64 + arm64)
./scripts/docker-build-push.sh frontend|docs                        # one service
./scripts/docker-build-push.sh auto                                 # only git-changed components
./scripts/docker-build-push.sh blackwell                            # on request only, never in `all`
SKIP_SECURITY_SCAN=true ./scripts/docker-build-push.sh backend      # quick iteration
BUILD_MODE=local ./scripts/docker-build-push.sh all                 # build + --load, push NOTHING
```

⚠️ **`PLATFORMS` is an explicit OVERRIDE, not a default.** Leave it unset and each component
builds only its own declared platforms. It used to default to `linux/amd64,linux/arm64` for
*every* component — the mechanism by which a CPU-only arm64 backend was published under the CUDA
image's tag (issue #680). Set it only to force a one-off:

```bash
PLATFORMS=linux/amd64 ./scripts/docker-build-push.sh lite   # skip the arm64 leg this once
```

Other knobs: `PUSH_LATEST=false` (the release flow's default — `:latest` moves later by digest in
`scripts/release/90-promote.sh`), `DRY_RUN=true`, `NO_CACHE=true`, `FAIL_ON_SECURITY_ISSUES=true`.

## Don't publish by hand for a release

`./scripts/release.sh run <version>` owns build → scan → rehearse → publish → smoke → promote.
`.github/workflows/docker-publish.yml` is **retired** (it published under the old `latest-amd64`
grammar and would overwrite the promoted `:latest` index).

Full docs: `scripts/README.md`, `scripts/CLAUDE.md`.
