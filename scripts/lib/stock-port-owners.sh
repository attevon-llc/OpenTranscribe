#!/bin/bash
# Who owns the stock ports (5173-5180)? ONE definition, sourced by the release stages.
#
# The rehearsal and smoke scenarios install under the installer's stock names and bind the
# stock ports on purpose. Their old "is the stack stopped" check asked docker for containers
# labelled with the two stock compose projects (`opentranscribe`, `transcribe-app`), so an
# `otfresh-*` stack — which the release `test` stage must run on the stock ports — passed it,
# and the scenarios then died in `gr_check_ports_free` with exit 1 (a release FAIL) instead of
# an unmet precondition (exit 3). This asks the question that matters instead: does ANY
# container publish a stock port, and which compose project does it belong to.
#
# Source it; it defines functions only. Pure bash — no pipes into early-exit readers
# (scripts/CLAUDE.md pipefail rule): docker's output is captured, then parsed in-shell.
#
#   stock_port_owners                    port<TAB>container<TAB>project<TAB>working_dir rows
#   stock_port_stop_hint P DIR [REPO] [CONTAINER]   the command that stops that owner
#   stock_port_check                     0 free · 1 owned · 2 could not check
#                                        sets STOCK_PORT_SUMMARY, STOCK_PORT_FIX
#
# `-` stands for an absent compose label so consumers can `read` tab-separated rows safely.

STOCK_PORT_LOW="${STOCK_PORT_LOW:-5173}"
STOCK_PORT_HIGH="${STOCK_PORT_HIGH:-5180}"

STOCK_PORT_SUMMARY=""
STOCK_PORT_FIX=""

# Rows on stdout; non-zero when docker cannot answer.
stock_port_owners() {
    local raw
    # `|` not a tab: an empty label is whitespace under IFS=$'\t' and `read` would collapse it.
    raw="$(docker ps --format '{{.Names}}|{{.Label "com.docker.compose.project"}}|{{.Label "com.docker.compose.project.working_dir"}}|{{.Ports}}')" || return 1

    local -A seen=()
    local name project workdir ports entry left hostport lo hi p
    while IFS='|' read -r name project workdir ports; do
        [[ -n "$name" ]] || continue
        project="${project:--}"
        workdir="${workdir:--}"
        local -a entries=()
        IFS=',' read -r -a entries <<< "$ports"
        for entry in "${entries[@]}"; do
            [[ "$entry" == *"->"* ]] || continue
            left="${entry%%->*}"
            hostport="${left##*:}"
            if [[ "$hostport" == *-* ]]; then
                lo="${hostport%-*}"; hi="${hostport#*-}"
            else
                lo="$hostport"; hi="$hostport"
            fi
            [[ "$lo" =~ ^[0-9]+$ && "$hi" =~ ^[0-9]+$ ]] || continue
            (( lo < STOCK_PORT_LOW )) && lo="$STOCK_PORT_LOW"
            (( hi > STOCK_PORT_HIGH )) && hi="$STOCK_PORT_HIGH"
            for (( p = lo; p <= hi; p++ )); do
                # IPv4 and IPv6 publishes of one port are one owner.
                [[ -n "${seen["$p|$name"]:-}" ]] && continue
                seen["$p|$name"]=1
                printf '%s\t%s\t%s\t%s\n' "$p" "$name" "$project" "$workdir"
            done
        done
    done <<< "$raw"
    return 0
}

# stock_port_stop_hint PROJECT WORKDIR [REPO_ROOT] [CONTAINER]
stock_port_stop_hint() {
    local project="$1" workdir="${2:--}" repo="${3:-${REPO_ROOT:-}}" container="${4:-}"
    local prefix=""
    if [[ "$workdir" != "-" && -n "$workdir" && "$workdir" != "$repo" ]]; then
        prefix="cd ${workdir} && "
    fi
    case "$project" in
        otfresh-*)
            printf '%s./opentr.sh stop --fresh %s\n' "$prefix" "${project#otfresh-}" ;;
        transcribe-app)
            printf '%s./opentr.sh stop\n' "$prefix" ;;
        -)
            printf 'docker stop %s   # not a compose project\n' "${container:-<container>}" ;;
        *)
            # A `curl | bash` install (or a release-test leftover) is stopped through its own
            # copy of opentranscribe.sh, not this checkout's opentr.sh.
            if [[ -n "$prefix" ]]; then
                printf '%s./opentranscribe.sh stop\n' "$prefix"
            else
                printf './opentr.sh stop\n'
            fi ;;
    esac
}

stock_port_check() {
    STOCK_PORT_SUMMARY=""
    STOCK_PORT_FIX=""
    local rows
    rows="$(stock_port_owners)" || return 2
    [[ -n "$rows" ]] || return 0

    local -A held=() hint_for=()
    local -a order=()
    local port container project workdir
    while IFS=$'\t' read -r port container project workdir; do
        [[ -n "$port" ]] || continue
        # A project is one owner; a bare container is its own.
        local key="$project"
        [[ "$project" == "-" ]] && key="container:$container"
        if [[ -z "${held[$key]:-}" ]]; then
            order+=("$key")
            hint_for[$key]="$(stock_port_stop_hint "$project" "$workdir" "${REPO_ROOT:-}" "$container")"
            held[$key]="${container}:${port}"
        else
            held[$key]+=", ${container}:${port}"
        fi
    done <<< "$rows"

    local key
    for key in "${order[@]}"; do
        [[ -n "$STOCK_PORT_SUMMARY" ]] && STOCK_PORT_SUMMARY+="; "
        STOCK_PORT_SUMMARY+="${key} holds ${held[$key]}"
        [[ -n "$STOCK_PORT_FIX" ]] && STOCK_PORT_FIX+=" ; "
        STOCK_PORT_FIX+="${hint_for[$key]}"
    done
    STOCK_PORT_SUMMARY="stock ports ${STOCK_PORT_LOW}-${STOCK_PORT_HIGH} are in use — ${STOCK_PORT_SUMMARY}"
    return 1
}
