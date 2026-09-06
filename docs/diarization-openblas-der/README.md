# OpenBLAS / diar-server DER — measured on amd64 AND arm64, closed by bundling (issue #721)

`diar-server` is built `openblas-system`: it carries an ELF `NEEDED` entry for
`libopenblas.so.0` and links whatever the **host image** provides, not what upstream
validated against. Upstream's `docs/DEPLOYMENT.md` reports that on **linux/arm64** an
OpenBLAS change moved AMI-16 full DER from **13.8% → 48.7%**, root-caused to 0.3.28/0.3.29's
arm64-only GEMM→GEMV forwarding — and that `diar-server verify-models` passes all five
stages throughout, so a plausible speaker count sits on badly wrong attribution. No smoke
test, image scan or build gate can see that. Only a DER measurement against hand-labelled
ground truth can.

This document records what was measured — on **both** architectures — and what was changed
in response.

## ⚠️ Read this before quoting 48.7% anywhere

**The defect did not reproduce on either architecture we can execute.** Measured
2026-09-05: **amd64 0.0669 DER on 0.3.29, identical to a 0.3.26 control**; and on **native
aarch64** (Apple M2 Max, not QEMU) **0.3.29 also measured 0.0669 — identical to the 0.3.26
control and to amd64**, with bit-identical segments. A direct **2,016-case `cblas_?gemm`
probe** over the GEMM→GEMV forwarding shapes found **0 mismatches**.

So this change is **not** repairing an active 48.7% regression in what we ship, and the PR
should not be read that way. What it does is narrower and still worth having:

1. **It removes unpinned version drift.** `libopenblas0` is installed bare and unversioned
   in both Dockerfiles, with **no apt pin anywhere in the repo**. Today's answer is "trixie
   ships 0.3.29 and 0.3.29 is fine here" — but nothing holds that, and the next Debian bump
   moves the library under the binary again with nothing in the build, the scan or the smoke
   test able to notice. Bundling makes the shipped pairing the one upstream validated,
   independent of the base image's package set.
2. **It covers the aarch64 generations we cannot test.** Graviton3/4 select **SVE** kernels,
   which the available hardware physically cannot run — forcing them SIGILLs. Those are
   exactly the code paths upstream's report implicates, and they remain unproven. Bundling
   the validated build is insurance for them rather than a measured fix.

**Status: closed by bundling.** Both `backend/Dockerfile.prod` and `backend/Dockerfile.lite`
now ship diar-native's own validated **0.3.26** beside the binary and point the binary's
**DT_RPATH** at it, so the tested pairing travels with the artifact instead of being
re-formed from whatever the runtime image happens to install. Measured DER is unchanged at
**0.0669** on both architectures — as expected, since neither showed a difference to begin
with.

## The version gap is real (its consequences turned out not to be)

| Where | Base | `libopenblas0` | Evidence |
|---|---|---|---|
| diar-native validates on | Ubuntu 24.04 | **0.3.26+ds-1ubuntu0.1** | `dpkg-query` in `davidamacey/diar-native:0.3.1` |
| `davidamacey/diar-native:0.3.1-cpu-arm64` | Ubuntu 24.04 | **0.3.26+ds-1ubuntu0.1** | `/var/lib/dpkg/status` extracted from the image |
| **`Dockerfile.prod` / `Dockerfile.lite` runtime** | `python:3.13-slim-trixie` | **0.3.29+ds-3** | `dpkg-query` in `opentranscribe-backend:latest` |
| **Debian trixie `arm64` archive** | — | **0.3.29+ds-3** | `deb.debian.org/debian/dists/trixie/main/binary-arm64/Packages.gz` |

Both Dockerfiles install a bare, unversioned `libopenblas0`, and **there is no apt pin
anywhere in the repo** — no `preferences.d`, no `apt-mark hold`, no `=version` on any
package in any `apt-get install` line. That apt install is still there, deliberately (see
"Why the apt `libopenblas0` stays" below); it is simply no longer what `diar-server` loads.

⚠️ **The validated OpenBLAS used not to travel with the binary.** Both Dockerfiles copied
only `diar-server`, the ORT libs and `smoke.wav` out of the diar-native image. The 0.3.26
upstream validated against was left behind in the source image, and the binary was
re-linked against trixie's 0.3.29 at container start. **That drift is what this change
closes** — not a measured accuracy defect, which neither architecture showed.

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
| A — shipped, **before** the fix | `opentranscribe-backend:latest` | `/usr/lib/…/libopenblasp-r0.3.29.so` | 90 | 2 | **0.0669** | 0.0913 | 0.0729 |
| B — control | `davidamacey/diar-native:0.3.1` | `/usr/lib/…/libopenblasp-r0.3.26.so` | 90 | 2 | **0.0669** | 0.0913 | 0.0729 |
| A′ — shipped, **after** the fix | `opentranscribe-backend:blas721` | `/opt/diar-native/lib/libopenblasp-r0.3.26.so` | 90 | 2 | **0.0669** | 0.0913 | 0.0729 |
| CONTROL — broken | (arm A, all speech collapsed to 1 speaker) | — | 90 | 1 | 0.3301 | 0.3570 | 0.6449 |

- **DER 6.69% on the shipped image**, against `diar-native-der-parity.py`'s own ≤ 0.15 gate.
- All three real arms are **bit-identical**: `segments`, `exclusive_segments` and `rttm` all
  equal, and the 2×256 PLDA centroids differ by **exactly 0.0**.
- The **collapsed control** is the falsifiability check. Without it, "both arms agree" is
  equally consistent with a metric that cannot move. It moves.
- Row A′ is the **re-measurement after bundling**, run with the same harness, same clip,
  same 600 s crop. Its `openblas actually mapped` column is also the attribution check: a
  run that still reported `0.3.29` would have been measuring the old image, and its 0.0669
  would have meant nothing.

**Conclusion for amd64: OpenBLAS 0.3.29 was in band, and bundling 0.3.26 changed nothing
measurable.** The version gap has no effect on this architecture in either direction, which
is exactly why the fix is low-risk here. On its own it says nothing about aarch64 — that
took a separate run on real hardware, below.

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

That distinction matters: the 0.3.28/0.3.29 defect upstream reports is an **incorrectness**,
not a rounding difference. Kernels agreeing when all are correct says nothing about what a
broken one would do — so the amd64 A/B could never have settled aarch64 by itself. That
needed its own run.

The primary evidence is therefore the DER number itself — 6.69% on the artifact we ship —
not the A/B delta.

## What was measured — native aarch64, 2026-09-05

Run on an **Apple M2 Max**, natively — **not** QEMU, which cannot answer this question at
all because OpenBLAS selects kernels by runtime CPU detection and an emulated run exercises
the wrong code path. (Upstream says so explicitly.)

- **Debian trixie's 0.3.29 measured 0.0669 DER on aarch64** — identical to the 0.3.26
  control, and identical to both amd64 arms. Segments bit-identical.
- A direct **`cblas_?gemm` probe, 2,016 cases**, sweeping the shapes that trigger the
  GEMM→GEMV forwarding path: **0 mismatches** between the two versions.

**Conclusion: the reported defect does not fire on any kernel this project can execute.**
Upstream's 48.7% observation is not contradicted — it is simply not reproducible on the
NEON/AdvSIMD kernels an M2 (or any hardware available here) selects.

## What is still NOT measured — and it is the part that justifies the change

**Graviton3/Graviton4 are unproven, and cannot be proven here.** They select OpenBLAS's
**SVE** kernel family; the available hardware physically cannot execute those instructions,
and forcing the selection **SIGILLs**. Those are precisely the code paths upstream's report
implicates, so "0.3.29 is fine on M2" does not transfer to them.

This is architecture-blocked verification, which **issue #713** already tracks. It is
recorded there rather than as a new issue.

**Bundling is insurance, not repair.** Shipping the validated 0.3.26 means every aarch64
deployment — SVE-selecting ones included — runs the library upstream validated on, so
upstream's own 13.8% figure is the applicable evidence rather than an untested pairing. The
same move also removes the unpinned-drift exposure on **every** architecture: nothing in
this repo pins `libopenblas0`, so today's measured-good 0.3.29 is one Debian bump away from
being a different library, silently.

### Where the residual exposure actually is

`./scripts/docker-build-push.sh list-platforms` reports `lite  cpu
linux/amd64,linux/arm64` — so the `lite` image ships an arm64 leg today, and it is the only
arm64 backend. Before this change that leg paired upstream's **aarch64** `diar-server` with
Debian trixie's **0.3.29**.

On the aarch64 hardware available to us that pairing is **fine** (measured above). The
exposure that remains is narrower and entirely about what we cannot execute or control:

- a **Graviton3/4** user selects SVE kernels nobody here can run, and
- **nothing pins `libopenblas0`**, so a future Debian bump silently re-forms the pairing.

In both cases the failure would be invisible: nothing in the build, the scan, the smoke test
or `verify-models` reports a DER regression, which is upstream's own recorded experience.

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

## The change that shipped — option 1, bundling

Of the four options this document originally listed, **option 1 was chosen and built**: ship
diar-native's own `libopenblas` so the validated pairing travels with the binary. The other
three are recorded below with what they would have cost.

⚠️ It was chosen and kept **after** the aarch64 measurement came back clean, deliberately. It
is not the repair of an observed regression; it is the removal of an unpinned dependency the
project has no other control over, plus coverage for the SVE generations that cannot be
measured. Judge it on that basis — the alternative is carrying an unpinned, unmonitored
library under an accuracy-critical binary and hoping every future Debian bump is as harmless
as this one turned out to be.

### What changed

Both `backend/Dockerfile.prod` and `backend/Dockerfile.lite` gained a throwaway
`diar-native-staged` stage that:

1. **COPYs `libopenblasp-r0.3.26.so` out of `--from=diar-native-bin`** — the *same*
   per-`TARGETARCH` stage the binary already comes from (`FROM diar-native-bin-${TARGETARCH}`,
   restructured for exactly this in #680). The library's architecture therefore matches the
   binary's automatically and cannot drift: the aarch64 leg gets the aarch64 0.3.26 with no
   second decision to get wrong. The source path is
   `/usr/lib/*/openblas-pthread/libopenblasp-r0.3.26.so` — one glob for the multiarch triplet,
   and `openblas-pthread` because that is the variant Debian/Ubuntu's alternatives select and
   the one `ldd diar-server` resolves to in the upstream image (verified, not assumed).
2. **Runs `patchelf --force-rpath --set-rpath /opt/diar-native/lib`** on the binary, and
   fails the build if the RPATH does not read back.

The runtime stage then copies the **patched** binary (not upstream's) plus the library into
`/opt/diar-native/lib`, and adds the soname link `libopenblas.so.0 → libopenblasp-r0.3.26.so`
that `diar-server`'s `DT_NEEDED` asks for.

⚠️ **The shipped `diar-server`'s sha256 therefore no longer equals upstream's**, by design —
its dynamic section carries an added `DT_RPATH`. The `4745ca3f…` digest quoted earlier in
this document is upstream's, from the measurement pass.

### Why DT_RPATH and not an environment variable

The obvious cheaper fix is to add `/opt/diar-native/lib` to the `diar-native` compose
service's existing `LD_LIBRARY_PATH`. It was rejected on evidence:

- **`diar-server` runs from more than one place.** `app/transcription/native_provision.py`
  runs `diar-server provision-models` as a **subprocess of the backend**, with
  `env=_subprocess_env()` = `dict(os.environ)` — the backend's environment, whose image-wide
  `LD_LIBRARY_PATH` names only the torch CUDA wheel directories. That subprocess is what
  **exports the ONNX/PLDA model set the sidecar later serves**, so an env-only fix would have
  left *model creation* on the unvalidated library while the serving path looked fixed.
  `scripts/diar-native-smoke.sh`, `scripts/release/85-smoke.sh` and
  `tests/integration/test_export_toolchain_in_shipped_images.py` are three more call sites
  that get no compose environment.
- **Image-wide is not available.** One shared backend image runs the API, every Celery worker
  and the sidecar (deliberately — one homelab should not maintain two CUDA base images), so an
  image-wide `LD_LIBRARY_PATH` would also hand diar's ORT 1.24.2 provider libs to Python's
  onnxruntime-gpu 1.28.0, which ships the same filenames. `Dockerfile.prod` already documents
  that collision. DT_RPATH is a property of one ELF, so the blast radius is exactly the binary
  that needs it.
- **`--force-rpath` is load-bearing.** It emits **DT_RPATH**, searched *before*
  `LD_LIBRARY_PATH`. patchelf's default emits **DT_RUNPATH**, searched *after* it. Only the
  former makes the validated pairing impossible to displace silently with an env var — the
  same failure class as the bug. Verified adversarially: with
  `LD_LIBRARY_PATH=/usr/lib/x86_64-linux-gnu:/usr/lib/x86_64-linux-gnu/openblas-pthread`
  pointed straight at the system 0.3.29, `ldd` still resolves
  `libopenblas.so.0 => /opt/diar-native/lib/libopenblas.so.0`. (`LD_PRELOAD` remains the
  deliberate escape hatch for A/B experiments.)

### Why the apt `libopenblas0` stays

Measured in the built image rather than assumed:

- **numpy and scipy never used it.** They load their own vendored builds
  (`numpy.libs/libscipy_openblas64_-*.so`, `scipy.libs/libscipy_openblas-*.so`), confirmed by
  reading `/proc/self/maps` after a real matmul and a real `scipy.linalg` call.
- An **ELF sweep of all 1,863 distinct objects** under `/usr /opt /app /home/appuser/.local
  /bin /sbin` found `libopenblas.so.0` has exactly one `DT_NEEDED` consumer outside its own
  package: `diar-server`. (The sweep carries a positive control — it must find `diar-server`
  itself, or its zero-counts mean nothing. The first draft did **not**, because it read
  `PT_DYNAMIC`'s `p_vaddr` instead of `p_offset`, and its "0 consumers" was an artefact.)

So removing it *looks* free — and is not. The same package set provides the `libblas.so.3` /
`liblapack.so.3` alternatives that ffmpeg's `libsphinxbase.so.3.0.0` links, so dropping it
re-points a production audio dependency's BLAS onto the reference implementation, on an
architecture that cannot be tested from this host. It is also what pulls in `libgfortran5`,
which the **bundled** 0.3.26 itself needs as its only non-glibc `NEEDED` entry.

Bundling does not require that change, so it does not make it. Two copies now exist in the
image and **DT_RPATH decides** which one `diar-server` gets — not load order, not an env var.

### The other three options, and what they cost

2. **Pin OpenBLAS** to a pre-0.3.28 version via apt. Trixie carries only 0.3.29, so this means
   a cross-suite pin — real maintenance burden and ABI risk — and it would change the library
   for *every* process in the image, not just the one that needs it.
3. **`OPENBLAS_CORETYPE`** to steer kernel selection away from the forwarding path. The knob
   demonstrably works (see the Nehalem probe above), but which core type avoids the defect
   needs upstream input, and it is an env var — defeasible, and invisible in the artifact.
4. **Hold the arm64 leg** out of the published index until it is measured. **Overtaken by
   events** — the aarch64 DER run happened (0.0669, clean), so withholding the leg would now
   cost users a working artifact to guard against something no available hardware exhibits.
   It remains the only response that would cover the SVE generations outright, at the price
   of shipping no arm64 backend at all.

### How this is kept wired

- `backend/tests/unit/test_diar_native_openblas_bundled.py` — fast, always-on, in CI. Asserts
  the library COPY, the `--force-rpath` patch, that the **shipped** binary is the patched one,
  the soname link, single-version consistency across the file, that the bundled version
  predates 0.3.28, that the apt install survives, and that the private dir never lands on an
  image-wide `ENV LD_LIBRARY_PATH`. 12 of its 16 cases were watched failing against the
  pre-change tree; the other 4 are guards, and were watched failing under deliberate mutation.
- `backend/tests/integration/test_diar_native_openblas_runtime.py` — the runtime half. Reads
  `/proc/<pid>/maps` out of the running `diar-native` sidecar and out of a fresh `diar-server`
  started with no helpful environment, and separately asserts numpy/scipy still map their own
  vendored BLAS and still compute correctly.

A build-time COPY that a runtime process ignores looks exactly like success, which is why
both halves exist.
