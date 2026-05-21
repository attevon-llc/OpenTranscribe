# CPU-Only Mode — End-to-End Testing Plan

This document describes the manual test procedure for verifying that
OpenTranscribe's CPU-only deployment path works correctly end-to-end. Run
through it after the install scripts, backend config, and frontend banner
work has been merged.

The CPU-only path covers two scenarios:

1. **Forced CPU** — user passed `--cpu` to `setup-opentranscribe.sh` (or set
   `OPENTRANSCRIBE_FORCE_CPU=1`). Persists as `FORCE_CPU_MODE=true` in
   `.env`.
2. **Auto-fallback CPU** — host has no working NVIDIA setup (no
   `nvidia-smi`, or no Container Toolkit, or non-Apple-Silicon Mac).

Both should produce a working stack with CPU-friendly defaults
(`WHISPER_MODEL=base`, `ENABLE_DIARIZATION=false`).

---

## Prerequisites

- Docker + Docker Compose installed.
- Either: a host with no NVIDIA GPU, **or** a GPU host where you'll use
  `--cpu` to force CPU.
- `.env` not already present in the install target (or be ready to back it
  up — the installer writes a fresh one).

---

## Test 1 — Install path with `--cpu` flag

**Goal:** Confirm `--cpu` is honored, GPU detection is skipped, and the
generated `.env` contains the safe CPU defaults.

```bash
# From a clean checkout:
OPENTRANSCRIBE_UNATTENDED=1 ./setup-opentranscribe.sh --cpu
```

**Expected console output (must include):**
- `ℹ️  CPU-only install mode selected` (from PR #194)
- `ℹ️  CPU-only mode requested (--cpu / OPENTRANSCRIBE_FORCE_CPU set)`
- `Skipping NVIDIA GPU detection.`
- A new CPU-mode advisory block in the summary listing:
  - Whisper model defaulted to `base`.
  - Diarization disabled.
  - How to re-enable.

**Verify `.env` contains:**

```bash
grep -E '^(FORCE_CPU_MODE|WHISPER_MODEL|ENABLE_DIARIZATION|DETECTED_DEVICE|USE_NVIDIA_RUNTIME)=' .env
```

Expected:
```
FORCE_CPU_MODE=true
WHISPER_MODEL=base
ENABLE_DIARIZATION=false
DETECTED_DEVICE=cpu
USE_NVIDIA_RUNTIME=false
```

**Pass criteria:** all five lines present with expected values.

---

## Test 2 — Install path with `OPENTRANSCRIBE_FORCE_CPU` env var

```bash
OPENTRANSCRIBE_UNATTENDED=1 OPENTRANSCRIBE_FORCE_CPU=1 \
  ./setup-opentranscribe.sh
```

**Pass criteria:** identical `.env` output to Test 1. Confirms the env-var
path is equivalent to the flag.

---

## Test 3 — Auto-fallback on a host with no GPU

Run the installer **without** `--cpu` on a host where `nvidia-smi` is
absent (or use a Linux container without the toolkit):

```bash
OPENTRANSCRIBE_UNATTENDED=1 ./setup-opentranscribe.sh
```

**Expected console output:**
- `ℹ️  Using CPU processing (no GPU acceleration detected)`
- CPU-mode advisory block (same as Test 1, minus the "forced via --cpu"
  caveat).

**Verify `.env`:**

```bash
grep -E '^(FORCE_CPU_MODE|WHISPER_MODEL|ENABLE_DIARIZATION|DETECTED_DEVICE)=' .env
```

Expected:
```
FORCE_CPU_MODE=false
WHISPER_MODEL=base
ENABLE_DIARIZATION=false
DETECTED_DEVICE=cpu
```

**Note:** `FORCE_CPU_MODE=false` here is correct — the user did not opt
out, the system simply has no GPU. The CPU-friendly defaults still kick in
because `DETECTED_DEVICE=cpu`.

**Pass criteria:** values match.

---

## Test 4 — Compose layering skips GPU overlay

After Test 1 or Test 2 completes:

```bash
./opentranscribe.sh start
```

**Expected console output:**
- `🧮 CPU-only mode (FORCE_CPU_MODE=true in .env) — skipping GPU overlay
  despite nvidia runtime being available` (only if Docker reports nvidia
  runtime; otherwise silent — no error).
- No `Blackwell GPU overlay enabled` / `NVIDIA GPU overlay enabled`
  message.

**Verify the actual compose file list:**

```bash
docker compose -f docker-compose.yml \
  $(./opentranscribe.sh _compose_files 2>/dev/null || \
    bash -c 'source ./opentranscribe.sh; get_compose_files') \
  config 2>&1 | grep -E 'nvidia|gpu' | head -5
```

(If the `_compose_files` helper isn't exposed, just inspect
`docker ps --format '{{.Names}}'` after start — no `celery-worker-gpu-*`
container should be present.)

**Pass criteria:** no GPU overlay applied.

---

## Test 5 — Backend startup warning

**Goal:** Confirm the backend logs a warning when it starts in CPU mode
with a heavyweight model or diarization on.

Manually edit `.env` to simulate a misconfigured CPU host:

```bash
sed -i 's/^WHISPER_MODEL=.*/WHISPER_MODEL=large-v3-turbo/' .env
sed -i 's/^ENABLE_DIARIZATION=.*/ENABLE_DIARIZATION=true/' .env
./opentranscribe.sh restart
```

```bash
docker logs opentranscribe-celery-worker 2>&1 | grep -i 'cpu mode\|cpu-only\|recommend' | head
```

**Expected:** A single `WARNING` line per worker process recommending
`WHISPER_MODEL=base` and `ENABLE_DIARIZATION=false`.

**Pass criteria:** warning fires once and is human-readable.

Restore safe defaults afterwards:

```bash
sed -i 's/^WHISPER_MODEL=.*/WHISPER_MODEL=base/' .env
sed -i 's/^ENABLE_DIARIZATION=.*/ENABLE_DIARIZATION=false/' .env
./opentranscribe.sh restart
```

The warning should no longer appear.

---

## Test 6 — Frontend CPU-mode banner

1. Ensure the stack is running with `FORCE_CPU_MODE=true` (Test 1
   state).
2. Open the app, log in, open **Settings → System Statistics**.

**Expected:**
- Amber/orange advisory banner at top of the System Statistics panel.
- Title: "Running in CPU-only mode".
- Subtitle: "(set via `--cpu` install flag)".
- Bullets reflecting `WHISPER_MODEL=base` and "Diarization: OFF
  (recommended for CPU)".

3. Edit `.env` → `FORCE_CPU_MODE=false` (simulate auto-fallback). Restart.
   Reload the Settings panel.

**Expected:** Banner subtitle changes to "(no GPU acceleration available —
automatic fallback)".

4. Edit `.env` → `WHISPER_MODEL=large-v3-turbo`,
   `ENABLE_DIARIZATION=true`. Restart. Reload.

**Expected:** Bullets update to show the new model name and a warning
that diarization is on but slow on CPU.

5. (GPU host only) Edit `.env` → `FORCE_CPU_MODE=false`, ensure GPU is
   actually available. Restart.

**Expected:** Banner does **not** render.

**Pass criteria:** all four states render correctly and the banner
disappears on a working GPU host.

---

## Test 7 — End-to-end transcription on CPU

**Goal:** Verify CPU-mode actually transcribes a file successfully (smoke
test, not a performance test).

1. With CPU-mode `.env` (Test 1 state), upload a short audio file (~30
   seconds).
2. Watch the file detail page or the worker logs.

**Expected:**
- File transitions: `pending → processing → completed`.
- Transcript is produced (English text reasonably accurate).
- No "speakers" panel populated (diarization is off — this is correct).
- Worker logs show `device=cpu`, `compute_type=int8`, `model=base`.

**Pass criteria:** transcription completes without error in a reasonable
time (a 30-second clip should finish in <2 minutes on a modern CPU).

---

## Test 8 — GPU regression check

**Goal:** Confirm GPU users see no behavioral change.

On a host with a working NVIDIA GPU:

```bash
# Without --cpu
OPENTRANSCRIBE_UNATTENDED=1 ./setup-opentranscribe.sh
```

**Verify `.env`:**

```bash
grep -E '^(FORCE_CPU_MODE|DETECTED_DEVICE|USE_NVIDIA_RUNTIME|WHISPER_MODEL|ENABLE_DIARIZATION)=' .env
```

Expected:
```
FORCE_CPU_MODE=false
DETECTED_DEVICE=cuda
USE_NVIDIA_RUNTIME=true
WHISPER_MODEL=large-v3-turbo
ENABLE_DIARIZATION=true
```

(`WHISPER_MODEL` and `ENABLE_DIARIZATION` should be absent or set to the
GPU defaults — the CPU-defaults branch must NOT have written them.)

```bash
./opentranscribe.sh start
docker ps --format '{{.Names}}' | grep -i gpu
```

**Pass criteria:**
- GPU compose overlay is applied.
- Transcription runs on GPU (worker logs show `device=cuda`).
- No CPU advisory banner in the UI.
- No backend "CPU mode" warning in logs.

---

## Test 9 — Switching back to GPU after CPU install

**Goal:** Confirm the documented escape hatch works.

Starting from Test 1's CPU-only `.env`:

```bash
sed -i 's/^FORCE_CPU_MODE=true/FORCE_CPU_MODE=false/' .env
./opentranscribe.sh restart
```

**Pass criteria** (on a GPU host):
- GPU overlay is applied.
- Worker uses GPU.
- Banner disappears (or shows differently — depends on whether you also
  flipped `WHISPER_MODEL` back).

---

## Pass/Fail Summary

| # | Test | Pass criteria |
|---|------|---------------|
| 1 | `--cpu` install | `.env` has expected 5 lines |
| 2 | `OPENTRANSCRIBE_FORCE_CPU` env | matches Test 1 |
| 3 | Auto-fallback (no GPU) | CPU defaults written, `FORCE_CPU_MODE=false` |
| 4 | Compose skips GPU overlay | no `celery-worker-gpu-*` container |
| 5 | Backend warning | single warning line on misconfigured CPU host |
| 6 | Frontend banner | renders correctly across forced / auto / restored states |
| 7 | End-to-end CPU transcribe | short file completes successfully |
| 8 | GPU regression | GPU host unchanged |
| 9 | CPU → GPU switch | `FORCE_CPU_MODE=false` re-enables GPU overlay |

If all 9 pass, the CPU-only deployment path is good to ship.
