#!/usr/bin/env bash
# Validate the monitoring configuration with the same pinned images the overlay runs
# (issue #791). Single source of the pins: docker-compose.monitoring.yml.
#
#   promtool check config   monitoring/prometheus/prometheus.yml (also loads rule_files)
#   promtool check rules    monitoring/prometheus/rules/*.yml
#   promtool test rules     monitoring/prometheus/tests/*.yml
#   amtool check-config     monitoring/alertmanager/alertmanager.yml
#
# Docs: https://prometheus.io/docs/prometheus/latest/configuration/unit_testing_rules/
#       https://prometheus.io/docs/alerting/latest/configuration/
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
overlay="$root/docker-compose.monitoring.yml"

image_of() {
  sed -n "s|^[[:space:]]*image:[[:space:]]*\(${1}[^[:space:]]*\).*|\1|p" "$overlay" | head -n1
}
prom="$(image_of 'prom/prometheus')"
am="$(image_of 'prom/alertmanager')"
[ -n "$prom" ] && [ -n "$am" ] || { echo "cannot find pinned images in $overlay" >&2; exit 2; }

run() { docker run --rm --network none "$@"; }

run --entrypoint promtool -v "$root/monitoring/prometheus:/etc/prometheus:ro" "$prom" \
  check config /etc/prometheus/prometheus.yml
# sh -c so the container expands the globs (promtool test rules does not).
run --entrypoint sh -v "$root/monitoring/prometheus:/etc/prometheus:ro" "$prom" \
  -c 'promtool check rules /etc/prometheus/rules/*.yml && promtool test rules /etc/prometheus/tests/*.yml'
run --entrypoint amtool -v "$root/monitoring/alertmanager:/etc/alertmanager:ro" "$am" \
  check-config /etc/alertmanager/alertmanager.yml
