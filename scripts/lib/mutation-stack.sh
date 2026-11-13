#!/usr/bin/env bash
# The stack check run-mutation-tests.sh makes before mutating anything. Sourced; needs the caller's
# colour variables and `port_open`.
#
# Postgres is the only hard service dependency (MinIO/OpenSearch tests auto-skip). The stack is
# RESOLVED (scripts/lib/stack_target.py), not assumed to be on 5176/5177: `--fresh NAME` (the
# caller's FRESH_NAME), else an exported POSTGRES_PORT or COMPOSE_PROJECT_NAME, is honoured, and a schema behind this
# checkout is refused, because mutants would "survive" against the wrong schema.

# shellcheck source=stack-target.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/stack-target.sh"
# shellcheck source=mutation-report.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/mutation-report.sh"

# mutation_check_stack -> 0 when the target stack is usable. Exports POSTGRES_PORT/REDIS_PORT.
mutation_check_stack() {
    local st_rc=0 ok=true
    st_target ${FRESH_NAME:+--fresh "$FRESH_NAME"} || st_rc=$?
    if [[ "$st_rc" -ne 0 ]]; then
        echo -e "${RED}✗ no stack resolved (stack-target exit $st_rc) — ./opentr.sh start dev${NC}"
        return 1
    fi
    st_require schema-head || ok=false
    if port_open "${POSTGRES_PORT:-0}"; then
        echo -e "${GREEN}✓ Postgres up ($ST_PROJECT :${POSTGRES_PORT})${NC}"
    else
        echo -e "${RED}✗ Postgres not reachable on ${POSTGRES_PORT:-unpublished}${NC}"
        ok=false
    fi
    if port_open "${REDIS_PORT:-0}"; then
        echo -e "${GREEN}✓ Redis up (${REDIS_PORT}) — lockout/session mutants need it${NC}"
    else
        echo -e "${YELLOW}! Redis not reachable on ${REDIS_PORT:-unpublished}: lockout/session modules${NC}"
        echo -e "${YELLOW}  will report false survivors. Fine for --module spans.${NC}"
    fi
    $ok
}

# mutation_check_tooling -> 0 when the venv and mutmut are usable. Uses the caller's VENV_BIN.
mutation_check_tooling() {
    local ok=true

    if [[ ! -x "$VENV_BIN/python" ]]; then
        echo -e "${RED}✗ backend/venv missing — create it per CLAUDE.md${NC}"; ok=false
    else
        echo -e "${GREEN}✓ backend/venv${NC}"
    fi

    if [[ ! -x "$VENV_BIN/mutmut" ]]; then
        echo -e "${RED}✗ mutmut not installed${NC}"
        echo -e "  ${YELLOW}$VENV_BIN/pip install 'mutmut>=3.2.0'${NC}  (it is in backend/requirements-dev.txt)"
        ok=false
    else
        echo -e "${GREEN}✓ mutmut $("$VENV_BIN/mutmut" version 2>/dev/null || echo '(version unknown)')${NC}"
    fi
    $ok
}

# ---------------------------------------------------------------------------------------------
# --parallel: several modules on ONE --fresh stack. Sourced; uses the caller's MODULE_PATH,
# MODULE_TESTS, OUT_DIR, REPO_ROOT, VENV_BIN, DRY_RUN, PAR_MODULES, PAR_JOBS and check_preconditions.
# The logic (plan, provision, probe, budget, judge) lives in mutation_isolation.py; this is only
# worktrees and job slots. Design: ~/ot-cleanup-work/speed/EFF_PLAN.md section 2a.
# ---------------------------------------------------------------------------------------------
_MUT_ISO_PY="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/mutation_isolation.py"
MUT_DEFAULT_MODULES="spans password_policy security dependencies lifecycle_gates external_token lockout lockout_store lockout_admin session"

_mut_iso() { "$VENV_BIN/python" "$_MUT_ISO_PY" "$@"; }

# The worktrees measure HEAD while check_baseline compares the MAIN checkout's files, so a dirty
# module or test file would mark the run NOT MEASURED after an hour. Refuse up front.
mutation_parallel_dirty_files() {
    local key f paths=()
    for key in "$@"; do
        paths+=("backend/${MODULE_PATH[$key]}")
        for f in ${MODULE_TESTS[$key]}; do paths+=("backend/$f"); done
    done
    git -C "$REPO_ROOT" status --porcelain -- "${paths[@]}"
}

# mutation_parallel_worktree KEY SHA -> prints the path; sparse (backend + scripts) to avoid 3 GB each.
mutation_parallel_worktree() {
    local key=$1 sha=$2 wt
    wt="${OT_MUTATION_WORKTREE_ROOT:-$(dirname "$REPO_ROOT")}/$(basename "$REPO_ROOT")-mut-$key"
    if [[ -e "$wt/.git" ]]; then
        [[ -z "$(git -C "$wt" status --porcelain)" ]] || { echo "dirty worktree $wt: refusing (remove it yourself)" >&2; return 3; }
        git -C "$wt" checkout -q --detach "$sha" || return 3
    else
        git -C "$REPO_ROOT" worktree add -q --detach --no-checkout "$wt" "$sha" || return 3
        git -C "$wt" sparse-checkout set --cone backend scripts || return 3
        git -C "$wt" checkout -q --detach "$sha" || return 3
    fi
    printf '%s\n' "$wt"
}

# mutation_parallel_run -> exit status per run-mutation-tests.sh's contract.
mutation_parallel_run() {
    local keys=() key jobs=$PAR_JOBS cpu children sha
    if [[ -n "$PAR_MODULES" ]]; then IFS=, read -r -a keys <<<"$PAR_MODULES"; else read -r -a keys <<<"$MUT_DEFAULT_MODULES"; fi
    for key in "${keys[@]}"; do
        [[ -n "${MODULE_PATH[$key]:-}" ]] || { echo -e "${RED}Unknown module '$key'.${NC}" >&2; return 2; }
    done
    [[ "$jobs" =~ ^[1-9][0-9]*$ ]] || { echo -e "${RED}--jobs must be a positive integer${NC}" >&2; return 2; }
    cpu=${OT_MUTATION_CPU_BUDGET:-$(( $(nproc) - 4 ))}
    children=$(( cpu / jobs )); (( children < 2 )) && children=2
    sha=$(git -C "$REPO_ROOT" rev-parse HEAD)
    local csv; csv=$(IFS=,; echo "${keys[*]}")

    local dirty; dirty=$(mutation_parallel_dirty_files "${keys[@]}")
    if [[ -n "$dirty" ]]; then
        echo -e "${RED}--parallel measures HEAD, but these module/test files are modified:${NC}\n$dirty" >&2
        return 3
    fi

    if $DRY_RUN; then
        local k db idx wt
        while IFS=$'\t' read -r k db idx; do
            wt="${OT_MUTATION_WORKTREE_ROOT:-$(dirname "$REPO_ROOT")}/$(basename "$REPO_ROOT")-mut-$k"
            echo "module $k: worktree=$wt max-children=$children jobs=$jobs"
            echo "    POSTGRES_DB=$db REDIS_URL=redis://***@<stack redis>/$idx addopts='-o addopts= -p no:cacheprovider -p no:testmon <tests>'"
        done < <(python3 "$_MUT_ISO_PY" plan --modules "$csv" --tsv)
        return 0
    fi

    check_preconditions || return 1
    _mut_iso budget --jobs "$jobs" --children "$children" || return 3
    local plan_tsv; plan_tsv=$(_mut_iso provision --modules "$csv" >/dev/null && _mut_iso plan --modules "$csv" --tsv) || return 3
    local iso
    iso="$OUT_DIR/parallel-$(date +%Y%m%dT%H%M%S).isolation"
    _mut_iso probe --modules "$csv" | tee "$iso" || { echo -e "${RED}isolation probe failed: nothing was mutated${NC}" >&2; return 3; }

    local db idx wt rc_dir="$OUT_DIR/parallel-rc"
    rm -rf "$rc_dir"; mkdir -p "$rc_dir"
    while IFS=$'\t' read -r key db idx; do
        wt=$(mutation_parallel_worktree "$key" "$sha") || return 3
        while (( $(jobs -rp | wc -l) >= jobs )); do wait -n || true; done
        (
            cd "$wt" || exit 3
            eval "$(_mut_iso env --module "$key" --index "$idx")"
            export OT_MUTATION_OUT_DIR="$OUT_DIR" OT_MUTATION_VENV="$BACKEND/venv" \
                   OT_MUTATION_MAX_CHILDREN="$children" OT_MUTATION_PARALLEL_JOBS="$jobs" \
                   OT_MUTATION_ISOLATION="db:$db,redis_index:$idx"
            "$wt/scripts/run-mutation-tests.sh" --module "$key" >"$OUT_DIR/$key.console" 2>&1
            echo $? >"$rc_dir/$key"
        ) &
    done <<<"$plan_tsv"
    wait
    mutation_parallel_report "$rc_dir" "${keys[@]}"
}

# mutation_parallel_report RC_DIR KEY... -> the run's exit status. Per module: the judge's verdict,
# then the survivors' file:line. The children's own output goes to <key>.console, which nobody
# watches, so this is the only console a --parallel operator reads (#1373: MUTRUN4 saw bare counts).
mutation_parallel_report() {
    local rc_dir=$1 rc=0 crc jrc base key
    shift
    for key in "$@"; do
        crc=$(cat "$rc_dir/$key" 2>/dev/null || echo 4)
        base=$(grep -P "^${key}\t" "$REPO_ROOT/scripts/mutation-baselines.tsv" | cut -f2)
        jrc=0; _mut_iso judge --module "$key" --log "$OUT_DIR/$key.log" ${base:+--baseline "$base"} || jrc=$?
        echo "  child exit $crc, judge exit $jrc ($key)"
        logged_survivor_locations "$key" "$OUT_DIR/$key.log"
        (( crc == 4 || jrc == 4 )) && rc=4
        (( rc != 4 && ( crc == 1 || jrc == 1 ) )) && rc=1
        (( rc == 0 && crc != 0 )) && rc=$crc
    done
    return "$rc"
}
