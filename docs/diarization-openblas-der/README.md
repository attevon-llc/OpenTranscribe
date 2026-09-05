# OpenBLAS / diar-server DER risk — measured on amd64, NOT measured on arm64 (issue #721)

`diar-server` is built `openblas-system`: it carries an ELF `NEEDED` entry for
`libopenblas.so.0` and links whatever the **host image** provides, not what upstream
validated against. OpenBLAS 0.3.28/0.3.29 shipped an **arm64-only** GEMM→GEMV forwarding
defect that moved upstream's AMI-16 DER from **13.8% → 48.7%**, and `diar-server
verify-models` passes all five stages throughout — a plausible speaker count with badly
wrong attribution. No smoke test, image scan or build gate can see it. Only a DER
measurement against hand-labelled ground truth can.

This document records what was measured, and — just as importantly — what was not.

## The version gap is real, and it is not hypothetical

| Where | Base | `libopenblas0` | Evidence |
|---|---|---|---|
| diar-native validates on | Ubuntu 24.04 | **0.3.26+ds-1ubuntu0.1** | `dpkg-query` in `davidamacey/diar-native:0.3.1` |
| `davidamacey/diar-native:0.3.1-cpu-arm64` | Ubuntu 24.04 | **0.3.26+ds-1ubuntu0.1** | `/var/lib/dpkg/status` extracted from the image |
| **`Dockerfile.prod` / `Dockerfile.lite` runtime** | `python:3.13-slim-trixie` | **0.3.29+ds-3** | `dpkg-query` in `opentranscribe-backend:latest` |
| **Debian trixie `arm64` archive** | — | **0.3.29+ds-3** | `deb.debian.org/debian/dists/trixie/main/binary-arm64/Packages.gz` |

Both Dockerfiles install a bare, unversioned `libopenblas0`
(`backend/Dockerfile.prod:147`, `backend/Dockerfile.lite:111`), and **there is no apt pin
anywhere in the repo** — no `preferences.d`, no `apt-mark hold`, no `=version` on any
package in any `apt-get install` line.

⚠️ **The validated OpenBLAS does not travel with the binary.** `Dockerfile.lite:148-153`
copies only `diar-server`, three ORT libs and `smoke.wav` out of the diar-native image.
The 0.3.26 that upstream validated against is left behind in the source image, and the
binary is re-linked against trixie's 0.3.29 at container start.

## What was measured — amd64, 2026-09-05

A controlled A/B: the **same** `diar-server` binary (sha256
`4745ca3f62519d2fec32c76a3c69297b99fcd0c48cfee01e210a20c62d10254c`, verified byte-identical
in both images), the same model export, the same audio, both arms pinned `DIAR_MODE=cpu`
(CPU is the bit-reproducible device — CUDA is not deterministic with itself, see
`backend/app/transcription/CLAUDE.md`). The only intended difference is the OpenBLAS the
loader resolves, read back from `/proc/1/maps` rather than assumed.

Clip: `karpathy_10m.wav` (600 s, 2 speakers) against the maintainer's hand labels in
`reference.rttm`, cropped to 0–600 s.

| Arm | image | OpenBLAS actually mapped | segs | spk | DER(0.25) | DER(0) | JER |
|---|---|---|---|---|---|---|---|
| A — shipped | `opentranscribe-backend:latest` | `libopenblasp-r0.3.29.so` | 90 | 2 | **0.0669** | 0.0913 | 0.0729 |
| B — control | `davidamacey/diar-native:0.3.1` | `libopenblasp-r0.3.26.so` | 90 | 2 | **0.0669** | 0.0913 | 0.0729 |
| CONTROL — broken | (arm A, all speech collapsed to 1 speaker) | — | 90 | 1 | 0.3301 | 0.3570 | 0.6449 |

- **DER 6.69% on the shipped image**, against `diar-native-der-parity.py`'s own ≤ 0.15 gate.
- The two arms are **bit-identical**: `segments`, `exclusive_segments` and `rttm` all equal,
  and the 2×256 PLDA centroids differ by **exactly 0.0**.
- The **collapsed control** is the falsifiability check. Without it, "both arms agree" is
  equally consistent with a metric that cannot move. It moves.

**Conclusion for amd64: OpenBLAS 0.3.29 is in band.** The version gap has no measurable
effect on this architecture.

> These are **not** the same numbers as `scripts/diar-native-der-parity.py`'s recorded
> `native` row (103 segs, DER 0.0515) and should not be read as a regression against it.
> That run was **CUDA**, driven through `ModelManager.diarize(audio)`; this one is
> **`DIAR_MODE=cpu`**, posted straight to `/diarize` with a `wav_path`. Device and entry path
> both differ, and `/diarize` output is explicitly not bit-identical across devices. Both sit
> far inside the ≤ 0.15 gate. What carries the argument here is the *within-run* comparison —
> two arms, one variable — not the absolute value against a different harness.

### A caveat that weakens the mechanism story (but not the DER result)

Forcing a completely different BLAS kernel on the *same* 0.3.29 —
`OPENBLAS_CORETYPE=Nehalem` (SSE) instead of the auto-detected `Haswell` (AVX2), confirmed
switched via `OPENBLAS_VERBOSE=2` printing `Core: Nehalem` — **also** produced bit-identical
output and a 0.0 centroid delta.

So the A/B's bit-identity is **not** proof that a heavily-exercised OpenBLAS path was
stressed and found equivalent. The honest reading is that diarization's numeric work is
dominated by ONNX Runtime's own MLAS, and the BLAS calls that remain (PLDA scoring over
256-d vectors for 2 speakers) are small enough that every *correct* kernel agrees
bit-for-bit.

That distinction matters for arm64: the 0.3.28/0.3.29 defect is an **incorrectness**, not a
rounding difference. Kernels agreeing when all are correct says nothing about what a broken
one would do. **This is why the amd64 result cannot be extrapolated.**

The primary evidence is therefore the DER number itself — 6.69% on the artifact we ship —
not the A/B delta.

## What was NOT measured

**arm64 DER is NOT MEASURED.** It was not measured here and could not be:

- This host is **x86_64 only** (`uname -m` → `x86_64`); there is no aarch64 machine available.
- **QEMU cannot answer it.** OpenBLAS selects kernels by runtime CPU detection, so an
  emulated run exercises the wrong code path — upstream says so explicitly. (Moot in
  practice here anyway: no `binfmt_misc` arm64 handler is registered, so running the arm64
  image fails with `exec format error`.)

This is architecture-blocked verification, which **issue #713** already tracks. It is
recorded there rather than as a new issue.

### The exposure is live on `master`, not a proposal

`./scripts/docker-build-push.sh list-platforms` reports `lite  cpu
linux/amd64,linux/arm64` — so the `lite` image ships an arm64 leg today, and it is the only
arm64 backend. That leg pairs upstream's **aarch64** `diar-server` with Debian trixie's
**0.3.29** — precisely the architecture-and-version combination upstream measured at 48.7%
DER — and nothing in the build, the scan, the smoke test or `verify-models` would report it.

### Existing tests cannot cover this

`backend/tests/integration/test_boundary_regression.py` asserts WSER, bleed-island count and
collar-0 DER per variant, but it **replays frozen inference** (`*.rawinfer.json` is a
serialized `Engine.run_gpu_stage()` output) and only exercises the CPU smoother. It is
structurally incapable of detecting an inference-time OpenBLAS regression: the diarizer never
runs. Do not treat a green boundary-regression run as coverage for this hazard.

## Reproducing it, on any architecture

`scripts/diar-openblas-der-ab.py` is the harness that produced the table above. It prints
`uname -m` and labels its result a claim about that architecture only, reads the mapped
OpenBLAS out of `/proc/1/maps`, and includes the collapsed-speaker control.

```bash
CLIP=benchmark/diarization-boundary/karpathy/karpathy_kwSVtQ7dziU
./scripts/diar-openblas-der-ab.py \
    --audio "$CLIP/karpathy_10m.wav" --reference "$CLIP/reference.rttm" --seconds 600
```

The audio is gitignored; see that folder's `download.sh` and `provenance.md`.

⚠️ `reference.rttm` labels the whole 66-minute source video while the clip is 10 minutes.
Scoring uncropped reports **DER ≈ 0.86 for every arm** — a fake catastrophe that reads
exactly like a real one. The script refuses to run when `--seconds` disagrees with the
decoded audio by more than 5 s; do not widen that tolerance.

## Options if arm64 does reproduce

1. **Ship diar-native's own `libopenblas`** rather than the distro's, so the validated
   pairing travels with the binary. Cleanest fit: the Dockerfiles already `COPY --from` the
   binary and ORT libs out of that image. Measured safe on amd64 (arm B above *is* 0.3.26),
   untested on arm64.
2. **Pin OpenBLAS** to a pre-0.3.28 version. Note trixie carries only 0.3.29, so this means a
   cross-suite pin — real maintenance burden and ABI risk.
3. **`OPENBLAS_CORETYPE`** to steer kernel selection away from the forwarding path. The knob
   demonstrably works (see the Nehalem probe above), but which core type avoids the defect
   needs upstream input.
4. **Hold the arm64 leg** out of the published index until it is measured.
