#!/bin/bash
# GPU device selection for opentr.sh: which host card each of the stack's AI workers
# reserves, the `--gpu-device` override, and the report of a caller's exported device id
# that .env or a flag overrode. Sourced by opentr.sh before .env, so
# snapshot_gpu_device_exports can still see what the caller exported.

# GPU device-id variables that docker compose INTERPOLATES to pick the physical
# card for one of OpenTranscribe's own AI workers. One list, so `--gpu-device`,
# the pre-export snapshot below, and the test that guards them cannot disagree
# about the membership.
GPU_DEVICE_VARS=(
  GPU_DEVICE_ID             # default GPU worker          (docker-compose.gpu.yml / .blackwell.yml)
  REDACTION_GPU_DEVICE_ID   # redaction worker            (docker-compose.gpu.yml)
  GPU_SCALE_DEVICE_ID       # --gpu-scale workers         (docker-compose.gpu-scale.yml)
  GPU_TRANSCRIBE_DEVICE_ID  # --with-gpu-split transcribe (docker-compose.gpu-split.yml)
  GPU_DIARIZE_DEVICE_ID     # --with-gpu-split diarize    (docker-compose.gpu-split.yml)
  DIAR_NATIVE_GPU           # diar-native sidecar         (docker-compose.diar-native.yml)
)

# What the CALLER exported for each of them, captured before `source ./.env`
# overwrites it. `GPU_SCALE_DEVICE_ID=2 ./opentr.sh start dev --gpu-scale` reads
# like it moves the scaled workers to GPU 2, but .env wins and they start on
# whatever .env says, with nothing printed. warn_ignored_gpu_device_exports names
# each export that lost and the flag that does what was meant. Plain variables,
# not an associative array: opentr.sh must also run under macOS's bash 3.2.
snapshot_gpu_device_exports() {
  local var
  for var in "${GPU_DEVICE_VARS[@]}"; do
    if [ -n "${!var+x}" ]; then
      printf -v "OT_PRE_EXPORTED_${var}" '%s' "${!var}"
    fi
  done
}

# `--gpu-device N` — retarget every GPU this stack's workers reserve, applied
# AFTER .env has been sourced.
#
# WHY IT EXISTS: opentr.sh does `set -a; source ./.env` near the top, so
# `GPU_DEVICE_ID=2 ./opentr.sh start dev` is silently overwritten by whatever
# .env says — a pre-export cannot win. The only remaining way to move a worker
# onto another card was to EDIT .env, which is shared with the live stack (and in
# a git worktree is often a copy of, or a symlink to, the same file). That has
# already happened here: a worktree .env edit moved the LIVE stack's GPU.
#
# WHY IT MOVES ALL FIVE: a flag that repoints one worker and leaves four behind
# is worse than no flag — it looks like it worked, and then two stacks fight over
# one card. `--gpu-device N` therefore means "this whole stack runs its AI work on
# host GPU N", and sets every variable in GPU_DEVICE_VARS.
#
# WHAT IT DELIBERATELY DOES NOT MOVE:
#   * LLM_TEST_GPU_DEVICE_ID (--with-llm-test) hosts a multi-GB LLM and is pinned
#     to a DIFFERENT card on purpose so it never contends with transcription.
#     Folding it in would co-locate them — the exact OOM that separation avoids.
#     Move it explicitly with `LLM_TEST_GPU_DEVICE_ID=N ./opentr.sh ...` (it is
#     absent from .env.example, so a pre-export survives unless your .env sets it).
#   * The container-side copy of GPU_DEVICE_ID is read INSIDE the container from
#     `env_file: .env` rather than interpolated by compose, so no shell export can
#     reach it. Warned about below.
apply_gpu_device_override() {
  local requested="$1"
  # Still .env's value here: nothing assigns GPU_DEVICE_ID between `source ./.env` and this
  # call. The containers read it from `env_file: .env`, not from this shell (see below).
  local dotenv_device_id="${GPU_DEVICE_ID:-}"
  local var

  if ! [[ "$requested" =~ ^[0-9]+$ ]]; then
    echo "❌ --gpu-device must be a non-negative integer GPU index (got '$requested')"
    exit 1
  fi

  # Catch the typo now rather than as an opaque "could not select device driver"
  # from the daemon several minutes into a build. Skipped when nvidia-smi is
  # absent (macOS, CPU-only host, CI) — the flag is a no-op there anyway.
  if command -v nvidia-smi &> /dev/null; then
    local gpu_count
    gpu_count=$(nvidia-smi -L 2>/dev/null | grep -c '^GPU ' || true)
    if [ -n "$gpu_count" ] && [ "$gpu_count" -gt 0 ] && [ "$requested" -ge "$gpu_count" ]; then
      echo "❌ --gpu-device $requested: this host has $gpu_count GPU(s) (valid indices 0-$((gpu_count - 1)))"
      nvidia-smi -L 2>/dev/null | sed 's/^/   /'
      exit 1
    fi
  fi

  for var in "${GPU_DEVICE_VARS[@]}"; do
    export "$var=$requested"
  done

  echo "🎯 --gpu-device $requested: pinning this stack's AI workers to host GPU $requested (overrides .env)"
  echo "   Set: ${GPU_DEVICE_VARS[*]} = $requested"
  echo "   NOT set: LLM_TEST_GPU_DEVICE_ID (--with-llm-test keeps its own card on purpose)"

  # The container-side copy of GPU_DEVICE_ID comes from `env_file: .env`, which no
  # shell export can override. It is only read for host GPU-stats display
  # (nvidia-smi -i), never for placement — placement is the reservation above —
  # but the admin GPU panel will keep naming the .env card until .env is edited.
  if [ -n "$dotenv_device_id" ] && [ "$dotenv_device_id" != "$requested" ]; then
    echo "   ℹ️  In-container GPU_DEVICE_ID stays ${dotenv_device_id} (from env_file: .env) — that value"
    echo "      only labels the admin GPU-stats panel; the reserved card is $requested."
  fi

}

# A GPU device id the caller exported but this stack will NOT use: .env (sourced
# after the caller's environment) or a --gpu-device / --diar-native-gpu flag set
# a different card. Call after every override has been applied, so the comparison
# is against the value compose will actually interpolate.
warn_ignored_gpu_device_exports() {
  local var snap pre effective
  for var in "${GPU_DEVICE_VARS[@]}"; do
    snap="OT_PRE_EXPORTED_${var}"
    [ -n "${!snap+x}" ] || continue
    pre="${!snap}"
    effective="${!var:-}"
    [ "$pre" = "$effective" ] && continue
    echo "   ⚠️  ${var}=${pre} was exported, but this stack will use ${var}=${effective:-<unset>}:"
    echo "      .env is sourced after your environment and wins, so a pre-export cannot move a card."
    if [ "$var" = "DIAR_NATIVE_GPU" ]; then
      echo "      Use --diar-native-gpu ${pre} (the sidecar only) or --gpu-device ${pre} (every AI worker)."
    else
      echo "      Use --gpu-device ${pre} (moves every AI worker), or edit .env if only ${var} should differ."
    fi
  done
}

# Flag combinations that make --gpu-device mean less than it looks like it means.
# Separate from apply_gpu_device_override so the override itself stays a pure
# "set these vars" step that the other flags' parse order cannot affect.
warn_gpu_device_override_conflicts() {
  local requested="$1"

  if [ -n "${CPU_FLAG:-}" ] || [ -n "${LITE_FLAG:-}" ]; then
    echo "   ⚠️  --cpu/--lite loads no GPU overlay, so --gpu-device $requested reserves nothing."
  fi

  if [ -n "${GPU_SPLIT_FLAG:-}" ]; then
    echo "   ⚠️  --with-gpu-split exists to put transcribe and diarize on DIFFERENT cards;"
    echo "      --gpu-device $requested collapses both onto GPU $requested. Drop one of the two flags,"
    echo "      or set GPU_TRANSCRIBE_DEVICE_ID / GPU_DIARIZE_DEVICE_ID in .env instead."
  fi
}

# ` VAR=N` for every GPU_DEVICE_VARS entry, as compose will interpolate it (an unset one
# falls back to GPU_DEVICE_ID, then 0, as the overlays do). For `--dry-run`, which prints it
# always so a dry run answers "which card does this stack take?" without reading .env;
# iterating the list means a device var added to it shows up there without a second edit.
gpu_device_reservations() {
  local var out=""
  for var in "${GPU_DEVICE_VARS[@]}"; do
    out="${out} ${var}=${!var:-${GPU_DEVICE_ID:-0}}"
  done
  printf '%s' "$out"
}
