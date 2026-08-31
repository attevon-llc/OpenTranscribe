#!/bin/bash
# Assertions about the compose overlay chain a deployment ACTUALLY resolved.
#
# WHY THIS FILE EXISTS
#
# Both scenarios used to hand-build `-f docker-compose.yml -f docker-compose.prod.yml
# [-f docker-compose.gpu.yml]` from their own TEST_USE_GPU variable. That is a second
# implementation of `opentranscribe.sh:get_compose_files()`, and it meant the shipped
# selector — GPU vs Blackwell vs CPU-only, nginx, scheduled backup — was never
# exercised by a release gate. release-manifest.txt's own header records what that
# costs: a fresh install on a Blackwell card silently fell back to the generic GPU
# overlay (i.e. the wrong image), and no rehearsal could have seen it.
#
# The scenarios now bring stacks up with `./opentranscribe.sh start`, so the selector
# runs for real. These helpers are the other half: they READ BACK what it chose and
# assert it, so "the containers came up" is no longer the whole verdict.
#
# `opentr.sh` is deliberately not involved anywhere here. It is the DEVELOPMENT script,
# is absent from release-manifest.txt on purpose, and a curl install never has it —
# see _stage_manager_at in test-upgrade.sh for the measured reason. `opentranscribe.sh`
# is what a real self-hoster runs, so it is what the rehearsal runs.
#
# THE ORACLE IS DELIBERATELY NOT A COPY OF THE IMPLEMENTATION.
#
# cc_assert_chain does not re-run the selector's branch structure and compare. It
# asserts a handful of PROPERTIES, each derived from a couple of cheap, independently
# observable facts (does docker advertise an nvidia runtime; what compute capability
# does nvidia-smi report; what does this install's own .env say; which overlay files
# are on disk). A property set that is smaller and differently shaped than the code it
# checks is what keeps this an oracle rather than a third implementation.

set -euo pipefail

cc_log() { echo -e "\033[0;34m[compose-chain]\033[0m $*" >&2; }

# cc_resolve_chain INSTALL_DIR
#   Echo the `-f ...` chain the SHIPPED script resolves for INSTALL_DIR.
#   Banners go to stderr inside opentranscribe.sh, so stdout is exactly the chain.
cc_resolve_chain() {
    local dir="$1"
    ( cd "$dir" && ./opentranscribe.sh compose-files 2>/dev/null )
}

# cc_chain_files CHAIN
#   The compose filenames from a chain, one per line, in order.
cc_chain_files() {
    # shellcheck disable=SC2001  # sed is clearer than a bash loop for this
    echo "$1" | tr ' ' '\n' | grep -v '^-f$' | grep -v '^$' || true
}

# cc_manifest_compose_overlays REPO_ROOT
#   Every compose file release-manifest.txt tells an installer to download.
#   Read from the manifest, never hardcoded here — a second list is the bug this
#   whole change set exists to remove.
cc_manifest_compose_overlays() {
    local repo_root="$1"
    awk -F'\t' '
        /^[[:space:]]*#/ { next }
        /^[[:space:]]*$/ { next }
        $1 ~ /^docker-compose.*\.yml$/ { print $1 }
    ' "$repo_root/release-manifest.txt"
}

# cc_expected_gpu_overlay INSTALL_DIR
#   Echo the GPU overlay this HOST should get: "docker-compose.blackwell.yml",
#   "docker-compose.gpu.yml", or "" for none. Derived from three facts, not from
#   get_compose_files(): the operator's opt-out, docker's advertised runtimes, and the
#   card's compute capability.
cc_expected_gpu_overlay() {
    local dir="$1"

    # The operator's explicit CPU-only choice (setup-opentranscribe.sh --cpu) wins over
    # everything, because an advertised nvidia runtime is necessary but not sufficient
    # for a working GPU.
    if [[ -n "${OPENTRANSCRIBE_FORCE_CPU:-}" ]]; then echo ""; return 0; fi
    local forced=""
    forced="$(grep -E '^FORCE_CPU_MODE=' "$dir/.env" 2>/dev/null | head -1 | cut -d= -f2- | tr -d ' "' || true)"
    if [[ "$forced" == "true" ]]; then echo ""; return 0; fi

    if ! docker info 2>/dev/null | grep -q "Runtimes.*nvidia"; then echo ""; return 0; fi

    local cap=""
    cap="$(nvidia-smi --query-gpu=compute_cap --format=csv,noheader 2>/dev/null | head -1 | tr -d '[:space:]' || true)"
    if [[ "$cap" == 12.* && -f "$dir/docker-compose.blackwell.yml" ]]; then
        echo "docker-compose.blackwell.yml"; return 0
    fi
    if [[ -f "$dir/docker-compose.gpu.yml" ]]; then
        echo "docker-compose.gpu.yml"; return 0
    fi
    echo ""
}

# cc_expected_nginx_overlay INSTALL_DIR — "docker-compose.nginx.yml" or ""
cc_expected_nginx_overlay() {
    local dir="$1" name=""
    name="$(grep -E '^NGINX_SERVER_NAME=' "$dir/.env" 2>/dev/null | head -1 | cut -d= -f2- | tr -d ' "' || true)"
    if [[ -n "$name" && -f "$dir/docker-compose.nginx.yml" \
          && -f "$dir/${NGINX_CERT_FILE:-nginx/ssl/server.crt}" \
          && -f "$dir/${NGINX_CERT_KEY:-nginx/ssl/server.key}" ]]; then
        echo "docker-compose.nginx.yml"
    else
        echo ""
    fi
}

# cc_expected_backup_overlay INSTALL_DIR — "docker-compose.backup.yml" or ""
#   Keyed on the DEDICATED toggle, never on BACKUP_HOST_PATH: .env.example ships that
#   set, so a truthiness test would enable the overlay (and its OpenSearch path.repo
#   recreate) for every install (issue #616).
cc_expected_backup_overlay() {
    local dir="$1" enabled=""
    enabled="$(grep -E '^BACKUP_OVERLAY_ENABLED=' "$dir/.env" 2>/dev/null | head -1 | cut -d= -f2- | tr -d ' "' || true)"
    if [[ "$enabled" == "true" && -f "$dir/docker-compose.backup.yml" ]]; then
        echo "docker-compose.backup.yml"
    else
        echo ""
    fi
}

# cc_assert_chain LABEL INSTALL_DIR REPO_ROOT
#   Record the resolved chain in the report and assert every property of it.
#   Requires assertions.sh (as_record/as_assert_eq) to be sourced by the caller.
cc_assert_chain() {
    local label="$1" dir="$2" repo_root="$3"

    local chain=""
    if ! chain="$(cc_resolve_chain "$dir")"; then
        as_record FAIL "$label: './opentranscribe.sh compose-files' resolved a chain" \
            "command failed in $dir"
        return 0
    fi
    local files=()
    while IFS= read -r f; do [[ -n "$f" ]] && files+=("$f"); done < <(cc_chain_files "$chain")

    cc_log "$label resolved: $chain"
    # Recorded as an assertion row with the chain in the Detail column, NOT as a fenced
    # markdown block: the report body is one long `| Status | Assertion | Detail |`
    # table, and anything else spliced into it ends the table for every row after it.
    if (( ${#files[@]} == 0 )); then
        as_record FAIL "$label: resolved a compose chain" "empty chain from ./opentranscribe.sh compose-files"
        return 0
    fi
    as_record PASS "$label: resolved a compose chain" "\`$chain\`"
    printf '%s\t%s\n' "$label" "$chain" >> "${TEST_ROOT:-/tmp}/compose-chains.tsv"

    # Order is not cosmetic: compose merges later -f files ONTO earlier ones, and the
    # base file is what carries every service definition.
    as_assert_eq "$label: base compose file is first" "docker-compose.yml" "${files[0]}"
    if [[ -f "$dir/docker-compose.prod.yml" ]]; then
        as_assert_eq "$label: prod overlay is second" "docker-compose.prod.yml" "${files[1]:-<none>}"
    fi

    # A chain naming a file the deployment does not have is the silent-degradation
    # shape the `[ -f ]` guards create; assert it directly rather than trusting them.
    local missing=()
    local f
    for f in "${files[@]}"; do
        [[ -f "$dir/$f" ]] || missing+=("$f")
    done
    as_assert "$label: every file in the chain exists in the install" '(( ${#missing[@]} == 0 ))'
    (( ${#missing[@]} == 0 )) || cc_log "MISSING from $dir: ${missing[*]}"

    # THE ASSERTION THAT WOULD HAVE CAUGHT THE BLACKWELL REGRESSION, on any host.
    # Every compose overlay release-manifest.txt promises must actually be on disk —
    # otherwise `[ -f ]` turns "selectable" into "silently skipped" for whichever
    # hardware needs it, and only that hardware ever finds out.
    local undownloaded=() overlay
    while IFS= read -r overlay; do
        [[ -n "$overlay" ]] || continue
        [[ -f "$dir/$overlay" ]] || undownloaded+=("$overlay")
    done < <(cc_manifest_compose_overlays "$repo_root")
    as_assert "$label: every compose overlay in release-manifest.txt was downloaded" \
        '(( ${#undownloaded[@]} == 0 ))'
    (( ${#undownloaded[@]} == 0 )) || cc_log "NOT downloaded into $dir: ${undownloaded[*]}"

    # Per-overlay expectations, each derived independently (see the header).
    local want_gpu want_nginx want_backup
    want_gpu="$(cc_expected_gpu_overlay "$dir")"
    want_nginx="$(cc_expected_nginx_overlay "$dir")"
    want_backup="$(cc_expected_backup_overlay "$dir")"

    local got_gpu="" got_nginx="" got_backup=""
    for f in "${files[@]}"; do
        case "$f" in
            docker-compose.gpu.yml|docker-compose.blackwell.yml) got_gpu="$f" ;;
            docker-compose.nginx.yml) got_nginx="$f" ;;
            docker-compose.backup.yml) got_backup="$f" ;;
        esac
    done

    as_assert_eq "$label: GPU overlay matches this host's hardware and .env" \
        "${want_gpu:-<none>}" "${got_gpu:-<none>}"
    as_assert_eq "$label: nginx overlay matches NGINX_SERVER_NAME + certificates" \
        "${want_nginx:-<none>}" "${got_nginx:-<none>}"
    as_assert_eq "$label: backup overlay matches BACKUP_OVERLAY_ENABLED" \
        "${want_backup:-<none>}" "${got_backup:-<none>}"

    # Finally: is it a valid compose project at all? A chain that resolves but does not
    # validate is the failure mode issue #613 measured (base file alone: "service ...
    # has neither an image nor a build context specified").
    local config_rc=0
    ( cd "$dir" && docker compose $chain config --quiet ) >/dev/null 2>&1 || config_rc=$?
    as_assert_eq "$label: resolved chain is a valid compose project" "0" "$config_rc"
}
