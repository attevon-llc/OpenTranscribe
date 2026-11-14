#!/usr/bin/env bash
# Stale .env VALUE check, sourced by opentranscribe.sh (shipped through release-manifest.txt).
# Needs read_env_value (scripts/common.sh, or opentranscribe.sh's standalone fallback) at call
# time. Only reads .env.
#
# issue #709: `update-full`'s "new .env keys" report (opentranscribe.sh update_report_env_drift) can only
# see a key that is ABSENT. Two real cases on this branch are a key that is PRESENT but whose
# VALUE has rotted — correct when written, silently wrong now:
#
#   1. ENGINE_SHARED_VOLUME_PATH=/tmp/transcription — issue #661 E2 removed the
#      transcription-temp volume this path named. `os.makedirs` recreates it INSIDE the
#      writer's own container, so the write "succeeds" and the reader finds nothing; every
#      job silently drops to the MinIO round-trip fallback. `resolve_engine_shared_volume_path()`
#      (backend/app/core/scratch_paths.py) already self-heals this at runtime by preferring the
#      coded default when the configured path doesn't exist — that saves the install. This
#      warning is complementary, not a duplicate: it tells the OPERATOR their .env is drifting,
#      which the silent runtime fallback deliberately does not.
#   2. GPU_SCALE_WORKERS explicitly set higher than DIAR_NATIVE_MAX_INFLIGHT — defeats the
#      docker-compose.gpu-scale.yml derivation (`${GPU_SCALE_WORKERS:-${DIAR_NATIVE_MAX_INFLIGHT:-2}}`)
#      added specifically to prevent that contention (test_env_example_gpu_scale_derivation.py).
#
# Consulted by BOTH update-full's post-download report and preflight_upgrade_env, so a release
# that invalidates a value says so BEFORE teardown, while the operator can still act — same
# placement issue #670 uses for the native-diarizer refusal.
#
# ⚠️ WARN and name the fix. NEVER rewrite .env — this repo never edits an operator's .env
# without confirmation (see the "new .env keys" report in opentranscribe.sh), and a silent correction here is
# exactly how the original bug (case 1) hid in the first place.
#
# To add a third case: append ONE row to STALE_ENV_CHECKS ("KEY|remedy text") and one `KEY)`
# arm to the case statement in check_stale_env_values() below — no new function, no new call
# site, no change to either caller.
STALE_ENV_CHECKS=(
    "ENGINE_SHARED_VOLUME_PATH|remove this line from .env (or set it to /scratch/opentranscribe/engine) — the path it names was removed by issue #661 E2's pipeline_scratch consolidation"
    "GPU_SCALE_WORKERS|comment this out in .env so docker-compose.gpu-scale.yml derives it from DIAR_NATIVE_MAX_INFLIGHT instead — an explicit value here can oversubscribe the diar-native sidecar's admission gate"
    "BACKEND_BIND_HOST|set it to 127.0.0.1 (or remove the line) unless another machine must call the API port directly — 0.0.0.0 was the old .env.example default, and the browser never needs it because the frontend proxies /api (issue #1332)"
    "BACKEND_LITE_IMAGE|comment this out in .env so docker-compose.lite.yml derives the image from OT_IMAGE_TAG instead — the old .env.example shipped this line SET to :latest, which overrides release pinning and makes 'update --version' / '--rollback' silently no-ops for every lite service (issue #895)"
)

# Prints one "  • KEY=value — remedy" line per stale key found in $1 (default .env) to
# stdout; prints nothing when none are stale. Never modifies the file.
check_stale_env_values() {
    local env_file="${1:-.env}"
    [ -f "$env_file" ] || return 0

    local row key remedy val
    for row in "${STALE_ENV_CHECKS[@]}"; do
        key="${row%%|*}"
        remedy="${row#*|}"
        val=$(read_env_value "$key" "$env_file")
        [ -z "$val" ] && continue

        case "$key" in
            ENGINE_SHARED_VOLUME_PATH)
                [ "$val" = "/tmp/transcription" ] || continue
                ;;
            GPU_SCALE_WORKERS)
                local max_inflight
                max_inflight=$(read_env_value DIAR_NATIVE_MAX_INFLIGHT "$env_file")
                max_inflight="${max_inflight:-2}"
                case "$val" in
                    ''|*[!0-9]*) continue ;;
                esac
                case "$max_inflight" in
                    ''|*[!0-9]*) continue ;;
                esac
                [ "$val" -gt "$max_inflight" ] || continue
                ;;
            BACKEND_LITE_IMAGE)
                # Deliberately narrow: flag ONLY the exact value the old .env.example
                # shipped, never any value whose tag merely differs from OT_IMAGE_TAG. A
                # broader "pin doesn't track OT_IMAGE_TAG" rule would also flag a private
                # registry mirror, a capability-leg pin (e.g. :v0.5.0-cpu-arm64, which is
                # supposed to differ from the plain release tag), and the release
                # rehearsal's own deliberate local pin — all legitimate, none of them the
                # #895 regression. This checks the one string that IS the regression: the
                # exact stale default, byte for byte.
                [ "$val" = "davidamacey/opentranscribe-backend-lite:latest" ] || continue
                ;;
            BACKEND_BIND_HOST)
                [ "$val" = "0.0.0.0" ] || continue  # the old template's exact value only
                ;;
            *)
                continue
                ;;
        esac

        echo "  • ${key}=${val} — ${remedy}"
    done
}
