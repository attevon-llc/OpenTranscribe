#!/usr/bin/env bash
# Sourced helper: resolve and vet the model cache directory for scripts that run a compose
# file bind-mounting ${MODEL_CACHE_DIR:-./models}.
#
# WHY: compose resolves a relative bind source against the project directory. From a git
# worktree that is the WORKTREE, which has no models/, so docker silently creates empty
# root-owned models/huggingface, models/torch, ... there and every GPU test then dies with
# PermissionError on the HF cache (observed 2026-11-05). The real cache lives in the main
# checkout, found from any worktree via `git rev-parse --git-common-dir`.
#
#   source "$SCRIPT_DIR/lib/model-cache-dir.sh"
#   MODEL_CACHE_DIR="$(resolve_model_cache_dir "$PROJECT_ROOT")"
#   require_model_cache_dir "$MODEL_CACHE_DIR" || exit 3
#   export MODEL_CACHE_DIR

_MCD_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# The checkout that owns the real (gitignored) models/ and .env: the parent of the shared
# git dir. Falls back to the given root outside a git checkout (an unpacked release).
model_cache_main_root() {
    local root="$1" common
    if common="$(git -C "$root" rev-parse --path-format=absolute --git-common-dir 2>/dev/null)" \
        && [ -n "$common" ]; then
        dirname "$common"
    else
        printf '%s\n' "$root"
    fi
}

# resolve_model_cache_dir <project-root>
#   Absolute MODEL_CACHE_DIR. Precedence: the environment, then the main checkout's .env,
#   then ./models. A relative value is resolved against the MAIN checkout, never the cwd or
#   the worktree. No existence requirement; see require_model_cache_dir.
resolve_model_cache_dir() {
    local main raw
    main="$(model_cache_main_root "$1")"
    raw="${MODEL_CACHE_DIR:-}"
    if [ -z "$raw" ] && [ -f "$main/.env" ] && command -v python3 >/dev/null 2>&1; then
        raw="$(python3 "$_MCD_LIB_DIR/env_reader.py" "$main/.env" MODEL_CACHE_DIR 2>/dev/null || true)"
    fi
    raw="${raw:-./models}"
    case "$raw" in
        \~|\~/*) raw="${HOME}${raw#\~}" ;;
        /*) ;;
        *) raw="$main/$raw" ;;
    esac
    realpath -m -- "$raw"
}

# require_model_cache_dir <abs-dir>
#   Returns 3 (message on stderr) when the directory is missing, holds no model cache, or
#   is owned by someone other than the invoking user (the signature of a docker-created
#   root-owned stub). Returns 0 otherwise.
require_model_cache_dir() {
    local dir="$1" me sub populated=0 owner
    me="$(id -u)"
    if [ ! -d "$dir" ]; then
        echo "FAIL: model cache directory does not exist: $dir" >&2
        echo "      Set MODEL_CACHE_DIR to the populated cache (main checkout: ./models)." >&2
        return 3
    fi
    for sub in "" huggingface torch nltk_data sentence-transformers; do
        [ -e "$dir/$sub" ] || continue
        owner="$(stat -c %u -- "$dir/$sub")"
        if [ "$owner" != "$me" ]; then
            echo "FAIL: ${dir}${sub:+/$sub} is owned by uid $owner, not you (uid $me)." >&2
            echo "      Docker creates such root-owned stubs when a bind source did not exist" >&2
            echo "      (e.g. a relative ./models resolved inside a git worktree)." >&2
            echo "      Fix the ownership or point MODEL_CACHE_DIR at the real cache." >&2
            return 3
        fi
    done
    for sub in huggingface torch; do
        if [ -d "$dir/$sub" ] && [ -n "$(ls -A -- "$dir/$sub" 2>/dev/null)" ]; then
            populated=1
        fi
    done
    if [ "$populated" -eq 0 ]; then
        echo "FAIL: $dir holds no model cache (huggingface/ and torch/ are absent or empty)." >&2
        echo "      Running against it would measure nothing. Populate it with" >&2
        echo "      ./scripts/download-models.sh, or point MODEL_CACHE_DIR at the real cache." >&2
        return 3
    fi
    return 0
}
