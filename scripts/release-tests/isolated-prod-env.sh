#!/bin/bash
#
# scripts/release-tests/isolated-prod-env.sh — run the prod+PKI check and the prod lite demo
# check against a GENERATED, production-grade config, never the operator's .env.
#
# WHY: from a git worktree `.env` is a symlink to the main checkout's, and container values
# arrive only through `env_file: .env`, which no shell export overrides. The prod backend's
# startup_validation.py then (correctly) refuses the operator's dev values. Editing that file
# is not an option: KMS auto-encryption there would change the live dev MinIO. This script
# builds an ISOLATED install tree instead, so it also runs unattended on a CI runner.
#
# HOW (the rehearsal's pattern, not a new one): HEAD is cloned (`git clone --shared`) into a temp
# tree (mktemp -d -t ot-prodenv.XXXXXX, the redcheck naming) so the images carry its GIT_SHA;
# uncommitted tracked changes refuse with exit 3, `.env` is `.env.example` plus
# generated secrets and the production credentials from
# scripts/release-tests/lib/env-template.sh `et_service_credentials` (the same helper
# test-upgrade.sh embeds), nginx certs come from scripts/generate-ssl-cert.sh and the PKI
# certs from scripts/pki/setup-test-pki.sh. The stack starts as `--fresh NAME` (own
# otfresh-NAME project, own volumes; the live data is unreachable), is asserted with
# scripts/lib/stack_target.py via stack-target.sh, and is stopped with ./opentr.sh stop.
# MODEL_CACHE_DIR points at the host's shared cache, resolved by scripts/lib/model-cache-dir.sh
# (no download). The operator's .env is never read.
#
# Usage:
#   scripts/release-tests/isolated-prod-env.sh prepare <pki|demo> [--dir DIR]
#       Build the tree and its .env only; prints the tree path. Starts nothing.
#   scripts/release-tests/isolated-prod-env.sh run pki  [--name N] [--port-offset N]
#                                                      [--keep-stack] [-- <extra start flags>]
#       start prod --build --with-pki --fresh N, assert, run backend/tests/e2e/test_pki.py.
#   scripts/release-tests/isolated-prod-env.sh run demo [--name N] [--port-offset N]
#                                                      [--keep-stack] [--bundle DIR] [-- ...]
#       start prod --build --lite --demo --fresh N, assert, then (with --bundle)
#       `python -m app.scripts.demo_seed import` inside the backend container.
#   Extra flags after `--` go to `./opentr.sh start` (e.g. --with-mock-llm).
#
# Exit codes (standard): 0 pass · 1 gate failed · 2 misuse · 3 precondition unmet

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

err() { echo "error: $*" >&2; }

# shellcheck source=scripts/release-tests/lib/env-template.sh
source "$SCRIPT_DIR/lib/env-template.sh"
# shellcheck source=scripts/lib/model-cache-dir.sh
source "$REPO_ROOT/scripts/lib/model-cache-dir.sh"

[[ $# -ge 2 ]] || { sed -n 3,36p "$0"; exit 2; }
ACTION="$1"; MODE="$2"; shift 2
[[ "$ACTION" == prepare || "$ACTION" == run ]] || { err "unknown action: $ACTION"; exit 2; }
[[ "$MODE" == pki || "$MODE" == demo ]] || { err "mode must be pki or demo"; exit 2; }

NAME="prodenv-$MODE"; OFFSET=""; KEEP=false; BUNDLE=""; DIR=""; EXTRA=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        --name)        NAME="$2"; shift 2 ;;
        --port-offset) OFFSET="$2"; shift 2 ;;
        --keep-stack)  KEEP=true; shift ;;
        --bundle)      BUNDLE="$2"; shift 2 ;;
        --dir)         DIR="$2"; shift 2 ;;
        --)            shift; EXTRA=("$@"); break ;;
        *) err "unknown option: $1"; exit 2 ;;
    esac
done

# set_env FILE KEY VALUE: replace an active or commented `KEY=` line, else append.
set_env() {
    local file="$1" key="$2" value="$3"
    if grep -qE "^#? *${key}=" "$file"; then
        python3 - "$file" "$key" "$value" <<'PY'
import re, sys
path, key, value = sys.argv[1:4]
lines = open(path).read().splitlines()
done = False
for i, line in enumerate(lines):
    if not done and re.match(rf"^#? *{re.escape(key)}=", line):
        lines[i] = f"{key}={value}"; done = True
open(path, "w").write("\n".join(lines) + "\n")
PY
    else
        echo "${key}=${value}" >> "$file"
    fi
}

prepare_tree() {
    local tree="${DIR:-$(mktemp -d -t ot-prodenv.XXXXXX)}" cache pw
    mkdir -p "$tree"
    # A clone of HEAD, not a file copy: opentr.sh stamps GIT_SHA from `git rev-parse` in the
    # tree it builds, and stack_target's code=image check refuses 'unknown' (a copied tree has
    # no .git). --shared borrows the object store, so the clone is cheap. Tracked files only:
    # no .env, no gitignored operator state.
    git clone -q --shared --no-checkout "$REPO_ROOT" "$tree" \
        && git -C "$tree" checkout -q --detach "$SOURCE_SHA" \
        || { err "could not clone $SOURCE_SHA into $tree"; return 1; }
    local env="$tree/.env"
    cp "$tree/.env.example" "$env"
    # Replace every CHANGE_ME placeholder with a generated secret (values never printed).
    while IFS= read -r key; do
        set_env "$env" "$key" "$(et_rand_hex 24)"
    done < <(grep -E '^[A-Z_]+=CHANGE_ME' "$env" | cut -d= -f1)
    # Split on the FIRST '=' by expansion: `IFS='=' read k v` drops a trailing '=' from v,
    # which strips the base64 padding off MINIO_KMS_SECRET_KEY and MinIO refuses to start.
    local line
    while IFS= read -r line; do
        set_env "$env" "${line%%=*}" "${line#*=}"
    done < <(et_service_credentials)
    set_env "$env" OPENSEARCH_ADMIN_PASSWORD "Ot-$(et_rand_hex 12)-aA1!"
    set_env "$env" ENVIRONMENT production
    cache="$(resolve_model_cache_dir "$REPO_ROOT")"
    set_env "$env" MODEL_CACHE_DIR "$cache"
    if [[ "$MODE" == demo ]]; then
        pw="$(et_rand_hex 12)"
        set_env "$env" DEMO_MODE true
        set_env "$env" DEMO_USER_EMAIL demo@example.com
        set_env "$env" DEMO_USER_PASSWORD "$pw"
    fi
    (cd "$tree" && ./scripts/generate-ssl-cert.sh localhost >/dev/null 2>&1) \
        || { err "generate-ssl-cert.sh failed"; return 1; }
    if [[ "$MODE" == pki ]]; then
        (cd "$tree" && ./scripts/pki/setup-test-pki.sh >/dev/null 2>&1) \
            || { err "setup-test-pki.sh failed"; return 1; }
    fi
    echo "$tree"
}

# The build is attributed to a commit, so uncommitted tracked edits would be silently left out.
if [[ -n "$(git -C "$REPO_ROOT" status --porcelain --untracked-files=no)" ]]; then
    err "$REPO_ROOT has uncommitted tracked changes: commit them first (the build is attributed to HEAD)"
    exit 3
fi
SOURCE_SHA="$(git -C "$REPO_ROOT" rev-parse HEAD)" || { err "$REPO_ROOT has no commit"; exit 3; }
TREE="$(prepare_tree)" || exit 1
if [[ "$ACTION" == prepare ]]; then echo "$TREE"; exit 0; fi

cd "$TREE" || exit 3
# shellcheck source=scripts/lib/stack-target.sh
source "$TREE/scripts/lib/stack-target.sh"

START=(./opentr.sh start prod --build --fresh "$NAME")
[[ -n "$OFFSET" ]] && START+=(--port-offset "$OFFSET")
if [[ "$MODE" == pki ]]; then START+=(--with-pki); else START+=(--lite --demo); fi
START+=("${EXTRA[@]}")

STARTED=false
teardown() {
    local rc_in=$?
    trap - EXIT INT TERM
    if [[ "$STARTED" == true && "$KEEP" != true ]]; then
        ./opentr.sh stop --fresh "$NAME" >/dev/null 2>&1 || true
    fi
    exit "$rc_in"
}
trap teardown EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

STARTED=true
"${START[@]}" || { err "start failed: ${START[*]}"; exit 1; }
st_target --fresh "$NAME" || { err "fresh target $NAME did not resolve"; exit 3; }
st_require code=image healthy isolated || { err "the $NAME stack is not this tree's healthy build"; exit 3; }

if [[ "$MODE" == pki ]]; then
    PORT=$(( ${PKI_HTTPS_PORT:-5182} + ${OFFSET:-0} ))
    deadline=$(( $(date +%s) + 600 ))
    until curl -sk --max-time 5 "https://localhost:${PORT}/" >/dev/null 2>&1; do
        [[ $(date +%s) -lt $deadline ]] || { err "mTLS listener :${PORT} never answered"; exit 1; }
        sleep 5
    done
    PKI_HTTPS_PORT="$PORT" RUN_PKI_E2E=true "$REPO_ROOT/backend/venv/bin/python" -m pytest \
        "$TREE/backend/tests/e2e/test_pki.py" -v || exit 1
elif [[ -n "$BUNDLE" ]]; then
    ctr="$(st_container backend)" || exit 3
    docker cp "$BUNDLE" "$ctr:/tmp/demo-bundle" || exit 1
    docker exec "$ctr" python -m app.scripts.demo_seed import --bundle /tmp/demo-bundle || exit 1
fi
exit 0
