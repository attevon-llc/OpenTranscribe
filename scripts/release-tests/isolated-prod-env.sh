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
# (no download), and the isolated diar-native models dir is seeded from that cache's export.
# The operator's .env is never read.
#
# Usage:
#   scripts/release-tests/isolated-prod-env.sh prepare <pki|demo> [--dir DIR] [--name N] [--port-offset N]
#       Build the tree and its .env only; prints the tree path. Starts nothing.
#   scripts/release-tests/isolated-prod-env.sh run pki  [--name N] [--port-offset N]
#                                                      [--keep-stack] [-- <extra start flags>]
#       start prod --build --with-pki --fresh N, assert, run backend/tests/e2e/test_pki.py.
#   scripts/release-tests/isolated-prod-env.sh run demo [--name N] [--port-offset N]
#                                                      [--keep-stack] [--bundle DIR] [-- ...]
#       start prod --build --lite --demo --fresh N behind nginx (the demo host's chain: the
#       tree's .env carries DEPLOYMENT_MODE=lite, NGINX_SERVER_NAME and the offset ports), assert,
#       then (with --bundle) `python -m app.scripts.demo_seed import` in the backend container.
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
# shellcheck source=scripts/release/fresh-stack-lib.sh
source "$REPO_ROOT/scripts/release/fresh-stack-lib.sh"

[[ $# -ge 2 ]] || { sed -n 3,38p "$0"; exit 2; }
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

# The demo tree is the install demo-reset.sh and demo-upgrade.sh act on, so its .env says what the
# demo host's says (docs-site/docs/operations/public-demo.md, "Set up the host"): lite, demo mode,
# and nginx with a certificate, which is the reset's fence and loads docker-compose.demo-cloudflare.yml.
# Host ports and the diar-native dir live in .env too, where an install keeps them, so a plain
# `docker compose` in this tree reaches the isolated stack, never the default ports or the shared
# model cache. opentr.sh offsets a port .env already sets from that value, so `run demo` passes it
# no --port-offset.
demo_install_env() {
    local env="$1" kv
    set_env "$env" DEPLOYMENT_MODE lite
    set_env "$env" DEMO_MODE true
    set_env "$env" DEMO_USER_EMAIL demo@example.com
    set_env "$env" DEMO_USER_PASSWORD "$(et_rand_hex 12)"
    set_env "$env" NGINX_SERVER_NAME localhost
    set_env "$env" DIAR_NATIVE_MODELS_DIR "./.fresh/${SLUG}/diar-native-models"
    [[ -n "$OFFSET" ]] || return 0
    # shellcheck source=scripts/lib/fresh-prod.sh
    source "$REPO_ROOT/scripts/lib/fresh-prod.sh" # FRESH_NGINX_PORT_VARS
    while IFS= read -r kv; do
        set_env "$env" "${kv%%=*}" "${kv#*=}"
    done < <(
        fresh_stack_port_exports "$OFFSET" | grep -v '^OT_RESOLVED_'
        for kv in "${FRESH_NGINX_PORT_VARS[@]}"; do echo "${kv%%=*}=$((${kv#*=} + OFFSET))"; done
    )
}

prepare_tree() {
    local tree="${DIR:-$(mktemp -d -t ot-prodenv.XXXXXX)}" cache
    mkdir -p "$tree"
    # A clone of HEAD, not a file copy: opentr.sh stamps GIT_SHA from `git rev-parse` in the
    # tree it builds, and stack_target's code=image check refuses 'unknown' (a copied tree has
    # no .git). --shared borrows the object store, so the clone is cheap. Tracked files only:
    # no .env, no gitignored operator state.
    git clone -q --shared --no-checkout "$REPO_ROOT" "$tree" \
        && git -C "$tree" checkout -q --detach "$SOURCE_SHA" \
        || { err "could not clone $SOURCE_SHA into $tree"; return 1; }
    # frontend/static/fonts is gitignored, so the clone has none and stack_target's
    # asset:frontend-fonts (and the PKI e2e preflight) refuse. Same provisioning as
    # scripts/e2e/styling-ab.sh: copy the checkout's fonts; fall back to the project's own
    # `npm run setup-fonts`. Never operator state: fonts are public assets.
    if compgen -G "$REPO_ROOT/frontend/static/fonts/*.woff2" >/dev/null; then
        mkdir -p "$tree/frontend/static" \
            && cp -a "$REPO_ROOT/frontend/static/fonts" "$tree/frontend/static/fonts" \
            || { err "could not copy frontend fonts"; return 1; }
    else
        (cd "$tree/frontend" && npm ci --silent >/dev/null 2>&1 && npm run --silent setup-fonts >/dev/null 2>&1) \
            || { err "no fonts in $REPO_ROOT and npm run setup-fonts failed"; return 1; }
    fi
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
    # The tag `start prod --build --fresh NAME` builds under (opentr.sh fresh_prod_dispatch), pinned
    # here as well so every compose run in this tree (demo-reset.sh, opentranscribe.sh) resolves the
    # images built from it, never a `:latest` that Docker Hub may hold.
    set_env "$env" OT_IMAGE_TAG "otfresh-${SLUG}"
    cache="$(resolve_model_cache_dir "$REPO_ROOT")"
    set_env "$env" MODEL_CACHE_DIR "$cache"
    [[ "$MODE" != demo ]] || demo_install_env "$env"
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

# Every run generates fresh secrets, but a project's named volumes keep the old ones: Postgres then
# rejects the new password and migrate exits 4, minutes into the build. Refuse before any build.
# Project-scoped by label (never a name grep) and same slug rule as opentr.sh fresh_sanitize_name.
SLUG="$(echo "$NAME" | tr '[:upper:]' '[:lower:]' | tr -cs 'a-z0-9' '-' | sed 's/^-//;s/-$//')"
SLUG="${SLUG:-default}"
if [[ "$ACTION" == run ]]; then
    OLD_VOLS="$(docker volume ls -q --filter "label=com.docker.compose.project=otfresh-${SLUG}" 2>/dev/null)" || OLD_VOLS=""
    if [[ -n "$OLD_VOLS" ]]; then
        err "project otfresh-${SLUG} already has volumes, holding secrets this run would not know:"
        echo "$OLD_VOLS" | sed 's/^/  /' >&2
        err "use a new --name, or have the owner destroy the old volumes (./opentr.sh fresh-destroy ${NAME})"
        exit 3
    fi
fi
TREE="$(prepare_tree)" || exit 1
if [[ "$ACTION" == prepare ]]; then echo "$TREE"; exit 0; fi
echo "isolated tree: $TREE (demo-reset.sh / demo-upgrade.sh run from here with DEMO_FRESH_NAME=$NAME)"

cd "$TREE" || exit 3
# shellcheck source=scripts/lib/stack-target.sh
source "$TREE/scripts/lib/stack-target.sh"

# The isolated stack's own diar-native models dir (.fresh/NAME/diar-native-models) starts empty and
# the generated .env has no HUGGINGFACE_TOKEN to provision it, so the sidecar would exit 8 forever
# (restart: unless-stopped). Seed it from the host's verified export, the same copy the release
# `test` stage makes (fresh-stack-lib.sh); never DIAR_ALLOW_UNVERIFIED_MODELS. RELEASE_TEST_DIAR_MODELS_SRC
# overrides the source. Not for the demo: the demo host has neither an export nor a token (it never
# diarizes), so no sidecar runs there, and an empty dir keeps it out of this stack's chain too.
if [[ "$MODE" == pki ]]; then
    DIAR_SRC="${RELEASE_TEST_DIAR_MODELS_SRC:-$(resolve_model_cache_dir "$REPO_ROOT")/diar-native}"
    if [[ -z "$(ls "$DIAR_SRC" 2>/dev/null)" ]]; then
        err "no diar-native export at $DIAR_SRC to seed the isolated stack from (set RELEASE_TEST_DIAR_MODELS_SRC)"
        exit 3
    fi
    REPO_ROOT="$TREE" RELEASE_TEST_DIAR_MODELS_SRC="$DIAR_SRC" fresh_stack_seed_models "$NAME" \
        || { err "could not seed diar-native models from $DIAR_SRC"; exit 3; }
fi

START=(./opentr.sh start prod --build --fresh "$NAME")
if [[ "$MODE" == pki ]]; then
    [[ -n "$OFFSET" ]] && START+=(--port-offset "$OFFSET")
    START+=(--with-pki)
else
    START+=(--lite --demo) # the offset ports are in the tree's .env (demo_install_env)
fi
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

# A --with-pki start generates PKI_TRUSTED_PROXIES from the project's docker network
# (scripts/pki/generate-test-env.sh), but on a fresh project that network only exists once `up`
# has run, so the first start falls back to 172.16.0.0/12 and the backend then refuses every
# header-sourced cert ("no configured trusted proxy can vouch"). docs/PKI_SETUP.md's remedy is
# "start once more so the network exists": do exactly that, only when the value differs.
settle_pki_trust() {
    local want have
    want="$(COMPOSE_PROJECT_NAME="otfresh-${SLUG}" ./scripts/pki/generate-test-env.sh \
        --print-trusted-proxies 2>/dev/null)" || { err "could not derive PKI trusted proxies"; return 1; }
    have="$(sed -n 's/^PKI_TRUSTED_PROXIES=//p' scripts/pki/test-certs/pki-test.env 2>/dev/null | tail -1)"
    [[ -n "$want" ]] || { err "empty derived PKI trusted proxies"; return 1; }
    [[ "$want" == "$have" ]] && return 0
    echo "PKI trusted proxies were '$have', the project network gives '$want': starting once more"
    "${START[@]}" || { err "restart failed: ${START[*]}"; return 1; }
}

if [[ "$MODE" == pki ]]; then
    settle_pki_trust || exit 1
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
    # Same as scripts/demo/demo-reset.sh (`run --rm -e DEMO_MODE=false ... demo_seed import`): the
    # seeder refuses to run with DEMO_MODE on, and only this one process is switched off.
    docker exec -e DEMO_MODE=false "$ctr" python -m app.scripts.demo_seed import --bundle /tmp/demo-bundle || exit 1
fi
exit 0
