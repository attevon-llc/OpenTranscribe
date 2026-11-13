#!/usr/bin/env bash
# The stack check run-mutation-tests.sh makes before mutating anything. Sourced; needs the caller's
# colour variables and `port_open`.
#
# Postgres is the only hard service dependency (MinIO/OpenSearch tests auto-skip). The stack is
# RESOLVED (scripts/lib/stack_target.py), not assumed to be on 5176/5177: an exported
# POSTGRES_PORT or COMPOSE_PROJECT_NAME for a --fresh stack is honoured, and a schema behind this
# checkout is refused, because mutants would "survive" against the wrong schema.

# shellcheck source=stack-target.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/stack-target.sh"

# mutation_check_stack -> 0 when the target stack is usable. Exports POSTGRES_PORT/REDIS_PORT.
mutation_check_stack() {
    local st_rc=0 ok=true
    st_target || st_rc=$?
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
