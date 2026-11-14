#!/bin/bash
# Sourced by run_profile.sh. capture_service_logs PROJECT OUT START: the log of EVERY service the
# compose project has a container for (running or exited, so a worker that crashed mid-run is
# still captured), derived from the project label, never from a name pattern: a hard-coded
# `worker|beat` filter once dropped `celery-redaction`. Needs st_container (stack-target.sh).
capture_service_logs() {
    local project="$1" out="$2" start="$3" svc
    for svc in $(docker ps -a --filter "label=com.docker.compose.project=${project}" \
            --format '{{.Label "com.docker.compose.service"}}' | sort -u); do
        docker logs --since "$start" "$(st_container "$svc")" > "${out}/${svc}.log" 2>&1 || true
        gzip -f "${out}/${svc}.log"
    done
}
